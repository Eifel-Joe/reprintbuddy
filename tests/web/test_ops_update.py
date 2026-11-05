import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import api_routes, csrf_of

from reprintbuddy import settings as st
from reprintbuddy.ops.runner import CommandResult
from reprintbuddy.ops.service import START_UPDATE_CMD
from reprintbuddy.ops.status import UPDATE_STEPS, UpdateState, UpdateStatus, read_status, write_status
from reprintbuddy.web import i18n

LS_REMOTE = ("git", "-C")  # prefix; the directory differs per test
REMOTE_TWO = CommandResult(0, "abc\trefs/tags/v0.1.0\ndef\trefs/tags/v0.2.0\n")
REMOTE_ONE = CommandResult(0, "abc\trefs/tags/v0.1.0\n")


def git_env(env, *, describe="v0.1.0\n", remote=REMOTE_TWO):
    """Make the fake install dir a git installation and script git's answers."""
    (env.install_dir / ".git").mkdir(parents=True, exist_ok=True)
    d = str(env.install_dir)
    env.runner.on(("git", "-C", d, "describe"), CommandResult(0, describe) if describe else CommandResult(128))
    env.runner.on(("git", "-C", d, "ls-remote"), remote)
    return env


def post(env, path, token=None, **data):
    token = token or csrf_of(env.client, "/settings")
    return env.client.post(path, data={"csrf_token": token, **data})


def check(env):
    return post(env, "/settings/update/check")


def data_dir(env):
    return env.db_path.parent


def now():
    return datetime.now(UTC)


def calls_with(env, word):
    return [c for c in env.runner.calls if word in c]


def test_update_unavailable_without_git(ops_env):
    page = ops_env.client.get("/settings").text
    assert "need an installation made with the install script" in page
    assert 'action="/settings/update"' not in page
    response = check(ops_env)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?ops=not_git#ops-update"
    assert ops_env.runner.calls == []


def test_settings_shows_installed(ops_env):
    git_env(ops_env)
    page = ops_env.client.get("/settings").text
    assert "Installed version: v0.1.0" in page
    assert "Not checked yet." in page


def test_dev_build_shown(ops_env):
    git_env(ops_env, describe=None)
    assert "development build 0.1.0.dev0" in ops_env.client.get("/settings").text


def test_installed_tag_is_read_once_per_process(ops_env):
    git_env(ops_env)
    for _ in range(3):
        ops_env.client.get("/settings")
    assert len(calls_with(ops_env, "describe")) == 1


def test_check_shows_newest(ops_env):
    git_env(ops_env)
    response = check(ops_env)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings#ops-update"
    page = ops_env.client.get("/settings").text
    assert "Newest release: v0.2.0" in page
    assert "Update to v0.2.0" in page


def test_check_up_to_date(ops_env):
    git_env(ops_env, remote=REMOTE_ONE)
    check(ops_env)
    page = ops_env.client.get("/settings").text
    assert "Reprintbuddy is up to date." in page
    assert "Update to" not in page


def test_check_failure_message(ops_env):
    git_env(ops_env, remote=CommandResult(128, "", "fatal: unable to access 'https://example.invalid/x'"))
    response = check(ops_env)
    assert response.headers["location"] == "/settings?ops=check_failed#ops-update"
    page = ops_env.client.get("/settings?ops=check_failed").text
    assert "The check for updates failed" in page
    assert "unable to access" not in page


def test_update_starts_unit(ops_env):
    git_env(ops_env)
    check(ops_env)
    response = post(ops_env, "/settings/update")
    assert response.status_code == 303
    assert response.headers["location"] == "/settings#ops-update"
    assert ops_env.runner.calls[-1] == START_UPDATE_CMD
    status = read_status(data_dir(ops_env))
    assert (status.state, status.from_tag, status.to_tag) == ("queued", "v0.1.0", "v0.2.0")


def test_update_refused_when_busy(ops_env):
    git_env(ops_env)
    check(ops_env)
    write_status(data_dir(ops_env), UpdateStatus(state="running", step="install", started_at=now() - timedelta(minutes=1)))
    response = post(ops_env, "/settings/update")
    assert response.headers["location"] == "/settings?ops=busy#ops-update"
    assert START_UPDATE_CMD not in ops_env.runner.calls


def test_double_click_starts_one_helper(ops_env):
    git_env(ops_env)
    check(ops_env)
    token = csrf_of(ops_env.client, "/settings")
    first = post(ops_env, "/settings/update", token)
    second = post(ops_env, "/settings/update", token)
    assert first.headers["location"] == "/settings#ops-update"
    assert second.headers["location"] == "/settings?ops=busy#ops-update"
    assert ops_env.runner.calls.count(START_UPDATE_CMD) == 1


def test_stale_status_allows_update(ops_env):
    git_env(ops_env)
    check(ops_env)
    write_status(data_dir(ops_env), UpdateStatus(state="running", step="install", started_at=now() - timedelta(minutes=31)))
    post(ops_env, "/settings/update")
    assert START_UPDATE_CMD in ops_env.runner.calls


def test_update_without_check_refused(ops_env):
    git_env(ops_env)
    response = post(ops_env, "/settings/update")
    assert response.headers["location"] == "/settings?ops=no_update#ops-update"
    assert START_UPDATE_CMD not in ops_env.runner.calls


def test_update_when_up_to_date_refused(ops_env):
    git_env(ops_env, remote=REMOTE_ONE)
    check(ops_env)
    response = post(ops_env, "/settings/update")
    assert response.headers["location"] == "/settings?ops=no_update#ops-update"


def test_update_without_git_refused(ops_env):
    response = post(ops_env, "/settings/update")
    assert response.headers["location"] == "/settings?ops=not_git#ops-update"
    assert ops_env.runner.calls == []


def test_update_start_failure(ops_env):
    git_env(ops_env)
    check(ops_env)
    ops_env.runner.on(START_UPDATE_CMD, CommandResult(1, "", "sudo: a password is required\nmore"))
    response = post(ops_env, "/settings/update")
    assert response.headers["location"] == "/settings?ops=start_failed#ops-update"
    status = read_status(data_dir(ops_env))
    assert status.state == "failed" and status.step == "start"
    assert status.message == "start: sudo: a password is required"


def test_status_json(ops_env):
    write_status(
        data_dir(ops_env),
        UpdateStatus(state="rolled_back", step="rollback", from_tag="v0.1.0", to_tag="v0.2.0", message="install: boom", started_at=now()),
    )
    response = ops_env.client.get("/settings/update/status")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = json.loads(response.content)
    assert (data["state"], data["step"], data["from_tag"], data["to_tag"], data["message"]) == (
        "rolled_back", "rollback", "v0.1.0", "v0.2.0", "install: boom",
    )


def test_status_json_requires_login(ops_env):
    ops_env.client.cookies.clear()
    assert ops_env.client.get("/settings/update/status").status_code == 303


def test_poll_script_only_when_busy(ops_env):
    git_env(ops_env)
    write_status(data_dir(ops_env), UpdateStatus(state="queued", step="start", started_at=now()))
    page = ops_env.client.get("/settings").text
    assert 'data-status-url="/settings/update/status"' in page
    assert "/static/js/ops.js" in page
    write_status(data_dir(ops_env), UpdateStatus(state="succeeded", step="done", started_at=now(), finished_at=now()))
    assert "update-status" not in ops_env.client.get("/settings").text


def test_state_is_shown_with_message_and_backup(ops_env):
    git_env(ops_env)
    write_status(
        data_dir(ops_env),
        UpdateStatus(
            state="rolled_back", step="rollback", started_at=now(), finished_at=now(),
            message="install: boom <b>", backup="/var/lib/reprintbuddy/backups/x.db",
        ),
    )
    page = ops_env.client.get("/settings").text
    assert "Update failed, previous version restored" in page
    assert "install: boom &lt;b&gt;" in page
    assert "/var/lib/reprintbuddy/backups/x.db" in page


def test_update_posts_require_csrf(ops_env):
    git_env(ops_env)
    csrf_of(ops_env.client, "/settings")
    for path in ("/settings/update/check", "/settings/update"):
        assert ops_env.client.post(path, data={}).status_code == 403
        assert ops_env.client.post(path, data={"csrf_token": "forged"}).status_code == 403
    assert not calls_with(ops_env, "ls-remote")
    assert START_UPDATE_CMD not in ops_env.runner.calls


def test_state_and_step_keys_exist():
    from typing import get_args

    locales = i18n.load_locales()
    wanted = {f"ops.update.state.{state}" for state in get_args(UpdateState)}
    wanted |= {f"ops.update.step.{step}" for step in UPDATE_STEPS}
    wanted |= {"ops.update.in_progress", "ops.update.state.interrupted"}
    for lang in ("en", "de"):
        assert wanted <= set(locales[lang]), (lang, sorted(wanted - set(locales[lang])))


def test_no_get_route_runs_commands(ops_env):
    git_env(ops_env)
    for route in api_routes(ops_env.client.app):
        if "GET" in route.methods and "{" not in route.path:
            ops_env.client.get(route.path)
    assert ops_env.runner.calls, "the walk reached no command at all"
    for call in ops_env.runner.calls:
        assert call[3] == "describe", call


def test_writes_blocked_during_update(ops_env):
    git_env(ops_env)
    token = csrf_of(ops_env.client, "/settings")
    write_status(data_dir(ops_env), UpdateStatus(state="running", step="install", started_at=now() - timedelta(minutes=1)))
    exempt = {"/settings/update/check", "/settings/update", "/logout"}
    paths = sorted(
        {
            route.path
            for route in api_routes(ops_env.client.app)
            if "POST" in route.methods and "{" not in route.path and route.path not in exempt
        }
    )
    assert "/settings/language" in paths and "/settings/restart" in paths
    for path in paths:
        response = ops_env.client.post(path, data={"csrf_token": token, "language": "de"})
        assert response.status_code == 503, path
        assert "An update is running. Changes are paused until it is finished." in response.text, path
    with ops_env.client.app.state.sessionmaker() as session:
        assert st.get_setting(session, "language") == "en"
    assert START_UPDATE_CMD not in ops_env.runner.calls
    assert not [c for c in ops_env.runner.calls if "restart" in c or "start" in c]  # no restart, no start either


def test_writes_allowed_again_after_the_update(ops_env):
    token = csrf_of(ops_env.client, "/settings")
    write_status(data_dir(ops_env), UpdateStatus(state="running", started_at=now() - timedelta(minutes=1)))
    assert ops_env.client.post("/settings/language", data={"csrf_token": token, "language": "de"}).status_code == 503
    write_status(data_dir(ops_env), UpdateStatus(state="succeeded", started_at=now(), finished_at=now()))
    assert ops_env.client.post("/settings/language", data={"csrf_token": token, "language": "de"}).status_code == 303


def test_stale_update_does_not_block_writes(ops_env):
    token = csrf_of(ops_env.client, "/settings")
    write_status(data_dir(ops_env), UpdateStatus(state="running", started_at=now() - timedelta(minutes=45)))
    assert ops_env.client.post("/settings/language", data={"csrf_token": token, "language": "de"}).status_code == 303


def test_logout_still_works_during_update(ops_env):
    token = csrf_of(ops_env.client, "/settings")
    write_status(data_dir(ops_env), UpdateStatus(state="running", started_at=now() - timedelta(minutes=1)))
    assert ops_env.client.post("/logout", data={"csrf_token": token}).status_code == 303


# --- an update whose helper is gone must not block everything -------------------------

SHOW_STATE = ("/usr/bin/systemctl", "show")


def unit_state(env, state):
    env.runner.on(SHOW_STATE, CommandResult(0, state + "\n"))


def test_dead_unit_releases_a_stale_busy_state(ops_env):
    """The container restarted mid-update, or the unit failed before it wrote anything."""
    git_env(ops_env)
    token = csrf_of(ops_env.client, "/settings")
    write_status(data_dir(ops_env), UpdateStatus(state="running", step="install", started_at=now() - timedelta(minutes=5)))
    unit_state(ops_env, "inactive")
    page = ops_env.client.get("/settings").text
    assert "Update was interrupted" in page
    assert "Installing packages" in page  # the step it stopped at
    assert "update-status" not in page  # no polling for something that is gone
    assert ops_env.client.post("/settings/language", data={"csrf_token": token, "language": "de"}).status_code == 303


def test_failed_unit_releases_a_queued_state(ops_env):
    git_env(ops_env)
    check(ops_env)
    write_status(data_dir(ops_env), UpdateStatus(state="queued", step="start", from_tag="v0.1.0", to_tag="v0.2.0", started_at=now() - timedelta(minutes=2)))
    unit_state(ops_env, "failed")
    response = post(ops_env, "/settings/update")
    assert response.headers["location"] == "/settings#ops-update"  # not "busy": a new update starts
    assert ops_env.runner.calls[-1] == START_UPDATE_CMD


def test_freshly_queued_update_is_busy_before_the_unit_shows_as_active(ops_env):
    token = csrf_of(ops_env.client, "/settings")
    write_status(data_dir(ops_env), UpdateStatus(state="queued", step="start", started_at=now() - timedelta(seconds=2)))
    unit_state(ops_env, "inactive")
    assert ops_env.client.post("/settings/language", data={"csrf_token": token, "language": "de"}).status_code == 503


@pytest.mark.parametrize("state", ["activating", "active", "deactivating"])
def test_a_running_unit_keeps_the_block(ops_env, state):
    token = csrf_of(ops_env.client, "/settings")
    write_status(data_dir(ops_env), UpdateStatus(state="running", step="install", started_at=now() - timedelta(minutes=5)))
    unit_state(ops_env, state)
    assert ops_env.client.post("/settings/language", data={"csrf_token": token, "language": "de"}).status_code == 503


def test_unknown_unit_state_keeps_the_block(ops_env):
    """systemctl missing or silent: do not guess that the update is gone."""
    token = csrf_of(ops_env.client, "/settings")
    write_status(data_dir(ops_env), UpdateStatus(state="running", step="install", started_at=now() - timedelta(minutes=5)))
    ops_env.runner.on(SHOW_STATE, CommandResult(127, "", "not found"))
    assert ops_env.client.post("/settings/language", data={"csrf_token": token, "language": "de"}).status_code == 503


def test_stale_running_state_is_shown_as_interrupted_not_as_running(ops_env):
    git_env(ops_env)
    write_status(data_dir(ops_env), UpdateStatus(state="running", step="health", started_at=now() - timedelta(minutes=45)))
    page = ops_env.client.get("/settings").text
    assert "Update was interrupted" in page
    assert "Update is running" not in page


def test_idle_page_asks_systemd_nothing(ops_env):
    git_env(ops_env)
    ops_env.client.get("/settings")
    assert not [c for c in ops_env.runner.calls if "systemctl" in c[0] or "show" in c]


# --- the Git tag and the package version side by side -------------------------------------


def test_package_version_is_shown_when_it_differs_from_the_tag(ops_env):
    git_env(ops_env)  # tag v0.1.0, package 0.1.0.dev0 (the version before the first release)
    page = ops_env.client.get("/settings").text
    assert "Installed version: v0.1.0 (package version 0.1.0.dev0)" in page


def test_package_version_is_not_repeated_when_it_matches(ops_env, monkeypatch):
    from reprintbuddy.web.routes import ops as routes

    monkeypatch.setattr(routes, "__version__", "0.1.0")
    git_env(ops_env)
    page = ops_env.client.get("/settings").text
    assert "Installed version: v0.1.0" in page
    assert "package version" not in page
