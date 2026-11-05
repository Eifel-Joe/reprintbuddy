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
