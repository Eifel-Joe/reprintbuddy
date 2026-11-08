"""Behind a proxy only an own address or an own port works, no sub-path (Spec 11.4; v0.3.11 p139).

The app builds absolute links and redirects from "/"; under a prefix they point past the proxy.
It is not supported, so it says so once in the log instead of failing in ways nobody can trace:
when the server hands over a `root_path`, or a proxy sends `X-Forwarded-Prefix`. The warning has a
fixed text: nothing from the request goes into the log.
"""

from __future__ import annotations

import logging

from reprintbuddy.web.subpath import SubPathWarning


def _scope(path="/healthz", root_path="", headers=()):
    return {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": path, "raw_path": path.encode(), "root_path": root_path, "query_string": b"",
        "headers": [(b"host", b"testserver"), *[(k.encode(), v.encode()) for k, v in headers]],
        "client": ("127.0.0.1", 1234), "server": ("testserver", 80),
    }


async def _get(app, **kwargs):
    sent = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app(_scope(**kwargs), receive, send)
    return [m["status"] for m in sent if m["type"] == "http.response.start"][0]


def _warnings(caplog):
    return [r for r in caplog.records if r.name == "reprintbuddy.web" and r.levelno == logging.WARNING]


async def test_a_root_path_gives_one_warning_for_the_whole_app(admin_client, caplog):
    caplog.set_level(logging.WARNING)
    app = admin_client.app
    await _get(app, path="/rb/healthz", root_path="/rb")  # the gate may redirect: the answer is not the point
    await _get(app, path="/rb/healthz", root_path="/rb")  # the gate may redirect: the answer is not the point
    records = _warnings(caplog)
    assert len(records) == 1
    text = records[0].getMessage()
    assert "sub-path" in text and "own address or port" in text
    assert "/rb" not in text  # nothing of the request in the log


async def test_the_forwarded_prefix_header_gives_a_warning_too(admin_client, caplog):
    caplog.set_level(logging.WARNING)
    app = admin_client.app
    await _get(app, headers=[("x-forwarded-prefix", "/evil\nforged log line")])
    await _get(app, headers=[("X-Forwarded-Prefix", "/other")])
    records = _warnings(caplog)
    assert len(records) == 1
    assert "evil" not in records[0].getMessage() and "forged" not in records[0].getMessage()


async def test_a_normal_request_gives_no_warning(admin_client, caplog):
    caplog.set_level(logging.WARNING)
    assert await _get(admin_client.app) == 200
    assert await _get(admin_client.app, headers=[("x-forwarded-for", "203.0.113.5")]) == 200
    assert _warnings(caplog) == []


async def test_the_warning_is_once_per_app_not_once_per_process(admin_client, caplog):
    caplog.set_level(logging.WARNING)
    calls = []

    async def inner(scope, receive, send):
        calls.append(1)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    for _ in range(2):  # two instances: each says it once
        await _get(SubPathWarning(inner), root_path="/rb", path="/rb/x")
    assert len(_warnings(caplog)) == 2
    assert len(calls) == 2


async def test_other_scopes_pass_through_without_a_warning(caplog):
    caplog.set_level(logging.WARNING)
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    scope = {"type": "lifespan", "root_path": "/rb"}
    await SubPathWarning(inner)(scope, None, None)
    assert seen == ["lifespan"]
    assert _warnings(caplog) == []
