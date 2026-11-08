"""The Brother raster job for one label and how it is sent (Spec 8.5). The expected bytes come from
Brother's raster command reference, written out here by hand, not from the code under test."""

import socket
import threading

import pytest
from PIL import Image

from reprintbuddy.labels import brother
from reprintbuddy.labels.errors import PrinterAddressRefused, PrinterUnreachable, SendAborted
from reprintbuddy.labels.rolls import DK_11209, ROLLS, roll_by_key

ROW = 90  # bytes per raster line: the print head has 720 dots


def white():
    return Image.new("1", (696, 271), 1)


def black_pixel(x, y):
    image = white()
    image.putpixel((x, y), 0)
    return image


def header():
    return (
        bytes(400)  # invalidate
        + b"\x1b\x40"  # initialise
        + b"\x1b\x69\x61\x01"  # raster mode
        + b"\x1b\x69\x7a\xce\x0b\x3e\x1d\x0f\x01\x00\x00\x00\x00"  # print info: die-cut 62 x 29, 271 lines, first page
        + b"\x1b\x69\x4d\x40"  # auto cut
        + b"\x1b\x69\x41\x01"  # cut after every label
        + b"\x1b\x69\x4b\x08"  # cut at the end, no two-colour, no high resolution
        + b"\x1b\x69\x64\x00\x00"  # margin 0
        + b"\x4d\x00"  # no compression
    )


def line(data=bytes(ROW)):
    return b"\x67\x00\x5a" + data


def test_the_roll_table_knows_the_one_roll():
    assert list(ROLLS) == ["DK-11209"]
    assert roll_by_key("DK-11209") is DK_11209
    assert roll_by_key("nope") is None
    assert (DK_11209.width_mm, DK_11209.length_mm, DK_11209.dots_wide, DK_11209.dots_high) == (62, 29, 696, 271)


def test_a_white_label_is_the_documented_byte_sequence():
    job = brother.build_job(white(), DK_11209)
    assert job == header() + line() * 271 + b"\x1a"
    assert len(job) == 25642  # 438 + 271 lines of 93 bytes + the end byte


def test_the_leftmost_dot_ends_up_at_the_far_end_of_the_line():
    """Brother numbers the dots from the other side: every line is mirrored (dot 0 = right edge)."""
    job = brother.build_job(black_pixel(0, 0), DK_11209)
    first = job[len(header()) : len(header()) + 93]
    expected = bytearray(ROW)
    expected[88] = 0x10  # data bit 695 sits at head dot 12 + 695 = 707: byte 88, bit index 3
    assert first == line(bytes(expected))
    assert job[len(header()) + 93 : -1] == line() * 270


def test_the_rightmost_dot_ends_up_after_the_left_margin():
    job = brother.build_job(black_pixel(695, 270), DK_11209)
    last = job[-1 - 93 : -1]
    expected = bytearray(ROW)
    expected[1] = 0x08  # head dot 12: byte 1, bit index 4
    assert last == line(bytes(expected))


def test_every_dark_dot_prints_and_nothing_spills_into_the_margins():
    image = Image.new("1", (696, 271), 0)  # all black
    job = brother.build_job(image, DK_11209)
    body = job[len(header()) : -1]
    for n in range(271):
        data = body[n * 93 + 3 : (n + 1) * 93]
        assert data[0] == 0x00 and data[1] == 0x0F  # 12 margin bits, then 4 data bits
        assert data[2:88] == b"\xff" * 86
        assert data[88] == 0xF0 and data[89] == 0x00


def test_a_picture_of_the_wrong_size_is_refused():
    with pytest.raises(ValueError):
        brother.build_job(Image.new("1", (100, 271), 1), DK_11209)


def test_a_grey_picture_is_made_black_and_white_first():
    grey = Image.new("L", (696, 271), 255)
    grey.putpixel((695, 0), 40)
    job = brother.build_job(grey, DK_11209)
    assert job[len(header()) + 3 + 1] == 0x08


# ---- sending: only ever to a stub on this machine ----


class Printer:
    """A stand-in for the printer's raw port: takes the bytes, then closes (or never closes)."""

    def __init__(self, *, close_after_read=True, reset=False):
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.received = b""
        self.close_after_read = close_after_read
        self.reset = reset
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        try:
            conn, _ = self.server.accept()
        except OSError:
            return  # stopped before anybody connected
        conn.settimeout(3)
        want = 25642
        try:
            while len(self.received) < want:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                self.received += chunk
        except OSError:
            pass
        if self.reset:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
            conn.close()
            return
        if self.close_after_read:
            conn.close()
        else:
            try:
                conn.settimeout(4)
                conn.recv(1)
            except OSError:
                pass
            conn.close()

    def stop(self):
        self.server.close()


@pytest.fixture
def printer():
    made = []

    def make(**kwargs):
        made.append(Printer(**kwargs))
        return made[-1]

    yield make
    for p in made:
        p.stop()


def send(port, **kwargs):
    job = brother.build_job(white(), DK_11209)
    kwargs.setdefault("allow_local", True)
    return job, brother.send_job("127.0.0.1", job, port=port, **kwargs)


def test_the_whole_job_arrives_and_the_printer_closing_ends_the_send(printer):
    p = printer()
    job, result = send(p.port)
    assert result is None
    p.thread.join(3)
    assert p.received == job


def test_a_printer_that_never_closes_is_waited_for_only_so_long(printer):
    import time

    p = printer(close_after_read=False)
    started = time.monotonic()
    job, result = send(p.port, wait_timeout=0.4)
    assert time.monotonic() - started < 2
    assert result is None  # everything was handed over; that counts as sent
    p.thread.join(5)
    assert p.received == job


def test_nobody_listening_is_not_reachable():
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    with pytest.raises(PrinterUnreachable):
        send(port, connect_timeout=0.5)


def test_a_reset_in_the_middle_is_an_aborted_job(printer):
    p = printer(reset=True)  # reads the first label's worth, then resets the connection
    with pytest.raises(SendAborted):
        brother.send_job("127.0.0.1", bytes(8_000_000), port=p.port, allow_local=True)


def test_loopback_and_wildcard_are_refused_without_the_test_switch():
    for host in ("127.0.0.1", "::1", "0.0.0.0", "::", "localhost"):
        with pytest.raises(PrinterAddressRefused):
            brother.send_job(host, b"x", port=9)


def test_a_name_that_resolves_to_this_machine_is_refused(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 9100))],
    )
    with pytest.raises(PrinterAddressRefused):
        brother.send_job("printer.example", b"x")


def test_a_name_that_does_not_resolve_is_not_reachable(monkeypatch):
    def fail(*a, **k):
        raise socket.gaierror("no such name")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(PrinterUnreachable):
        brother.send_job("printer.example", b"x")


def test_a_device_that_keeps_sending_does_not_hold_the_wait_beyond_its_limit():
    """The limit is for the whole wait, not for each read: a byte every 0.2 s must not stretch it."""
    import time

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    stop = threading.Event()

    def dribble():
        conn, _ = server.accept()
        conn.settimeout(2)
        try:
            while not stop.is_set():
                conn.sendall(b"x")
                time.sleep(0.2)
        except OSError:
            pass
        conn.close()

    threading.Thread(target=dribble, daemon=True).start()
    started = time.monotonic()
    try:
        brother.send_job("127.0.0.1", b"job", port=server.getsockname()[1], wait_timeout=0.5, allow_local=True)
    finally:
        stop.set()
        server.close()
    assert time.monotonic() - started < 1.5


def test_a_name_that_takes_too_long_to_resolve_is_not_reachable_within_the_limit(monkeypatch):
    import time

    from reprintbuddy.labels import addresses

    def slow(*a, **k):
        time.sleep(1.5)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.51.100.9", 9100))]

    monkeypatch.setattr(socket, "getaddrinfo", slow)
    started = time.monotonic()
    with pytest.raises(PrinterUnreachable):
        addresses.resolve("printer.example", resolve_timeout=0.3)
    assert time.monotonic() - started < 1.0


def test_an_ipv4_address_in_ipv6_form_is_as_local_as_the_plain_one():
    from reprintbuddy.labels import addresses

    for text in ("::ffff:127.0.0.1", "::ffff:0.0.0.0", "::ffff:224.0.0.1"):
        assert addresses.is_local_or_wildcard(text), text
    assert not addresses.is_local_or_wildcard("::ffff:198.51.100.9")
