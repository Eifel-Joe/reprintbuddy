import re
from pathlib import Path
from typing import NamedTuple

import pytest
from fastapi.testclient import TestClient

from reprintbuddy import settings as st
from reprintbuddy.web.app import create_app

ADMIN = "admin"
PASSWORD = "password123"

_TOKEN = re.compile(r'name="csrf_token" value="([^"]+)"')


def csrf_of(client: TestClient, path: str) -> str:
    """Fetch a page and return the CSRF token from its form."""
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    match = _TOKEN.search(response.text)
    assert match, f"no csrf token on {path}"
    return match.group(1)


def api_routes(app):
    """All path operations, also those hidden in included routers (FastAPI >= 0.16x)."""
    from fastapi.routing import APIRoute

    def walk(routes):
        for route in routes:
            nested = getattr(route, "original_router", None)
            if nested is not None:
                yield from walk(nested.routes)
            elif isinstance(route, APIRoute):
                yield route

    return list(walk(app.routes))


def make_client(db_path, **create_kwargs) -> TestClient:
    app = create_app(db_path, **create_kwargs)
    app.state.login_failure_delay = 0  # tests do not wait; some tests set these themselves
    app.state.login_brake.delay = 0
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "t.db"


@pytest.fixture
def client(db_path):
    return make_client(db_path)


def finish_admin_setup(client):
    """First-run step 1: admin account created and logged in."""
    token = csrf_of(client, "/setup")
    response = client.post(
        "/setup",
        data={
            "csrf_token": token,
            "username": ADMIN,
            "password": PASSWORD,
            "password_repeat": PASSWORD,
            "language": "en",
        },
    )
    assert response.status_code == 303
    return client


def finish_bambuddy_setup(client):
    """Step 2 done as well: Bambuddy settings written straight into the DB."""
    with client.app.state.sessionmaker() as session:
        st.set_setting(session, "bambuddy_url", "http://bambuddy.test:8000")
        st.set_setting(session, "bambuddy_username", "reprintbuddy")
        st.set_setting(session, "bambuddy_api_key", "bb_testkey")
        st.set_setting(session, "bambuddy_review_confirmed", True)
    return client


@pytest.fixture
def admin_client(client):
    """First-run step 1 done: admin account exists and is logged in."""
    return finish_admin_setup(client)


@pytest.fixture
def ready_client(admin_client):
    return finish_bambuddy_setup(admin_client)


class FakeRunner:
    """Stands in for the command runner: records every call, answers from a script.

    `on(prefix, *results)` answers calls that start with `prefix`: the results in order,
    the last one repeats. A result may be a function that receives the call (to check
    the state of the world in the middle of a run) and returns a CommandResult.
    The longest matching prefix wins; without a match the call succeeds.
    """

    def __init__(self):
        self.calls: list[tuple[str, ...]] = []
        self._rules: list[tuple[tuple[str, ...], list, list[int]]] = []

    def on(self, prefix, *results):
        self._rules.append((tuple(prefix), list(results), [0]))

    def __call__(self, args, *, cwd=None, timeout=120.0):
        from reprintbuddy.ops.runner import CommandResult

        call = tuple(args)
        self.calls.append(call)
        best = None
        for prefix, results, position in self._rules:
            if call[: len(prefix)] == prefix and (best is None or len(prefix) >= len(best[0])):
                best = (prefix, results, position)
        if best is None:
            return CommandResult(0)
        _, results, position = best
        result = results[min(position[0], len(results) - 1)]
        position[0] += 1
        return result(call) if callable(result) else result


def make_tls_pair(name: str = "localhost") -> tuple[str, str]:
    """A certificate and the matching private key (PEM text) for tests."""
    import trustme

    leaf = trustme.CA().issue_cert(name)
    return leaf.cert_chain_pems[0].bytes().decode(), leaf.private_key_pem.bytes().decode()


@pytest.fixture
def tls_pair():
    return make_tls_pair()


@pytest.fixture
def fake_runner():
    return FakeRunner()


class OpsEnv(NamedTuple):
    client: TestClient
    runner: "FakeRunner"
    install_dir: Path
    db_path: Path


@pytest.fixture
def ops_env(tmp_path, fake_runner):
    """Like `ready_client`, but the app runs with a fake command runner and a fake install dir."""
    from reprintbuddy.ops.context import OpsContext
    from reprintbuddy.ops.server import EffectiveServer

    db = tmp_path / "ops.db"
    install_dir = tmp_path / "opt"
    ops = OpsContext(
        install_dir=install_dir,
        runner=fake_runner,
        effective=EffectiveServer("0.0.0.0", 8080, None),
    )
    client = make_client(db, ops=ops)
    finish_bambuddy_setup(finish_admin_setup(client))
    return OpsEnv(client, fake_runner, install_dir, db)
