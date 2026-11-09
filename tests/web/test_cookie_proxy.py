"""p177: behind a proxy that does HTTPS, the cookies are marked Secure too.

Uvicorn's proxy layer takes `X-Forwarded-Proto` only from a proxy on the same machine (127.0.0.1, ::1) and
then sets the request's scheme to https; the cookies follow the scheme. A header from anyone else changes
nothing: a device in the network could otherwise claim HTTPS for a plain connection.
"""

from __future__ import annotations

import uvicorn
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from conftest import make_client
from reprintbuddy import cli
from reprintbuddy.web.app import SESSION_COOKIE

FORWARDED_HTTPS = {"X-Forwarded-Proto": "https", "X-Forwarded-For": "192.0.2.7"}


def session_cookie_of(response) -> str:
    cookies = [c for c in response.headers.get_list("set-cookie") if c.startswith(f"{SESSION_COOKIE}=")]
    assert cookies, response.headers
    return cookies[0].lower()


def behind_proxy(db_path, proxy_address: str) -> TestClient:
    """The app as `serve` runs it: inside uvicorn's proxy layer, which trusts the same machine only."""
    app = make_client(db_path).app
    wrapped = ProxyHeadersMiddleware(app, trusted_hosts=cli.TRUSTED_PROXIES)
    return TestClient(wrapped, follow_redirects=False, client=(proxy_address, 50000))


def test_a_request_over_https_gets_a_secure_session_cookie(db_path):
    client = make_client(db_path)
    client.base_url = client.base_url.copy_with(scheme="https")
    assert "; secure" in session_cookie_of(client.get("https://testserver/setup"))


def test_a_proxy_on_the_same_machine_that_did_https_makes_the_cookie_secure(db_path):
    client = behind_proxy(db_path, "127.0.0.1")
    assert "; secure" in session_cookie_of(client.get("/setup", headers=FORWARDED_HTTPS))


def test_the_same_header_from_another_device_changes_nothing(db_path):
    client = behind_proxy(db_path, "192.0.2.50")
    assert "secure" not in session_cookie_of(client.get("/setup", headers=FORWARDED_HTTPS))


def test_plain_http_in_the_network_keeps_a_cookie_without_secure(db_path):
    # Sign-in over plain HTTP in the LAN must go on working: a Secure cookie would never come back.
    client = behind_proxy(db_path, "192.0.2.50")
    assert "secure" not in session_cookie_of(client.get("/setup"))


def test_serve_trusts_forwarded_headers_of_the_same_machine_only(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")  # an environment may not widen it
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(kw))
    assert cli.main(["serve", "--db", str(tmp_path / "t.db")]) == 0
    (kwargs,) = calls
    assert kwargs["proxy_headers"] is True
    assert kwargs["forwarded_allow_ips"] == ["127.0.0.1", "::1"]
