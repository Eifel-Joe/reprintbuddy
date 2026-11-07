"""The HTTP client factory of the app: one TLS context per start, one client per call.

Building an `httpx.AsyncClient` with httpx's default loads the CA certificates each time (most of
a second, more under load) and blocks the event loop. The app builds that context once when it
starts and hands it to every client. No test here sends a request.
"""

import ssl

import httpx

from reprintbuddy.web.app import create_app


def _count(monkeypatch, owner, name):
    """Wrap `owner.name` so every call is recorded; returns the list of results."""
    original = getattr(owner, name)
    results = []

    def counting(*args, **kwargs):
        result = original(*args, **kwargs)
        results.append(result)
        return result

    monkeypatch.setattr(owner, name, counting)
    return results


async def test_ssl_context_built_once(db_path, monkeypatch):
    built = _count(monkeypatch, httpx, "create_ssl_context")  # as `app.py` calls it
    loaded = _count(monkeypatch, ssl, "create_default_context")  # where httpx loads certificates
    app = create_app(db_path)
    assert len(built) == 1 and len(loaded) == 1  # at start, before any call
    shared = built[0]

    clients = [app.state.http_client_factory(), app.state.http_client_factory()]
    for client in clients:
        async with client:
            pass

    assert clients[0] is not clients[1]  # still one client per call (Decision 4)
    assert len(built) == 1
    assert len(loaded) == 1  # httpx built no context of its own for either client
    for client in clients:
        assert client._transport._pool._ssl_context is shared


async def test_factory_returns_async_client(db_path):
    app = create_app(db_path)
    client = app.state.http_client_factory()
    assert isinstance(client, httpx.AsyncClient)
    async with client as entered:
        assert entered is client
    assert client.is_closed


def test_given_factory_is_kept(db_path):
    def factory():
        return httpx.AsyncClient()

    app = create_app(db_path, http_client_factory=factory)
    assert app.state.http_client_factory is factory
