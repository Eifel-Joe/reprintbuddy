"""Every way to render the settings page asks the system in a worker thread (v0.3.9 p122).

The installed version (`git describe`, once per process) and the question whether an update runs
(systemd) cost real time. The page asks both in a worker thread through `ops_ready`; before v0.3.9
only `GET /settings` did, so a first page of a run that was an error page of a save (or a label
page) asked them in the event loop and held the whole server for that moment.
"""

import ast
import asyncio
from pathlib import Path

import pytest
from conftest import csrf_of
from test_ops_update import git_env

import reprintbuddy.web.routes as routes_package

ROUTES = Path(routes_package.__file__).parent

# One refused save per route that renders the settings page again with its error.
REFUSED_SAVES = {
    "language": ("/settings/language", {"language": "klingon"}),
    "qr": ("/settings/qr", {"qr_base_url": "not an address"}),
    "bambuddy": ("/settings/bambuddy", {"bambuddy_address": ""}),
    "password": ("/settings/password", {"current_password": "wrong", "new_password": "x", "new_password_repeat": "y"}),
    "server": ("/settings/server", {"host": "0.0.0.0", "port": "not a number"}),
    "automation": ("/settings/automation", {"queue_cap": "x", "poll_minutes": "x"}),
    "labels": ("/settings/labels", {"label_host": "", "label_roll": "no such roll"}),
    "labels-test": ("/settings/labels/test", {"label_host": "", "label_roll": "no such roll"}),
}


@pytest.mark.parametrize(("path", "data"), REFUSED_SAVES.values(), ids=REFUSED_SAVES.keys())
def test_a_refused_save_asks_the_system_in_a_worker_thread(ops_env, monkeypatch, path, data):
    import reprintbuddy.web.routes.ops as ops_routes

    git_env(ops_env)
    token = csrf_of(ops_env.client, "/settings")  # this page warms the tag: forget it again
    app = ops_env.client.app
    del app.state.installed_tag

    where: list[str] = []

    def mark(what):
        try:
            asyncio.get_running_loop()
            where.append(f"{what}: event loop")
        except RuntimeError:
            where.append(f"{what}: worker thread")

    real_installed, real_running = ops_routes.installed_tag, ops_routes.update_running

    def installed(*args, **kwargs):
        mark("installed")
        return real_installed(*args, **kwargs)

    def running(*args, **kwargs):
        mark("running")
        return real_running(*args, **kwargs)

    monkeypatch.setattr(ops_routes, "installed_tag", installed)
    monkeypatch.setattr(ops_routes, "update_running", running)

    response = ops_env.client.post(path, data={"csrf_token": token, **data})
    assert response.status_code == 200, response.text[:300]  # the page again, with the error
    assert "installed: worker thread" in where
    assert not [entry for entry in where if entry.endswith("event loop")], where


def _calls_of_page(path: Path) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_page"
    ]


@pytest.mark.parametrize("module", ["settings.py", "labels.py"])
def test_every_call_that_renders_the_settings_page_hands_in_the_answer_of_ops_ready(module):
    """`_page` renders the settings; the callers (async) ask `ops_ready` and hand the answer in."""
    calls = _calls_of_page(ROUTES / module)
    if module == "labels.py":
        assert calls, "no call found: the check watches nothing"
    for call in calls:
        busy = next((kw.value for kw in call.keywords if kw.arg == "busy"), None)
        assert busy is not None, f"{module} line {call.lineno}: _page without busy="
        assert (
            isinstance(busy, ast.Await)
            and isinstance(busy.value, ast.Call)
            and getattr(busy.value.func, "id", None) == "ops_ready"
        ), f"{module} line {call.lineno}: busy= is not `await ops_ready(request)`"
