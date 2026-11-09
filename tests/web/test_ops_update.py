import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import STREAM_PATHS, api_routes, csrf_of

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
    page = ops_env.client.get("/settings?tab=maintenance").text
    assert "need an installation made with the install script" in page
    assert 'action="/settings/update"' not in page
    response = check(ops_env)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?tab=maintenance&ops=not_git#ops-update"
    assert ops_env.runner.calls == []


def test_settings_shows_installed(ops_env):
    git_env(ops_env)
    page = ops_env.client.get("/settings?tab=maintenance").text
    assert "Installed version: v0.1.0" in page
    assert "Not checked yet." in page


def test_dev_build_shown(ops_env, monkeypatch):
    from reprintbuddy.web.routes import ops as routes

    monkeypatch.setattr(routes, "__version__", "0.1.0.dev0")
    git_env(ops_env, describe=None)
    assert "development build 0.1.0.dev0" in ops_env.client.get("/settings?tab=maintenance").text


def test_installed_tag_is_read_once_per_process(ops_env):
    git_env(ops_env)
    for _ in range(3):
        ops_env.client.get("/settings?tab=maintenance")
    assert len(calls_with(ops_env, "describe")) == 1


def test_check_shows_newest(ops_env):
    git_env(ops_env)
    response = check(ops_env)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?tab=maintenance#ops-update"
    page = ops_env.client.get("/settings?tab=maintenance").text
    assert "Newest release: v0.2.0" in page
    assert "Update to v0.2.0" in page


def test_check_up_to_date(ops_env):
    git_env(ops_env, remote=REMOTE_ONE)
    check(ops_env)
    page = ops_env.client.get("/settings?tab=maintenance").text
    assert "Reprintbuddy is up to date." in page
    assert "Update to" not in page


def test_check_failure_message(ops_env):
    git_env(ops_env, remote=CommandResult(128, "", "fatal: unable to access 'https://example.invalid/x'"))
    response = check(ops_env)
    assert response.headers["location"] == "/settings?tab=maintenance&ops=check_failed#ops-update"
    page = ops_env.client.get("/settings?tab=maintenance&ops=check_failed").text
    assert "The check for updates failed" in page
    assert "unable to access" not in page


def test_update_starts_unit(ops_env):
    git_env(ops_env)
    check(ops_env)
    response = post(ops_env, "/settings/update")
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?tab=maintenance#ops-update"
    assert ops_env.runner.calls[-1] == START_UPDATE_CMD
    status = read_status(data_dir(ops_env))
    assert (status.state, status.from_tag, status.to_tag) == ("queued", "v0.1.0", "v0.2.0")


def test_update_refused_when_busy(ops_env):
    git_env(ops_env)
    check(ops_env)
    write_status(data_dir(ops_env), UpdateStatus(state="running", step="install", started_at=now() - timedelta(minutes=1)))
    response = post(ops_env, "/settings/update")
    assert response.headers["location"] == "/settings?tab=maintenance&ops=busy#ops-update"
    assert START_UPDATE_CMD not in ops_env.runner.calls


def test_double_click_starts_one_helper(ops_env):
    git_env(ops_env)
    check(ops_env)
    token = csrf_of(ops_env.client, "/settings")
    first = post(ops_env, "/settings/update", token)
    second = post(ops_env, "/settings/update", token)
    assert first.headers["location"] == "/settings?tab=maintenance#ops-update"
    assert second.headers["location"] == "/settings?tab=maintenance&ops=busy#ops-update"
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
    assert response.headers["location"] == "/settings?tab=maintenance&ops=no_update#ops-update"
    assert START_UPDATE_CMD not in ops_env.runner.calls


def test_update_when_up_to_date_refused(ops_env):
    git_env(ops_env, remote=REMOTE_ONE)
    check(ops_env)
    response = post(ops_env, "/settings/update")
    assert response.headers["location"] == "/settings?tab=maintenance&ops=no_update#ops-update"


def test_update_without_git_refused(ops_env):
    response = post(ops_env, "/settings/update")
    assert response.headers["location"] == "/settings?tab=maintenance&ops=not_git#ops-update"
    assert ops_env.runner.calls == []


def test_update_start_failure(ops_env):
    git_env(ops_env)
    check(ops_env)
    ops_env.runner.on(START_UPDATE_CMD, CommandResult(1, "", "sudo: a password is required\nmore"))
    response = post(ops_env, "/settings/update")
    assert response.headers["location"] == "/settings?tab=maintenance&ops=start_failed#ops-update"
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
    page = ops_env.client.get("/settings?tab=maintenance").text
    assert 'data-status-url="/settings/update/status"' in page
    assert "/static/js/ops.js" in page
    write_status(data_dir(ops_env), UpdateStatus(state="succeeded", step="done", started_at=now(), finished_at=now()))
    assert "update-status" not in ops_env.client.get("/settings?tab=maintenance").text


def test_state_is_shown_with_message_and_backup(ops_env):
    git_env(ops_env)
    write_status(
        data_dir(ops_env),
        UpdateStatus(
            state="rolled_back", step="rollback", started_at=now(), finished_at=now(),
            message="install: boom <b>", backup="/var/lib/reprintbuddy/backups/x.db",
        ),
    )
    page = ops_env.client.get("/settings?tab=maintenance").text
    assert "Update failed, previous version restored" in page
    assert "install: boom &lt;b&gt;" in page
    assert "/var/lib/reprintbuddy/backups/x.db" in page


def _set_language(env, lang):
    from conftest import sm_of

    with sm_of(env.client).begin() as session:
        st.set_setting(session, "language", lang, commit=False)


def _failed(**kwargs):
    return UpdateStatus(state="failed", step="fetch", started_at=now(), finished_at=now(), **kwargs)


@pytest.mark.parametrize(
    "lang, expected",
    [
        ("en", "The releases could not be fetched: fatal: no network"),
        ("de", "Die Versionen konnten nicht geholt werden: fatal: no network"),
    ],
)
def test_the_reason_of_a_failed_update_is_in_the_language_of_the_page(ops_env, lang, expected):  # v0.3.14 p176
    """The helper writes a code next to its English line; the card shows the code in the viewer's language
    (it showed the English line in the German page)."""
    git_env(ops_env)
    _set_language(ops_env, lang)
    write_status(
        data_dir(ops_env),
        _failed(message="fetch: fatal: no network", reason="fetch_failed", params={"detail": "fatal: no network"}),
    )
    page = ops_env.client.get("/settings?tab=maintenance").text
    assert expected in page
    assert "fetch: fatal: no network" not in page  # not twice, and not the English line


def test_every_reason_has_a_text_in_both_languages_that_the_card_can_fill(ops_env):
    from string import Formatter

    from reprintbuddy.ops.status import UPDATE_REASONS

    git_env(ops_env)
    locales = i18n.load_locales()
    for lang in ("en", "de"):
        _set_language(ops_env, lang)
        for reason in UPDATE_REASONS:
            key = f"ops.update.reason.{reason}"
            names = {n for _, n, _, _ in Formatter().parse(locales[lang][key]) if n}
            params = {name: f"value-{name}" for name in names}
            write_status(data_dir(ops_env), _failed(message="the english line", reason=reason, params=params))
            page = ops_env.client.get("/settings?tab=maintenance").text
            assert "the english line" not in page, (lang, reason)
            for value in params.values():
                assert value in page, (lang, reason, value)


@pytest.mark.parametrize("lang, label", [("en", "Backup:"), ("de", "Sicherung:")])
def test_the_backup_of_a_failed_rollback_is_labelled(ops_env, lang, label):  # review of p176
    """The English line said "– backup: <path>"; with the reason from the language files the path needs its label."""
    git_env(ops_env)
    _set_language(ops_env, lang)
    write_status(
        data_dir(ops_env),
        UpdateStatus(
            state="rollback_failed", step="rollback", started_at=now(), finished_at=now(),
            message="health: x – backup: /var/lib/reprintbuddy/backups/x.db", reason="health_failed",
            backup="/var/lib/reprintbuddy/backups/x.db",
        ),
    )
    page = ops_env.client.get("/settings?tab=maintenance").text
    assert f"{label} /var/lib/reprintbuddy/backups/x.db" in page


def test_a_reason_whose_values_are_missing_falls_back_to_the_english_line(ops_env):
    git_env(ops_env)
    _set_language(ops_env, "de")
    write_status(data_dir(ops_env), _failed(message="fetch: fatal: x", reason="fetch_failed", params={}))
    assert "fetch: fatal: x" in ops_env.client.get("/settings?tab=maintenance").text


def test_a_state_file_without_reason_shows_the_line_as_before(ops_env):
    git_env(ops_env)
    _set_language(ops_env, "de")
    write_status(data_dir(ops_env), _failed(message="fetch: fatal: x"))
    assert "fetch: fatal: x" in ops_env.client.get("/settings?tab=maintenance").text


def test_a_failed_start_of_the_helper_names_its_reason(ops_env):  # p176: written by the route, not the helper
    git_env(ops_env)
    check(ops_env)
    ops_env.runner.on(START_UPDATE_CMD, CommandResult(1, "", "sudo: a password is required\nmore"))
    post(ops_env, "/settings/update")
    status = read_status(data_dir(ops_env))
    assert (status.reason, status.params) == ("start_failed", {"detail": "sudo: a password is required"})


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
    # `ops_view` builds a key for every state but "idle" (nothing has run: the card shows no state); a busy
    # state nobody runs any more is "interrupted" (v0.3.10 p129: no text for "idle").
    wanted = {f"ops.update.state.{state}" for state in get_args(UpdateState) if state != "idle"}
    wanted |= {f"ops.update.step.{step}" for step in UPDATE_STEPS}
    wanted |= {"ops.update.in_progress", "ops.update.state.interrupted", "ops.update.state.not_started"}
    left_behind = ("not_started_check", "not_started_none", "unrecorded", "unrecorded_target")  # p144, p145
    wanted |= {f"ops.update.state.{key}" for key in left_behind}
    for lang in ("en", "de"):
        assert wanted <= set(locales[lang]), (lang, sorted(wanted - set(locales[lang])))


def test_no_get_route_runs_commands(ops_env):
    git_env(ops_env)
    for route in api_routes(ops_env.client.app):
        if "GET" in route.methods and "{" not in route.path and route.path not in STREAM_PATHS:
            ops_env.client.get(route.path)
    assert ops_env.runner.calls, "the walk reached no command at all"
    for call in ops_env.runner.calls:
        assert call[3] == "describe", call


def test_writes_blocked_during_update(ops_env):
    git_env(ops_env)
    token = csrf_of(ops_env.client, "/settings")
    write_status(data_dir(ops_env), UpdateStatus(state="running", step="install", started_at=now() - timedelta(minutes=1)))
    exempt = {
        "/settings/update/check",
        "/settings/update",
        "/logout",
        "/bambuddy/rights/later",
        "/bambuddy/rights/recheck",
        "/bambuddy/rights/done",
    }
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


def test_rights_buttons_work_during_an_update(ops_env, respx_mock):
    """M4 of the review: they write nothing to the database, so the dialog can be closed."""
    from conftest import mock_auth_me

    mock_auth_me(respx_mock)
    token = csrf_of(ops_env.client, "/settings")
    write_status(data_dir(ops_env), UpdateStatus(state="running", step="install", started_at=now() - timedelta(minutes=1)))
    for path in ("/bambuddy/rights/later", "/bambuddy/rights/recheck"):
        response = ops_env.client.post(path, data={"csrf_token": token, "next": "/settings"})
        assert response.status_code == 303 and response.headers["location"] == "/settings", path
    with ops_env.client.app.state.sessionmaker() as session:
        assert st.get_setting(session, "language") == "en"  # nothing was written


def test_done_works_while_an_update_runs(ops_env):
    """"Continue" of the green rights dialog only changes the session cookie: never paused."""
    token = csrf_of(ops_env.client, "/settings")
    write_status(data_dir(ops_env), UpdateStatus(state="running", step="install", started_at=now() - timedelta(minutes=1)))
    response = ops_env.client.post("/bambuddy/rights/done", data={"csrf_token": token, "next": "/settings"})
    assert response.status_code == 303 and response.headers["location"] == "/settings"


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
    page = ops_env.client.get("/settings?tab=maintenance").text
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
    assert response.headers["location"] == "/settings?tab=maintenance#ops-update"  # not "busy": a new update starts
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
    page = ops_env.client.get("/settings?tab=maintenance").text
    assert "Update was interrupted" in page
    assert "Update is running" not in page


# --- a start cut off by a shutdown (v0.3.11 p123) -----------------------------------------
# The helper's first act is to overwrite "queued" with "running": a "queued" whose grace is over and
# whose unit is not running was never picked up by the helper, so nothing was changed.


def queued_long_ago(env, *, age=timedelta(minutes=2), to_tag="v0.2.0", from_tag="v0.1.0"):
    write_status(
        data_dir(env),
        UpdateStatus(state="queued", step="start", from_tag=from_tag, to_tag=to_tag, started_at=now() - age),
    )


@pytest.mark.parametrize("unit", ["inactive", "failed"])
@pytest.mark.parametrize("age", [timedelta(minutes=2), timedelta(minutes=45)])
def test_a_start_that_never_began_says_so_not_interrupted(ops_env, unit, age):
    git_env(ops_env)
    check(ops_env)
    queued_long_ago(ops_env, age=age)
    unit_state(ops_env, unit)
    page = ops_env.client.get("/settings?tab=maintenance").text
    assert "The update did not start. Please start it again." in page
    assert "Update was interrupted" not in page
    assert "Starting" not in page  # no step for a run that never began
    assert "Update to v0.2.0" in page  # the button to start it again
    assert "update-status" not in page  # nothing to follow


def test_a_queued_state_whose_target_runs_shows_the_update_as_finished(ops_env):
    """The helper ran but could not record it (status file not writable): the target version runs."""
    git_env(ops_env, describe="v0.2.0\n")
    queued_long_ago(ops_env)
    unit_state(ops_env, "inactive")
    page = ops_env.client.get("/settings?tab=maintenance").text
    assert "Update finished" in page
    assert "did not start" not in page and "Update was interrupted" not in page


def test_a_fresh_start_is_still_shown_as_starting(ops_env):
    git_env(ops_env)
    queued_long_ago(ops_env, age=timedelta(seconds=2))
    unit_state(ops_env, "inactive")
    page = ops_env.client.get("/settings?tab=maintenance").text
    assert "Update is starting" in page
    assert "did not start" not in page


def test_idle_page_asks_systemd_nothing(ops_env):
    git_env(ops_env)
    ops_env.client.get("/settings?tab=maintenance")
    assert not [c for c in ops_env.runner.calls if "systemctl" in c[0] or "show" in c]


# --- the Git tag and the package version side by side -------------------------------------


def test_package_version_is_shown_when_it_differs_from_the_tag(ops_env, monkeypatch):
    from reprintbuddy.web.routes import ops as routes

    monkeypatch.setattr(routes, "__version__", "0.1.0.dev0")
    git_env(ops_env)  # tag v0.1.0, package 0.1.0.dev0
    page = ops_env.client.get("/settings?tab=maintenance").text
    assert "Installed version: v0.1.0 (package version 0.1.0.dev0)" in page


def test_package_version_is_not_repeated_when_it_matches(ops_env, monkeypatch):
    from reprintbuddy.web.routes import ops as routes

    monkeypatch.setattr(routes, "__version__", "0.1.0")
    git_env(ops_env)
    page = ops_env.client.get("/settings?tab=maintenance").text
    assert "Installed version: v0.1.0" in page
    assert "package version" not in page


# --- v0.3.8 p96: the question whether an update runs is never asked on the event loop -------------------------


def test_the_write_guard_and_the_settings_page_ask_in_a_worker_thread(ops_env, monkeypatch):
    import asyncio

    from conftest import csrf_of

    from reprintbuddy.ops import running

    seen: list[str] = []

    def record(*_args):
        try:
            asyncio.get_running_loop()
            seen.append("event loop")
        except RuntimeError:
            seen.append("worker thread")
        return False

    monkeypatch.setattr(running, "update_running", record)
    assert ops_env.client.get("/settings").status_code == 200  # the update card
    token = csrf_of(ops_env.client, "/settings")
    response = ops_env.client.post("/settings/language", data={"csrf_token": token, "language": "en"})  # the guard
    assert response.status_code == 303
    assert seen and set(seen) == {"worker thread"}, seen


# --- v0.3.8: starting the update and the restart do not hold the event loop, and a shutdown stops them ----------

import asyncio  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from test_concurrency import COOKIE, asgi_client, token_of  # noqa: E402

from reprintbuddy.ops.runner import cancel_flag  # noqa: E402
from reprintbuddy.ops.service import RESTART_CMD  # noqa: E402


def hanging_sudo(env, command):
    """Make `command` block (in the worker thread) until the test lets it go."""
    started, release = threading.Event(), threading.Event()

    def blocked(_call):
        started.set()
        release.wait(10)
        return CommandResult(0)

    env.runner.on(command, blocked)
    return started, release


def sudo_until_told_to_stop(env, command):
    """A `command` that waits for the cancel flag of `in_thread` (what the real runner would do)."""
    started, seen_stop = threading.Event(), threading.Event()

    def waits(_call):
        flag = cancel_flag.get()
        started.set()
        deadline = time.monotonic() + 10
        while not flag.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        if flag.is_set():
            seen_stop.set()
        return CommandResult(130, "", "cancelled")

    env.runner.on(command, waits)
    return started, seen_stop


async def test_a_hanging_sudo_holds_neither_the_loop_nor_lets_a_second_click_through(ops_env):
    git_env(ops_env)
    check(ops_env)
    started, release = hanging_sudo(ops_env, START_UPDATE_CMD)
    app = ops_env.client.app
    async with asgi_client(app, ops_env.client.cookies.get(COOKIE)) as client:
        token = token_of(await client.get("/settings"))
        first = asyncio.create_task(client.post("/settings/update", data={"csrf_token": token}))
        assert await asyncio.to_thread(started.wait, 5)  # sudo is hanging in its worker thread
        began = time.monotonic()
        assert (await client.get("/healthz")).status_code == 200  # the loop answers meanwhile
        assert time.monotonic() - began < 2
        second = await client.post("/settings/update", data={"csrf_token": token})
        assert second.headers["location"] == "/settings?tab=maintenance&ops=busy#ops-update"
        assert app.state.update_starting is True
        release.set()
        response = await asyncio.wait_for(first, 10)
    assert response.headers["location"] == "/settings?tab=maintenance#ops-update"
    assert ops_env.runner.calls.count(START_UPDATE_CMD) == 1
    assert app.state.update_starting is False
    assert read_status(data_dir(ops_env)).state == "queued"


async def test_a_failing_start_gives_the_gate_and_the_state_back(ops_env):
    git_env(ops_env)
    check(ops_env)
    ops_env.runner.on(START_UPDATE_CMD, CommandResult(1, "", "sudo: a password is required"))
    app = ops_env.client.app
    async with asgi_client(app, ops_env.client.cookies.get(COOKIE)) as client:
        token = token_of(await client.get("/settings"))
        response = await client.post("/settings/update", data={"csrf_token": token})
    assert response.headers["location"] == "/settings?tab=maintenance&ops=start_failed#ops-update"
    assert read_status(data_dir(ops_env)).state == "failed"
    assert app.state.update_starting is False


async def test_a_runner_that_raises_is_a_failed_start_not_a_stuck_one(ops_env):
    git_env(ops_env)
    check(ops_env)

    def breaks(_call):
        raise PermissionError("sudo")

    ops_env.runner.on(START_UPDATE_CMD, breaks)
    app = ops_env.client.app
    async with asgi_client(app, ops_env.client.cookies.get(COOKIE)) as client:
        token = token_of(await client.get("/settings"))
        response = await client.post("/settings/update", data={"csrf_token": token})
    assert response.headers["location"] == "/settings?tab=maintenance&ops=start_failed#ops-update"
    status = read_status(data_dir(ops_env))
    assert (status.state, status.message) == ("failed", "start: PermissionError")
    assert app.state.update_starting is False


async def test_a_restore_that_begins_while_the_start_asks_systemd_wins(ops_env, monkeypatch):
    """The restore sets its state before it asks; the start looks again right before it writes queued."""
    from reprintbuddy.web.routes import ops as routes

    git_env(ops_env)
    check(ops_env)
    write_status(
        data_dir(ops_env), UpdateStatus(state="running", step="install", started_at=now() - timedelta(minutes=2))
    )
    app = ops_env.client.app

    armed = []  # set right before the POST: the first question after that is the handler's own

    async def stale_then_a_restore_begins(*_args):
        if armed:
            app.state.restore.phase = "running"
        return False  # systemd says the old update is dead

    monkeypatch.setattr(routes, "update_running_async", stale_then_a_restore_begins)
    async with asgi_client(app, ops_env.client.cookies.get(COOKIE)) as client:
        token = token_of(await client.get("/settings"))
        armed.append(1)
        response = await client.post("/settings/update", data={"csrf_token": token})
    assert response.status_code == 303, response.text[:300]
    assert response.headers["location"] == "/settings?tab=maintenance&ops=busy#ops-update"
    assert START_UPDATE_CMD not in ops_env.runner.calls
    assert read_status(data_dir(ops_env)).state == "running"  # nothing was written over it
    assert app.state.update_starting is False


async def test_cancelling_the_start_stops_sudo_and_frees_the_gate(ops_env):
    """A shutdown (or a closed request) does not wait for a sudo that hangs."""
    git_env(ops_env)
    check(ops_env)
    started, seen_stop = sudo_until_told_to_stop(ops_env, START_UPDATE_CMD)
    app = ops_env.client.app
    async with asgi_client(app, ops_env.client.cookies.get(COOKIE)) as client:
        token = token_of(await client.get("/settings"))
        task = asyncio.create_task(client.post("/settings/update", data={"csrf_token": token}))
        assert await asyncio.to_thread(started.wait, 5)
        began = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.monotonic() - began < 2
    assert await asyncio.to_thread(seen_stop.wait, 5)  # the command itself was told to stop
    assert app.state.update_starting is False
    assert read_status(data_dir(ops_env)).state == "queued"  # left as it is; believed for the grace only


async def test_cancelling_the_restart_stops_sudo(ops_env):
    started, seen_stop = sudo_until_told_to_stop(ops_env, RESTART_CMD)
    app = ops_env.client.app
    async with asgi_client(app, ops_env.client.cookies.get(COOKIE)) as client:
        token = token_of(await client.get("/settings"))
        task = asyncio.create_task(client.post("/settings/restart", data={"csrf_token": token}))
        assert await asyncio.to_thread(started.wait, 5)
        began = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.monotonic() - began < 2
    assert await asyncio.to_thread(seen_stop.wait, 5)


def test_the_installed_tag_is_asked_in_a_worker_thread(ops_env):
    """v0.3.8: `git describe` (once per process) is not run in the event loop by the settings page."""
    import asyncio

    git_env(ops_env)
    where = []
    real = ops_env.runner

    def runner(args, **kwargs):
        if "describe" in args:
            try:
                asyncio.get_running_loop()
                where.append("event loop")
            except RuntimeError:
                where.append("worker thread")
        return real(args, **kwargs)

    app = ops_env.client.app
    ops = app.state.ops
    app.state.ops = type(ops)(install_dir=ops.install_dir, runner=runner, effective=ops.effective)
    assert ops_env.client.get("/settings?tab=maintenance").status_code == 200
    assert where == ["worker thread"]
    ops_env.client.get("/settings?tab=maintenance")
    assert where == ["worker thread"]  # asked once, then remembered


# --- review: a click whose sudo is still on its way counts as a running update everywhere ----------------------


def test_a_start_on_its_way_pauses_writes_and_is_seen_by_the_engine(ops_env):
    """"queued" is believed for 15 s only and sudo may take as long: the gate itself says busy."""
    app = ops_env.client.app
    app.state.update_starting = True
    try:
        token = csrf_of(ops_env.client, "/settings")
        response = ops_env.client.post("/settings/language", data={"csrf_token": token, "language": "de"})
        assert response.status_code == 503  # the write guard
        assert app.state.ticker.update_running() is True  # the engine holds its ticks and sends
        assert app.state.ticker.update_started() is True
    finally:
        app.state.update_starting = False
    assert app.state.ticker.update_running() is False
    token = csrf_of(ops_env.client, "/settings")
    assert ops_env.client.post("/settings/language", data={"csrf_token": token, "language": "de"}).status_code == 303


# --- v0.3.12 p144/p145: what the card says about a "queued" the helper never overwrote -------------------------
# p144: "please start it again" needs a way to do so. p145: a helper that could not write its state at all
# leaves "queued" behind although it backed up the database (and maybe rolled back); the backup made after
# the start tells the two apart.


def backup_made(env, *, label="v0.1.0", at=None):
    folder = data_dir(env) / "backups"
    folder.mkdir(exist_ok=True)
    moment = at or now()
    path = folder / f"reprintbuddy-{label}-{moment:%Y%m%dT%H%M%SZ}.db"
    path.write_bytes(b"")
    return path


def maintenance(env):
    return env.client.get("/settings?tab=maintenance").text


def test_p144_a_start_that_never_began_without_a_check_offers_the_check(ops_env):
    git_env(ops_env)  # no check since the start: the process that would know the newest version is gone
    queued_long_ago(ops_env)
    unit_state(ops_env, "inactive")
    page = maintenance(ops_env)
    assert "The update did not start. Please check for updates, then start it again." in page
    notice = page.split("The update did not start.", 1)[1].split("</section>", 1)[0]
    assert 'action="/settings/update/check"' in notice  # the check is offered right at the notice
    assert calls_with(ops_env, "ls-remote") == []  # the page asked GitHub nothing


def test_p144_a_start_that_never_began_with_nothing_newer_says_so(ops_env):
    git_env(ops_env, remote=REMOTE_ONE)  # the version was withdrawn: the check finds nothing newer
    check(ops_env)
    queued_long_ago(ops_env)
    unit_state(ops_env, "inactive")
    page = maintenance(ops_env)
    assert "The update to v0.2.0 did not start, and there is no newer version now." in page
    assert "Please start it again" not in page
    assert 'action="/settings/update"' not in page


def test_p144_a_start_that_never_began_with_a_newer_version_keeps_the_button(ops_env):
    git_env(ops_env)
    check(ops_env)
    queued_long_ago(ops_env)
    unit_state(ops_env, "inactive")
    page = maintenance(ops_env)
    assert "The update did not start. Please start it again." in page
    assert "Update to v0.2.0" in page


def test_p145_a_helper_that_backed_up_but_could_not_record_is_no_start_that_never_began(ops_env):
    git_env(ops_env)  # v0.1.0 runs again: the helper rolled back, or stopped after the backup
    check(ops_env)
    queued_long_ago(ops_env)
    backup_made(ops_env)
    unit_state(ops_env, "inactive")
    page = maintenance(ops_env)
    assert "did not start" not in page
    assert "Update finished" not in page
    assert "could not save its state" in page
    assert "journalctl -u reprintbuddy-update" in page


def test_p145_the_target_runs_after_a_backup_points_to_the_log_not_to_finished(ops_env):
    git_env(ops_env, describe="v0.2.0\n")
    queued_long_ago(ops_env)
    backup_made(ops_env)
    unit_state(ops_env, "inactive")
    page = maintenance(ops_env)
    assert "Version v0.2.0 is running. The update helper could not save its state" in page
    assert "journalctl -u reprintbuddy-update" in page
    assert "Update finished" not in page


@pytest.mark.parametrize(
    "card_tag, helper_label",
    [
        (None, "v0.1.0"),  # p158: the card's `git describe` failed at the start, the helper's did not
        ("v0.1.0", "0123456789ab"),  # the mirror: the helper's `git describe` failed, it named the commit
    ],
)
def test_p158_the_helpers_backup_is_found_whichever_side_knew_the_tag(ops_env, card_tag, helper_label):
    git_env(ops_env)
    check(ops_env)
    queued_long_ago(ops_env, from_tag=card_tag)
    backup_made(ops_env, label=helper_label)
    unit_state(ops_env, "inactive")
    page = maintenance(ops_env)
    assert "did not start" not in page
    assert "could not save its state" in page


@pytest.mark.parametrize(
    "label, age",
    [
        ("v0.1.0", timedelta(minutes=10)),  # made before this start: an earlier update's backup
        ("before-restore", timedelta(0)),  # a restore after the grace: not the helper's
    ],
)
def test_p145_other_backups_do_not_count(ops_env, label, age):
    git_env(ops_env)
    check(ops_env)
    queued_long_ago(ops_env)
    backup_made(ops_env, label=label, at=now() - age)
    unit_state(ops_env, "inactive")
    page = maintenance(ops_env)
    assert "The update did not start. Please start it again." in page
