"""Asking the printer which roll is in and how it is (Spec 8.5): Brother's status block over SNMP
(v1, community "public", OID 1.3.6.1.4.1.2435.3.3.9.1.6.1.0), at most one second, and what the
answer means. The request is compared with bytes written out by hand; the answers are built here
by a small encoder of the test's own, so the parser is not tested against itself."""

import socket
import threading
import time

import pytest

from reprintbuddy.labels import snmp, status
from reprintbuddy.labels.rolls import DK_11209

OID = "1.3.6.1.4.1.2435.3.3.9.1.6.1.0"
OID_BYTES = bytes.fromhex("2b0601040193030303090106" "0100")  # 1.3 6 1 4 1 2435 3 3 9 1 6 1 0


def ber(tag, body):
    assert len(body) < 128
    return bytes([tag, len(body)]) + body


def integer(value):
    raw = value.to_bytes(max(1, (value.bit_length() + 8) // 8), "big", signed=True)
    return ber(0x02, raw)


# The status block the real printer sent (QL-820NWBc, a 62 x 29 roll, ready), from the notes of the
# Filamentwaage's tests: 32 bytes.
READY = bytes.fromhex("8020423441300400" "0000" "3e0b" "0000030000" "1d" "00000000000000" "01" "0000000000" "00")


def block(*, error1=0, error2=0, width=62, media=0x0B, length=29, kind=0x00, phase=0x00):
    data = bytearray(READY)
    data[8], data[9], data[10], data[11], data[17], data[18], data[19] = error1, error2, width, media, length, kind, phase
    return bytes(data)


def response(value, *, request_id=0x01020304, error_status=0, oid=OID_BYTES, community=b"public", value_tag=0x04):
    varbind = ber(0x30, ber(0x06, oid) + ber(value_tag, value))
    pdu = ber(0xA2, integer(request_id) + integer(error_status) + integer(0) + ber(0x30, varbind))
    return ber(0x30, integer(0) + ber(0x04, community) + pdu)


# ---- the request ----


def test_the_request_is_the_documented_snmp_get():
    expected = bytes.fromhex(
        "302f"  # message
        "020100"  # version 1
        "0406" + b"public".hex() +
        "a022"  # GetRequest
        "020401020304"  # request id
        "020100" "020100"  # no error
        "3014" "3012" "060e" + "2b0601040193030303090106" "0100" + "0500"
    )
    assert snmp.build_get(OID, request_id=0x01020304) == expected
    assert len(expected) == 49


@pytest.mark.parametrize("request_id", [1, 127, 128, 255, 256, 0x7FFFFFFF])
def test_the_request_id_is_a_valid_integer_for_any_size(request_id):
    message = snmp.build_get(OID, request_id=request_id)
    assert snmp.parse_response(response(READY, request_id=request_id), OID, request_id) == READY
    assert message[0] == 0x30


# ---- the answer ----


def test_the_answer_of_the_printer_is_read():
    assert snmp.parse_response(response(READY), OID, 0x01020304) == READY


@pytest.mark.parametrize(
    "answer",
    [
        b"",
        b"\x30",
        b"\x30\x05\x02\x01\x00",
        response(READY)[:-3],  # cut off
        response(READY) + b"\x00",  # more than it says
        response(READY, request_id=99),  # somebody else's answer
        response(READY, error_status=2),  # noSuchName
        response(READY, oid=bytes.fromhex("2b06010201010100")),  # another value
        response(READY, community=b"other"),
        response(READY, value_tag=0x02),  # not an octet string
        b"\xff" * 78,
        b"GET / HTTP/1.0\r\n\r\n",
    ],
)
def test_anything_else_is_no_answer(answer):
    assert snmp.parse_response(answer, OID, 0x01020304) is None


# ---- over the network: a stub on this machine, never a printer ----


class SnmpStub:
    """Answers one request: with `value` in a proper answer, or `raw` bytes as they are; `wrong_first`
    sends an answer with a foreign request id before the right one; `silent` says nothing."""

    def __init__(self, value=None, *, raw=None, silent=False, wrong_first=False):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.seen = []
        self.value, self.raw, self.silent, self.wrong_first = value, raw, silent, wrong_first
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        self.sock.settimeout(3)
        try:
            data, peer = self.sock.recvfrom(2048)
        except OSError:
            return
        self.seen.append(data)
        if self.silent:
            return
        if self.raw is not None:
            self.sock.sendto(self.raw, peer)
            return
        at = data.index(bytes([0xA0])) + 3  # tag, length, then the request id: 02 04 <4 bytes>
        request_id = int.from_bytes(data[at + 1 : at + 5], "big")
        if self.wrong_first:
            self.sock.sendto(response(self.value, request_id=request_id ^ 1), peer)
        self.sock.sendto(response(self.value, request_id=request_id), peer)

    def stop(self):
        self.sock.close()


@pytest.fixture
def stub():
    made = []

    def make(*args, **kwargs):
        made.append(SnmpStub(*args, **kwargs))
        return made[-1]

    yield make
    for item in made:
        item.stop()


def ask(port, **kwargs):
    kwargs.setdefault("allow_local", True)
    return snmp.query_status("127.0.0.1", port=port, **kwargs)


def test_the_status_block_comes_back_from_a_printer_that_answers(stub):
    s = stub(READY)
    assert ask(s.port) == READY
    assert len(s.seen) == 1 and s.seen[0][:2] == b"\x30\x2f"


def test_a_printer_that_stays_silent_is_no_answer_after_the_time_limit(stub):
    s = stub(silent=True)
    started = time.monotonic()
    assert ask(s.port, timeout=0.3) is None
    assert time.monotonic() - started < 1.0


def test_a_foreign_answer_first_does_not_count_and_the_right_one_after_it_does(stub):
    s = stub(READY, wrong_first=True)
    assert ask(s.port, timeout=0.5) == READY


def test_a_broken_answer_is_no_answer(stub):
    s = stub(raw=b"\x30\x03\x02\x01\x00")  # sent as it is
    assert ask(s.port, timeout=0.3) is None


def test_a_port_nobody_listens_on_is_no_answer_not_an_error():
    free = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    assert ask(port, timeout=0.3) is None


def test_this_machine_is_refused_without_the_test_switch():
    from reprintbuddy.labels.errors import PrinterAddressRefused

    with pytest.raises(PrinterAddressRefused):
        snmp.query_status("127.0.0.1", port=9)


# ---- what the block says ----


def test_the_block_of_a_ready_printer_with_the_right_roll():
    parsed = status.parse(READY)
    assert (parsed.width_mm, parsed.length_mm, parsed.media_type) == (62, 29, 0x0B)
    assert parsed.error1 == 0 and parsed.error2 == 0
    assert status.problem(parsed, DK_11209) is None


@pytest.mark.parametrize("data", [b"", READY[:31], READY + b"\x00", b"\x00" * 32, b"\x80\x20" + b"X" * 30, bytes(32)])
def test_a_block_that_is_not_brothers_is_not_read(data):
    assert status.parse(data) is None


def test_the_other_family_of_the_die_cut_type_counts_too():
    assert status.problem(status.parse(block(media=0x4B)), DK_11209) is None


@pytest.mark.parametrize(
    "fields,key",
    [
        (dict(error2=0x10), "cover_open"),
        (dict(error1=0x02), "roll_empty"),
        (dict(error2=0x40), "roll_empty"),
        (dict(error2=0x01), "roll_empty"),  # "replace media"
        (dict(error1=0x01, width=0, media=0, length=0), "no_roll"),
        (dict(width=0, media=0, length=0), "no_roll"),
        (dict(error1=0x04), "cutter"),
        (dict(error2=0x80), "error"),
        (dict(error2=0x04), "error"),
        (dict(error2=0x02), "error"),
    ],
)
def test_the_problems_the_printer_reports(fields, key):
    found = status.problem(status.parse(block(**fields)), DK_11209)
    assert found is not None and found.key == key


def test_the_cover_is_named_before_anything_else():
    found = status.problem(status.parse(block(error1=0x03, error2=0x10 | 0x80)), DK_11209)
    assert found.key == "cover_open"


def test_a_busy_printer_does_not_block_a_print():
    assert status.problem(status.parse(block(error1=0x10)), DK_11209) is None


def test_a_printer_that_is_switching_off_does_not_block():
    assert status.problem(status.parse(block(error1=0x20)), DK_11209) is None


def test_another_die_cut_roll_names_what_is_in():
    found = status.problem(status.parse(block(width=62, length=100)), DK_11209)
    assert found.key == "wrong_roll"
    assert found.params == {"width": 62, "length": 100, "continuous": False, "known": None}


def test_a_continuous_roll_is_named_as_such():
    found = status.problem(status.parse(block(media=0x4A, width=62, length=0)), DK_11209)
    assert found.key == "wrong_roll"
    assert found.params == {"width": 62, "length": 0, "continuous": True, "known": None}


def test_the_same_size_but_continuous_is_no_match():
    found = status.problem(status.parse(block(media=0x4A, width=62, length=29)), DK_11209)
    assert found.key == "wrong_roll" and found.params["continuous"] is True


def test_a_known_roll_that_is_not_the_chosen_one_is_named_by_its_key(monkeypatch):
    from reprintbuddy.labels import rolls

    other = rolls.Roll("DK-TEST", "DK-TEST", 29, 90, 306, 991, 0)
    monkeypatch.setitem(rolls.ROLLS, "DK-TEST", other)
    found = status.problem(status.parse(block(width=29, length=90)), DK_11209)
    assert found.key == "wrong_roll" and found.params["known"] == "DK-TEST"
