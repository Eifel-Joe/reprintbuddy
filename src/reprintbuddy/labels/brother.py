"""Brother's raster language (QL-810W / QL-820NWB) and the sending of a job to the printer's raw
port 9100 (Spec 8.5).

The bytes of one label are exactly the documented sequence of Brother's "Raster Command
Reference": invalidate, initialise, raster mode, print information, auto cut, expanded mode,
margin, no compression, one raster line per row of the picture, print with feed. One label is one
job. The printer answers nothing on this port; it closes the connection once it has read the job.

Network rules (Spec 8.5): connect for at most 3 seconds, wait for the printer to close for at most
10 seconds, never send a job a second time by itself. The address comes from the settings only and
may never be this machine itself (a loopback or wildcard address) unless a test says so.
"""

from __future__ import annotations

import socket
import time

from PIL import Image

from reprintbuddy.labels.addresses import RAW_PORT, resolve
from reprintbuddy.labels.errors import PrinterUnreachable, SendAborted
from reprintbuddy.labels.rolls import HEAD_DOTS, Roll

PORT = RAW_PORT
CONNECT_TIMEOUT = 3.0
WAIT_TIMEOUT = 10.0
_LINE_BYTES = HEAD_DOTS // 8  # 90


def _raster_lines(image: Image.Image, roll: Roll) -> list[bytes]:
    """One 90-byte head line per row of the picture: dark = dot, mirrored (Brother numbers the dots
    from the other side), the roll's margin dots left empty on both sides."""
    if image.size != (roll.dots_wide, roll.dots_high):
        raise ValueError(f"the picture must be {roll.dots_wide} x {roll.dots_high} dots")
    dark = image.convert("L").point(lambda value: 255 if value < 128 else 0).convert("1")
    mirrored = dark.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    row_bytes = roll.dots_wide // 8
    data = mirrored.tobytes()
    lines = []
    for row in range(roll.dots_high):
        bits = int.from_bytes(data[row * row_bytes : (row + 1) * row_bytes], "big")
        shifted = bits << (HEAD_DOTS - roll.margin_dots - roll.dots_wide)
        lines.append(shifted.to_bytes(_LINE_BYTES, "big"))
    return lines


def build_job(image: Image.Image, roll: Roll) -> bytes:
    """The complete print job of one label (about 25 kB for the 62 x 29 roll)."""
    lines = _raster_lines(image, roll)
    count = len(lines).to_bytes(4, "little")
    parts = [
        bytes(400),  # invalidate: gets the printer out of any half-read job
        b"\x1b\x40",  # initialise
        b"\x1b\x69\x61\x01",  # raster command mode
        # print information: valid flags (type, width, length, quality), type, width, length,
        # number of raster lines, first page, no extra
        b"\x1b\x69\x7a" + bytes([0xCE, roll.media_type, roll.width_mm, roll.length_mm]) + count + b"\x00\x00",
        b"\x1b\x69\x4d\x40",  # auto cut on
        b"\x1b\x69\x41\x01",  # cut after every label
        b"\x1b\x69\x4b\x08",  # expanded mode: cut at end, one colour, normal resolution
        b"\x1b\x69\x64\x00\x00",  # margin 0 (the only value for die-cut)
        b"\x4d\x00",  # no compression
    ]
    parts.extend(b"\x67\x00" + bytes([_LINE_BYTES]) + line for line in lines)
    parts.append(b"\x1a")  # print with feed (last page)
    return b"".join(parts)


def send_job(
    host: str,
    data: bytes,
    *,
    port: int = PORT,
    connect_timeout: float = CONNECT_TIMEOUT,
    wait_timeout: float = WAIT_TIMEOUT,
    allow_local: bool = False,
) -> None:
    """Send one job to the printer; returns when the printer has closed the connection (it has read
    the job) or after `wait_timeout` seconds, as everything was handed over by then. Raises
    `PrinterAddressRefused`, `PrinterUnreachable` (no connection in time) or `SendAborted`."""
    address = resolve(host, port, allow_local=allow_local)
    try:
        connection = socket.create_connection((address, port), timeout=connect_timeout)
    except OSError:
        raise PrinterUnreachable() from None
    with connection:
        try:
            connection.settimeout(wait_timeout)
            connection.sendall(data)
        except OSError:
            raise SendAborted() from None
        deadline = time.monotonic() + wait_timeout  # one limit for the whole wait, not for each read
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return  # still busy printing: the job is with the printer, do not send it again
            try:
                connection.settimeout(remaining)
                if not connection.recv(4096):  # the printer sends nothing; it closes when it has the job
                    return
            except TimeoutError:
                return
            except OSError:
                raise SendAborted() from None  # reset: the printer did not close in the normal way
