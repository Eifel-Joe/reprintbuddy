import re
import socket
from pathlib import Path
from typing import NamedTuple

import pytest
from fastapi.testclient import TestClient

from network_guard import NetworkGuard
from reprintbuddy import settings as st
from reprintbuddy.bambuddy.fake import FakeBambuddy
from reprintbuddy.bambuddy.types import FilamentRequirement
from reprintbuddy.db import make_engine, make_sessionmaker, upgrade
from reprintbuddy.web.app import create_app, shared_tls_client_factory

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


_FORM_TOKEN = re.compile(r'name="form_token" value="([^"]+)"')


def form_token_of(client: TestClient, path: str, action: str | None = None) -> str:
    """The hidden one-time token of the form on `path` whose action is `action`
    (None: the first form with a token)."""
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    html = response.text
    if action is not None:
        forms = re.findall(rf'<form[^>]*action="{re.escape(action)}"[^>]*>(.*?)</form>', html, re.S)
        assert forms, f"no form with action {action} on {path}"
        html = forms[0]
    match = _FORM_TOKEN.search(html)
    assert match, f"no form token on {path} ({action})"
    return match.group(1)


def sm_of(client: TestClient):
    """The application's sessionmaker (`with sm_of(client).begin() as s:` writes immediately)."""
    return client.app.state.sessionmaker


REQUIRED = (
    "library:read_all",
    "queue:read_all",
    "queue:create",
    "queue:start_unreviewed",
    "printers:read",
    "inventory:read",
    "filaments:read",
)


def auth_me(username="reprintbuddy", permissions=REQUIRED, groups=("Reprintbuddy",), **extra):
    """The body Bambuddy's /api/v1/auth/me answers with for a key."""
    body = {
        "id": 7,
        "username": username,
        "is_admin": False,
        "groups": [{"id": n + 1, "name": name} for n, name in enumerate(groups)],
        "permissions": list(permissions) if isinstance(permissions, (list, tuple)) else permissions,
    }
    body.update(extra)
    return body


def landed(response):
    """What a good check in set-up step 4 leads to (p179): the page that shows the result has a link
    "Continue", and where it leads is what the redirect said before. Anything else comes back as it is."""
    from types import SimpleNamespace

    if response.status_code != 200:
        return response
    found = re.search(r'<a class="btn" href="([^"]+)"[^>]*>\s*Continue\s*</a>', response.text)
    if not found:
        return response
    return SimpleNamespace(status_code=200, headers={"location": found.group(1)}, text=response.text)


def mock_auth_me(respx_mock, url="http://bambuddy.test:8000", status=200, **kwargs):
    """Make Bambuddy's /auth/me answer; returns the route."""
    route = respx_mock.get(f"{url}/api/v1/auth/me")
    if status == 200:
        return route.respond(200, json=auth_me(**kwargs))
    return route.respond(status)


# GET routes that answer with an endless stream: a walk over all GET routes skips them,
# or it would wait forever for the end of the response (tests/live_support.py drives them).
STREAM_PATHS = frozenset({"/live"})


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


# The production factory, built once at module level: one TLS context for the whole test session
# (httpx's default loads the CA certificates each time a client is built, most of a second), and
# every test client goes through the code that runs in production.
http_client_factory = shared_tls_client_factory()


def make_client(db_path, **create_kwargs) -> TestClient:
    # No background tick loop unless a test asks for it (`with client:` would start it).
    create_kwargs.setdefault("run_engine", False)
    # Never the system's install directory: with the engine on, the automatic check for new
    # versions would run a real `git ls-remote` wherever that is a git copy (a container, CI).
    from reprintbuddy.ops.context import OpsContext

    create_kwargs.setdefault("ops", OpsContext(install_dir=db_path.parent / "no-install"))
    create_kwargs.setdefault("http_client_factory", http_client_factory)
    app = create_app(db_path, **create_kwargs)
    app.state.login_failure_delay = 0  # tests do not wait; some tests set these themselves
    app.state.login_brake.delay = 0
    return TestClient(app, follow_redirects=False)


@pytest.fixture(autouse=True)
def fixed_display_zone(monkeypatch):
    """p183: times are shown in the zone of the server. The tests pin one, so that they read the same on a
    machine in any zone (CI runs in UTC); tests/web/test_server_time_zone.py sets the default back."""
    from zoneinfo import ZoneInfo

    from reprintbuddy.web import formatting

    monkeypatch.setattr(formatting, "LOCAL_ZONE", ZoneInfo("Europe/Berlin"))


@pytest.fixture(autouse=True)
def no_release_notes_request(monkeypatch):
    """p143: the release notes never come from the real GitHub. `with_notes` swallows every error, so a
    request that slipped through would go unnoticed; it is refused here and fails the test. Tests of the
    HTTP function itself hand in an httpx transport and pass."""
    from reprintbuddy.ops import releasenotes

    real = releasenotes._get
    asked: list[str] = []

    def guarded(url, headers, transport):
        if transport is None:
            asked.append(url)
            raise RuntimeError("no network in tests")
        return real(url, headers, transport)

    monkeypatch.setattr(releasenotes, "_get", guarded)
    yield
    if asked:
        pytest.fail(f"a test asked GitHub for release notes: {asked}", pytrace=False)


@pytest.fixture(autouse=True)
def no_test_host_lookup(monkeypatch):
    """Fail any test that looks up a `.test` host: that is a request no respx route caught."""
    guard = NetworkGuard()
    monkeypatch.setattr(socket, "getaddrinfo", guard.getaddrinfo)
    yield guard
    problem = guard.report()
    if problem:
        pytest.fail(problem, pytrace=False)


@pytest.fixture(autouse=True)
def no_machine_address(monkeypatch):
    """The tests never ask the real routing table: the machine counts as having no network
    address, so a QR code carries the address of the call. Tests of the search itself and of the
    codes' address say otherwise (tests/test_netaddr.py, tests/web/test_qr_address.py)."""
    from reprintbuddy.web import netaddr

    monkeypatch.setattr(netaddr, "machine_address", lambda: None)


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
    # Tests that are not about the rights must not ask a Bambuddy that is not there (Spec 12):
    # the key counts as checked. The rights tests set the state back to "unknown".
    client.app.state.rights.status = "ok"
    return client


@pytest.fixture
def admin_client(client):
    """First-run step 1 done: admin account exists and is logged in."""
    return finish_admin_setup(client)


@pytest.fixture
def ready_client(admin_client):
    finish_bambuddy_setup(admin_client)
    # The app would now read Bambuddy over HTTP; the tests that use `ready_client` mean
    # "no Bambuddy" and must never cause a network call (Spec 12). Tests with a Bambuddy
    # use `bb_client` (the fake) or respx.
    admin_client.app.state.bambuddy_factory = lambda: None
    return admin_client


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
    client.app.state.bambuddy_factory = lambda: None  # no Bambuddy, no network call (Spec 12)
    return OpsEnv(client, fake_runner, install_dir, db)


@pytest.fixture
def db(tmp_path):
    """A fresh migrated database; yields a sessionmaker (use `with db.begin() as s:`)."""
    engine = make_engine(tmp_path / "domain.db")
    upgrade(engine)
    yield make_sessionmaker(engine)
    engine.dispose()


@pytest.fixture
def fake_bambuddy():
    """Folder "Templates" (id 1) with Template A (1 plate) and Template B (2 plates), three printers."""
    fake = FakeBambuddy()
    folder = fake.add_folder("Templates")
    template_a = fake.add_file("Template A.gcode.3mf", folder.id, printer_model="A1", plates=1)
    fake.set_requirements(template_a.id, 1, [FilamentRequirement(1, 42.5, "PLA", "#000000")])
    fake.add_file("Template B.gcode.3mf", folder.id, printer_model="A1", plates=2)
    fake.add_printer("Printer 1", "A1")
    fake.add_printer("Printer 2", "A1")
    fake.add_printer("Printer 3", "P1S")
    return fake


@pytest.fixture
def bb_client(ready_client, fake_bambuddy):
    """A ready client whose Bambuddy is the in-memory fake."""
    ready_client.app.state.bambuddy_factory = lambda: fake_bambuddy
    return ready_client


class EngineEnv:
    """An engine on a fresh database with the fake Bambuddy (plan B4 Task 10).

    `updating` is the flag the engine's `update_running` reads; `article_id` is Template A.
    """

    def __init__(self, sm, fake: FakeBambuddy) -> None:
        from reprintbuddy.domain.engine import Engine

        self.sm = sm
        self.fake = fake
        self.updating = False
        self.engine = Engine(sm, lambda: fake, update_running=lambda: self.updating)
        self.article_id = 0


@pytest.fixture
def engine_env(db, fake_bambuddy):
    """Automation on; Template A (file 1, plate 1, any A1, minimum stock 1, buffer 0)."""
    from factories import make_article

    env = EngineEnv(db, fake_bambuddy)
    with db() as s:
        st.set_setting(s, "automation", True)
        # The owner of the key, as the connection check stores it: the search for an
        # unclear hand-over looks for entries of this user (Spec 7.6).
        st.set_setting(s, "bambuddy_username", fake_bambuddy.username)
    with db.begin() as s:
        env.article_id = make_article(
            s,
            name="Template A",
            library_file_id=1,
            plate_id=1,
            file_name="Template A.gcode.3mf",
            file_fingerprint="v1",
            target_model="A1",
            auto_enabled=True,
            min_stock=1,
            buffer=0,
        ).id
    return env


def run_tick(engine, poll: bool = False):
    """One tick from synchronous code (web tests): `asyncio.run(engine.tick(poll=poll))`."""
    import asyncio

    return asyncio.run(engine.tick(poll=poll))
