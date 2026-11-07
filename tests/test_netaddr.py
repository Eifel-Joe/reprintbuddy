"""The server's own network address for the QR codes (Spec 8.4), found without any network traffic."""

import socket

import pytest

from reprintbuddy.web import netaddr

# conftest replaces `netaddr.machine_address` in every test; this file tests the real one.
machine_address = netaddr.machine_address


class FakeUdp:
    """A UDP socket that only knows what the routing table would answer; sending is a failure."""

    log: list[tuple] = []
    routes: dict[str, str | OSError] = {}

    def __init__(self, family, kind, *args):
        assert (family, kind) == (socket.AF_INET, socket.SOCK_DGRAM)
        self._target = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def connect(self, address):
        FakeUdp.log.append(("connect", address[0]))
        answer = FakeUdp.routes.get(address[0], OSError("Network is unreachable"))
        if isinstance(answer, OSError):
            raise answer
        self._target = answer

    def getsockname(self):
        FakeUdp.log.append(("getsockname",))
        return (self._target, 54321)

    def send(self, *args):
        raise AssertionError("the address search must not send anything")

    sendto = send
    sendall = send


@pytest.fixture
def udp(monkeypatch):
    FakeUdp.log = []
    FakeUdp.routes = {}
    monkeypatch.setattr(netaddr.socket, "socket", FakeUdp)
    return FakeUdp


# ---- which addresses are usable ----


@pytest.mark.parametrize("text", ["203.0.113.64", "198.51.100.7", "100.64.1.1", "8.8.4.4", "fd12:3456::1", "2001:db8::1"])
def test_a_network_address_is_usable(text):
    assert netaddr.usable(text) == text


@pytest.mark.parametrize(
    "text",
    ["127.0.0.1", "127.0.1.1", "::1", "0.0.0.0", "::", "169.254.3.4", "fe80::1", "224.0.0.1", "255.255.255.255",
     "", "testserver", "not an address", "203.0.113.64%eth0x"],
)
def test_loopback_wildcard_link_local_and_names_are_not_usable(text):
    assert netaddr.usable(text) is None


def test_an_ipv6_zone_is_dropped_or_refused_never_kept():
    assert netaddr.usable("fd12:3456::1%eth0") in (None, "fd12:3456::1")


# ---- the address the connection came in on ----


def test_the_local_address_of_the_connection_comes_first():
    assert netaddr.connection_address(("203.0.113.64", 8080)) == "203.0.113.64"


@pytest.mark.parametrize("server", [None, ("testserver", 80), ("127.0.0.1", 8080), ("::1", 8080), ("0.0.0.0", 1), "x", ()])
def test_a_connection_without_a_usable_address_gives_none(server):
    assert netaddr.connection_address(server) is None


# ---- the route the machine would take ----


def test_the_default_route_gives_the_address_without_sending_anything(udp):
    udp.routes["192.0.2.1"] = "203.0.113.64"
    assert machine_address() == "203.0.113.64"
    assert [entry[0] for entry in udp.log] == ["connect", "getsockname"]


def test_without_a_default_route_the_private_networks_are_tried(udp):
    udp.routes[netaddr._PRIVATE_PROBES[0]] = "203.0.113.64"
    assert machine_address() == "203.0.113.64"
    assert [entry for entry in udp.log if entry[0] == "connect"][0] == ("connect", "192.0.2.1")


def test_a_route_that_ends_on_loopback_does_not_count(udp):
    udp.routes["192.0.2.1"] = "127.0.0.1"
    udp.routes[netaddr._PRIVATE_PROBES[1]] = "198.51.100.3"
    assert machine_address() == "198.51.100.3"


def test_a_machine_without_network_gives_none(udp):
    assert machine_address() is None


def test_a_failing_socket_gives_none_never_an_error(monkeypatch):
    def broken(*args):
        raise OSError("no sockets")

    monkeypatch.setattr(netaddr.socket, "socket", broken)
    assert machine_address() is None
