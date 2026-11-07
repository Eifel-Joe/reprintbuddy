"""The guard against lookups of test hosts proves itself (Spec 12)."""

import socket

import pytest

from network_guard import NetworkGuard


def test_guard_refuses_and_records_a_test_host():
    guard = NetworkGuard(real=lambda *a, **k: pytest.fail("must not reach the resolver"))
    with pytest.raises(socket.gaierror):
        guard.getaddrinfo("bambuddy.test", 8000)
    with pytest.raises(socket.gaierror):
        guard.getaddrinfo("Spoolman.TEST.", 80)
    assert "bambuddy.test" in guard.report()


def test_guard_passes_other_hosts_through():
    guard = NetworkGuard(real=lambda host, *a, **k: [("real", host)])
    assert guard.getaddrinfo("localhost", 80) == [("real", "localhost")]
    assert guard.report() is None


def test_autouse_guard_is_installed_and_reports(no_test_host_lookup):
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("bambuddy.test", 8000)
    assert no_test_host_lookup.leaks == ["bambuddy.test"]
    no_test_host_lookup.leaks.clear()  # proven; do not fail this test at teardown
