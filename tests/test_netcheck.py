import pytest

from reprintbuddy.netcheck import can_bind


@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0", "localhost"])
def test_local_addresses_can_be_bound(host):
    assert can_bind(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "",
        "   ",
        "256.1.1.1",  # not an address
        "no-such-host.invalid",  # does not resolve
        "203.0.113.77",  # documentation range: valid, but not an address of this machine
    ],
)
def test_other_hosts_cannot_be_bound(host):
    assert can_bind(host) is False


def _infos(*addresses):
    import socket

    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0)) for address in addresses]


def test_every_resolved_address_must_be_bindable(monkeypatch):
    """A name that resolves to several addresses is only usable if all of them are ours."""
    import socket

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _infos("127.0.0.1", "203.0.113.77"))
    assert can_bind("several.test") is False
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _infos("203.0.113.77", "127.0.0.1"))
    assert can_bind("several.test") is False  # the order does not matter


def test_several_local_addresses_are_fine(monkeypatch):
    import socket

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _infos("127.0.0.1", "0.0.0.0"))
    assert can_bind("several.test") is True


def test_an_empty_answer_is_not_bindable(monkeypatch):
    import socket

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [])
    assert can_bind("nothing.test") is False
