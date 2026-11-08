"""A file that is too large for its route gets the page with a message, not a bare 413 (v0.3.10 p132).

Since v0.3.9 `BodyLimitMiddleware` bounds every body. A route that takes a file has a limit of its own and
checks the declared length first (`upload_length_ok`), so the person sees a page. `/settings/server` (the
HTTPS certificate and key) had no such check: its CSRF check ran as a dependency before the route and read
the form, so a certificate over 1 MiB got the middleware's text/plain 413.

Covered here: the HTTPS certificate and key and the backup restore; the print file route is covered in
test_article_upload.py (a declared length over its limit).
What no page can fix: a browser that is still sending a large body may stop at the early answer and show
its own error; the server answers without reading and closes the connection.
"""

from __future__ import annotations

import html
import re
import socket
import threading
import time

import pytest
import uvicorn
from conftest import csrf_of

from reprintbuddy import settings as st
from reprintbuddy.ops import fullbackup as fb
from reprintbuddy.web import bodylimit
from reprintbuddy.web.i18n import translate

TOO_BIG = 2 * 1024 * 1024  # over the 1 MiB of every other form


def en(key, **params):
    return translate("en", key, **params)


def text_of(response) -> str:
    return html.unescape(response.text)


def saved(client, key):
    with client.app.state.sessionmaker() as s:
        return st.get_setting(s, key)


def server_post(client, **files):
    token = csrf_of(client, "/settings")
    return client.post(
        "/settings/server",
        data={"csrf_token": token, "host": "0.0.0.0", "port": "8443"},
        files=files,
    )


# ---- HTTPS certificate and key ---------------------------------------------------------------


@pytest.mark.parametrize("field", ["tls_cert_file", "tls_key_file"])
def test_an_oversized_https_file_gets_the_settings_page_with_a_message(ready_client, field):
    response = server_post(ready_client, **{field: ("big.pem", b"x" * TOO_BIG, "application/x-pem-file")})
    assert response.status_code == 413
    assert "text/html" in response.headers["content-type"]
    assert en("settings.invalid_pem") in text_of(response)
    assert response.headers.get("connection") == "close"  # nothing of the body was read
    assert saved(ready_client, "port") != 8443  # nothing was saved
    assert saved(ready_client, "tls_cert") is None


def test_an_oversized_https_text_field_gets_the_same_page(ready_client):
    token = csrf_of(ready_client, "/settings")
    response = ready_client.post(
        "/settings/server", data={"csrf_token": token, "host": "0.0.0.0", "port": "8443", "tls_cert": "x" * TOO_BIG}
    )
    assert response.status_code == 413
    assert en("settings.invalid_pem") in text_of(response)


def test_the_page_shows_the_server_card(ready_client):
    response = server_post(ready_client, tls_cert_file=("big.pem", b"x" * TOO_BIG, "application/x-pem-file"))
    assert 'action="/settings/server"' in response.text  # the general tab, where the message sits


def test_a_file_over_64_kib_but_inside_the_body_limit_is_still_refused_by_the_route(ready_client):
    response = server_post(ready_client, tls_cert_file=("c.pem", b"x" * (100 * 1024), "application/x-pem-file"))
    assert response.status_code == 200
    assert en("settings.invalid_pem") in text_of(response)


def test_a_signed_out_visitor_never_gets_the_settings_page(ready_client):
    ready_client.cookies.clear()
    response = ready_client.post(
        "/settings/server",
        data={"host": "0.0.0.0", "port": "8443"},
        files={"tls_cert_file": ("big.pem", b"x" * TOO_BIG, "application/x-pem-file")},
        follow_redirects=False,
    )
    assert response.status_code in (303, 401, 403)
    assert en("settings.invalid_pem") not in text_of(response)


def test_a_wrong_token_on_a_small_form_is_still_403_and_saves_nothing(ready_client):
    csrf_of(ready_client, "/settings")
    response = ready_client.post("/settings/server", data={"csrf_token": "forged", "host": "0.0.0.0", "port": "8443"})
    assert response.status_code == 403
    assert saved(ready_client, "port") != 8443


def test_a_wrong_token_with_a_file_is_403_and_saves_nothing(ready_client):
    csrf_of(ready_client, "/settings")
    response = ready_client.post(
        "/settings/server",
        data={"csrf_token": "forged", "host": "0.0.0.0", "port": "8443"},
        files={"tls_cert_file": ("c.pem", b"x" * 1000, "application/x-pem-file")},
    )
    assert response.status_code == 403
    assert saved(ready_client, "port") != 8443


def test_the_route_has_a_limit_of_its_own_in_the_list(ready_client):
    limit = ready_client.app.state.body_limits["/settings/server"]
    assert 2 * 64 * 1024 < limit < bodylimit.DEFAULT_LIMIT


# ---- the sister routes: backup restore and print file ------------------------------------------


def test_an_oversized_backup_gets_a_page_with_the_message(ready_client, monkeypatch):
    monkeypatch.setattr(fb, "MAX_UPLOAD_BYTES", 1000)
    token = csrf_of(ready_client, "/settings")
    response = ready_client.post(
        "/settings/restore/upload",
        data={"csrf_token": token},
        files={"backup_file": ("b.zip", b"x" * (bodylimit.MULTIPART_OVERHEAD + 5000), "application/zip")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers.get("connection") == "close"
    page = ready_client.get(response.headers["location"])
    assert page.status_code == 200
    assert en("ops.msg.restore_too_large") in text_of(page)


# ---- through a real server: the answer comes without waiting for the body --------------------


@pytest.fixture
def port(ready_client):
    config = uvicorn.Config(ready_client.app, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not srv.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert srv.started
    yield srv.servers[0].sockets[0].getsockname()[1]
    srv.should_exit = True
    thread.join(timeout=10)


def _answer_without_sending_the_body(port: int, path: str, cookie: str, length: int):
    head = (
        f"POST {path} HTTP/1.1\r\nHost: x\r\nCookie: {cookie}\r\n"
        f"Content-Type: multipart/form-data; boundary=b\r\nContent-Length: {length}\r\n\r\n"
    ).encode()
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(head)  # and not one byte of the body
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    status = int(data.split(b" ", 2)[1])
    return status, data


def test_the_answer_for_an_oversized_certificate_does_not_wait_for_the_body(ready_client, port):
    csrf_of(ready_client, "/settings")
    cookie = "; ".join(f"{name}={value}" for name, value in ready_client.cookies.items())
    status, data = _answer_without_sending_the_body(port, "/settings/server", cookie, 50 * 1024 * 1024)
    assert status == 413
    assert html.unescape(data.decode("utf-8", "replace")).count(en("settings.invalid_pem")) == 1
    # The page, not the bare 413: only the body counts. The status line carries the reason phrase,
    # which Python 3.12 calls "Request Entity Too Large" and 3.13 "Content Too Large".
    body = data.split(b"\r\n\r\n", 1)[1]
    assert b"Too Large" not in body
    assert b"<html" in body


def test_the_answer_for_an_oversized_backup_does_not_wait_for_the_body(ready_client, port):
    csrf_of(ready_client, "/settings")
    cookie = "; ".join(f"{name}={value}" for name, value in ready_client.cookies.items())
    status, data = _answer_without_sending_the_body(port, "/settings/restore/upload", cookie, 500 * 1024 * 1024)
    assert status == 303
    assert re.search(rb"(?i)\r\nlocation: /settings", data)
