"""The QR code of an article (Spec 8.4): its address and the picture of the code.

The code carries one short, fixed address, `<base>/a/<id>`, and nothing else. What it opens is
decided when it is scanned (`routes/qr.py`), so a printed code never goes stale. Made with segno
(BSD-3-Clause, pure Python, no network): the picture is drawn here, never by a service.
"""

from __future__ import annotations

import io
import ipaddress
from urllib.parse import urlsplit

import segno
from fastapi import Request
from sqlalchemy.orm import Session

from reprintbuddy import settings as st

# Error correction M (about 15 % can be damaged): a code on a shelf gets scratched and dusty, and
# the short address keeps it a small, coarse pattern anyway. Quiet zone of 4 modules is the standard.
_ERROR_LEVEL = "m"
_BORDER = 4
_WHITE = "#ffffff"
_BLACK = "#000000"
_PNG_SCALE = 10  # pixels per module: a version 3 code is about 370 pixels wide
_SVG_SCALE = 8


def base_address(request: Request, db: Session) -> str:
    """The start of the address in the codes: the address entered in the settings, else the one this
    very request came in on (no trailing slash)."""
    entered = st.get_setting(db, "qr_base_url")
    return str(entered) if entered else str(request.base_url).rstrip("/")


def article_address(base: str, article_id: int) -> str:
    """The address inside the code of an article; `base` has no trailing slash."""
    return f"{base.rstrip('/')}/a/{article_id}"


def _code(address: str) -> segno.QRCode:
    return segno.make(address, error=_ERROR_LEVEL, micro=False)


def svg(address: str) -> bytes:
    """The code as an SVG picture (dark on white, so it prints the same on any page colour)."""
    buffer = io.BytesIO()
    _code(address).save(
        buffer, kind="svg", scale=_SVG_SCALE, border=_BORDER, dark=_BLACK, light=_WHITE,
        xmldecl=False, svgns=True, nl=False,
    )
    return buffer.getvalue()


def png(address: str) -> bytes:
    """The code as a PNG picture, dark on white."""
    buffer = io.BytesIO()
    _code(address).save(buffer, kind="png", scale=_PNG_SCALE, border=_BORDER, dark=_BLACK, light=_WHITE)
    return buffer.getvalue()


def matrix(address: str) -> tuple[tuple[int, ...], ...]:
    """The modules of the code (1 = dark), for tests that check what a code says."""
    return tuple(tuple(row) for row in _code(address).matrix)


def is_local_address(base: str) -> bool:
    """Does `base` name this very machine (`localhost`, 127.x, ::1)? A phone cannot open that."""
    try:
        host = (urlsplit(base).hostname or "").lower()
    except ValueError:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
