"""How `reprintbuddy serve` runs uvicorn (web/server.py, p535 v0.4.8).

The server closes the live channel before it waits for the open requests, so open pages end their
stream by themselves instead of being cancelled after the grace (tests/web/test_live_shutdown.py).
Everything else is as with `uvicorn.run`: the options reach uvicorn unchanged, a server that could not
start ends the process with uvicorn's code 3, Ctrl+C ends it quietly.
"""

import asyncio
from types import SimpleNamespace

import pytest
import uvicorn

from reprintbuddy.web import server as web_server
from reprintbuddy.web.live import LiveBroker
from reprintbuddy.web.server import LiveClosingServer, serve


def _app():
    return SimpleNamespace(state=SimpleNamespace(live=LiveBroker()))


def _fake_run(monkeypatch, *, started=True, raises=None):
    seen = []

    def run(self, sockets=None):
        seen.append(self)
        self.started = started
        if raises is not None:
            raise raises

    monkeypatch.setattr(LiveClosingServer, "run", run)
    return seen


def test_serve_hands_the_options_to_uvicorn_and_the_live_channel_to_the_server(monkeypatch):
    app = _app()
    seen = _fake_run(monkeypatch)
    serve(
        app, host="0.0.0.0", port=8443, ssl_certfile="c.pem", ssl_keyfile="k.pem",
        timeout_graceful_shutdown=3, proxy_headers=True, forwarded_allow_ips=["127.0.0.1", "::1"],
    )
    (server,) = seen
    assert isinstance(server, LiveClosingServer)
    config = server.config
    assert config.app is app
    assert (config.host, config.port) == ("0.0.0.0", 8443)
    assert (config.ssl_certfile, config.ssl_keyfile) == ("c.pem", "k.pem")
    assert config.timeout_graceful_shutdown == 3
    assert config.proxy_headers is True and config.forwarded_allow_ips == ["127.0.0.1", "::1"]
    assert server.live is app.state.live


def test_serve_ends_with_uvicorns_code_when_the_server_did_not_start(monkeypatch):
    _fake_run(monkeypatch, started=False)
    with pytest.raises(SystemExit) as ended:
        serve(_app(), host="127.0.0.1", port=8080)
    assert ended.value.code == 3


def test_ctrl_c_ends_serve_quietly(monkeypatch):
    _fake_run(monkeypatch, raises=KeyboardInterrupt())
    serve(_app(), host="127.0.0.1", port=8080)


def test_the_live_channel_is_closed_before_uvicorn_waits_for_the_requests(monkeypatch):
    app = _app()
    closed_when_uvicorn_stops = []

    async def uvicorn_shutdown(self, sockets=None):
        closed_when_uvicorn_stops.append(app.state.live.closed)

    monkeypatch.setattr(uvicorn.Server, "shutdown", uvicorn_shutdown)
    server = LiveClosingServer(uvicorn.Config(app), live=app.state.live)
    asyncio.run(server.shutdown())
    assert closed_when_uvicorn_stops == [True]


def test_the_cli_starts_through_serve():
    """Guard for the tests that replace `web_server.serve`: the command line must call it there."""
    import inspect

    from reprintbuddy import cli

    source = inspect.getsource(cli._serve)
    assert "web_server.serve(" in source and "uvicorn.run(" not in source
    assert web_server.serve is serve
