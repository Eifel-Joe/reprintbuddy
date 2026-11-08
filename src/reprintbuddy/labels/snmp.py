"""One SNMP v1 GET, enough to ask a Brother printer for its status block (Spec 8.5).

The raw printer port answers nothing, so the status (which roll, cover, errors) comes over SNMP:
community "public", Brother's OID `1.3.6.1.4.1.2435.3.3.9.1.6.1.0`, UDP port 161. Written out here
(about fifty lines of BER) instead of pulling in an SNMP library for a single question.

The answer is read strictly: anything that is not exactly the awaited GetResponse (another request
id, an error, another value, cut off, longer than it says) counts as no answer. The whole exchange
takes at most `timeout` seconds (Spec 8.5: one second), and it never raises for a printer that is
silent or confused: the print then goes ahead.
"""

from __future__ import annotations

import ipaddress
import secrets
import socket
import time

from reprintbuddy.labels.addresses import resolve

PORT = 161
TIMEOUT = 1.0
COMMUNITY = b"public"
STATUS_OID = "1.3.6.1.4.1.2435.3.3.9.1.6.1.0"

_INTEGER, _OCTET_STRING, _NULL, _OID, _SEQUENCE = 0x02, 0x04, 0x05, 0x06, 0x30
_GET_REQUEST, _GET_RESPONSE = 0xA0, 0xA2


def _length(size: int) -> bytes:
    if size < 128:
        return bytes([size])
    raw = size.to_bytes((size.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _tlv(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + _length(len(body)) + body


def _integer(value: int) -> bytes:
    return _tlv(_INTEGER, value.to_bytes(max(1, (value.bit_length() + 8) // 8), "big", signed=True))


def _oid(text: str) -> bytes:
    arcs = [int(part) for part in text.split(".")]
    body = bytearray([arcs[0] * 40 + arcs[1]])
    for arc in arcs[2:]:
        chunk = [arc & 0x7F]
        arc >>= 7
        while arc:
            chunk.append(0x80 | (arc & 0x7F))
            arc >>= 7
        body.extend(reversed(chunk))
    return _tlv(_OID, bytes(body))


def build_get(oid: str, *, request_id: int) -> bytes:
    """The SNMP v1 GetRequest for one OID."""
    varbind = _tlv(_SEQUENCE, _oid(oid) + _tlv(_NULL, b""))
    pdu = _tlv(_GET_REQUEST, _integer(request_id) + _integer(0) + _integer(0) + _tlv(_SEQUENCE, varbind))
    return _tlv(_SEQUENCE, _integer(0) + _tlv(_OCTET_STRING, COMMUNITY) + pdu)


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data, self.at = data, 0

    def tlv(self) -> tuple[int, bytes]:
        if self.at + 2 > len(self.data):
            raise ValueError("cut off")
        tag, first = self.data[self.at], self.data[self.at + 1]
        self.at += 2
        if first & 0x80:
            count = first & 0x7F
            if count == 0 or count > 3 or self.at + count > len(self.data):
                raise ValueError("bad length")
            size = int.from_bytes(self.data[self.at : self.at + count], "big")
            self.at += count
        else:
            size = first
        if self.at + size > len(self.data):
            raise ValueError("cut off")
        body = self.data[self.at : self.at + size]
        self.at += size
        return tag, body

    def done(self) -> bool:
        return self.at == len(self.data)


def _expect(reader: _Reader, tag: int) -> bytes:
    found, body = reader.tlv()
    if found != tag:
        raise ValueError("unexpected tag")
    return body


def _int(body: bytes) -> int:
    """An INTEGER's value; an empty one is not an integer."""
    if not body:
        raise ValueError("empty integer")
    return int.from_bytes(body, "big", signed=True)


def parse_response(data: bytes, oid: str, request_id: int) -> bytes | None:
    """The octet string a printer answered for `oid` to our request `request_id`, or None for
    anything else."""
    try:
        outer = _Reader(data)
        message = _Reader(_expect(outer, _SEQUENCE))
        if not outer.done():
            return None
        if _int(_expect(message, _INTEGER)) != 0:  # SNMP v1
            return None
        if _expect(message, _OCTET_STRING) != COMMUNITY:
            return None
        pdu = _Reader(_expect(message, _GET_RESPONSE))
        if not message.done():
            return None
        if _int(_expect(pdu, _INTEGER)) != request_id:
            return None
        if _int(_expect(pdu, _INTEGER)) != 0:  # error status
            return None
        if _int(_expect(pdu, _INTEGER)) != 0:  # error index
            return None
        bindings = _Reader(_expect(pdu, _SEQUENCE))
        if not pdu.done():
            return None
        binding = _Reader(_expect(bindings, _SEQUENCE))
        if not bindings.done():
            return None
        if _expect(binding, _OID) != _oid(oid)[2:]:
            return None
        value = _expect(binding, _OCTET_STRING)
        return value if binding.done() else None
    except ValueError:
        return None


def query_status(host: str, *, port: int = PORT, timeout: float = TIMEOUT, allow_local: bool = False) -> bytes | None:
    """Brother's status block (32 bytes) from the printer at `host`, or None: no answer in time,
    a broken or foreign answer, anything that goes wrong on the way. Raises only
    `PrinterAddressRefused` (this machine is no printer)."""
    address = resolve(host, port, allow_local=allow_local)
    request_id = secrets.randbelow(0x7FFFFFFF - 1) + 1
    message = build_get(STATUS_OID, request_id=request_id)
    family = socket.AF_INET6 if ipaddress.ip_address(address.split("%", 1)[0]).version == 6 else socket.AF_INET
    deadline = time.monotonic() + timeout
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.sendto(message, (address, port))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                sock.settimeout(remaining)
                try:
                    data, peer = sock.recvfrom(2048)
                except (TimeoutError, OSError):
                    return None
                if peer[0].split("%", 1)[0] != address.split("%", 1)[0] or peer[1] != port:
                    continue  # not from the printer we asked, nor from its SNMP port
                value = parse_response(data, STATUS_OID, request_id)
                if value is not None:
                    return value
    except OSError:
        return None
