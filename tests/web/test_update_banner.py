"""The notice bar for a new version and the switch for the automatic check (v0.3.6, Spec 11.3a).

The remote is a scripted command runner (`FakeRunner`): no git, no network. The bar is only drawn
for an installation that sits on a real release tag, when a newer release exists and the check is
switched on; a failed check shows nothing.
"""

import asyncio
import re
from types import SimpleNamespace

import pytest
from conftest import csrf_of, make_client, sm_of
from test_ops_update import REMOTE_TWO, git_env

from reprintbuddy import settings as st
from reprintbuddy.ops.context import OpsContext
from reprintbuddy.ops.runner import CommandResult
from reprintbuddy.web.i18n import translate

PAGES = ["/", "/queue", "/articles", "/settings", "/settings?tab=maintenance"]
LINK = 'href="/settings?tab=maintenance#ops-update"'


def banner(page: str) -> str | None:
    found = re.search(r'<div class="update-banner"[^>]*>(.*?)</div>', page, re.S)
    return found.group(1) if found else None


async def check_now(env) -> None:
    await env.client.app.state.auto_check.run_once()


async def test_a_newer_release_shows_the_bar_on_every_page(ops_env):
    git_env(ops_env)
    await check_now(ops_env)
    for path in PAGES:
        found = banner(ops_env.client.get(path).text)
        assert found is not None, path
        assert translate("en", "update.banner", version="v0.2.0") in found
        assert translate("en", "update.banner_link") in found
        assert LINK in found


async def test_the_bar_speaks_german_too(ops_env):
    git_env(ops_env)
    with sm_of(ops_env.client).begin() as s:
        st.set_setting(s, "language", "de")
    await check_now(ops_env)
    found = banner(ops_env.client.get("/").text)
    assert "Version v0.2.0 ist verfügbar" in found
    assert "Update anzeigen" in found


async def test_the_same_state_as_the_button_check(ops_env):
    git_env(ops_env)
    await check_now(ops_env)
    page = ops_env.client.get("/settings?tab=maintenance").text
    assert "Newest release: v0.2.0" in page  # the card that the button's check fills


async def test_up_to_date_shows_no_bar(ops_env):
    """After a successful update the new process finds installed = newest: no bar."""
    git_env(ops_env, describe="v0.2.0\n", remote=REMOTE_TWO)
    await check_now(ops_env)
    assert banner(ops_env.client.get("/").text) is None


async def test_the_bar_goes_when_the_installed_version_catches_up(ops_env):
    git_env(ops_env)
    await check_now(ops_env)
    assert banner(ops_env.client.get("/").text) is not None
    ops_env.runner.on(("git", "-C", str(ops_env.install_dir), "describe"), CommandResult(0, "v0.2.0\n"))
    await check_now(ops_env)
    assert banner(ops_env.client.get("/").text) is None


async def test_a_development_build_gets_no_bar(ops_env):
    git_env(ops_env, describe=None)  # git, but not on a tag
    await check_now(ops_env)
    assert banner(ops_env.client.get("/").text) is None


async def test_without_git_nothing_is_asked_and_no_bar(ops_env):
    assert await ops_env.client.app.state.auto_check.run_once() is None
    assert ops_env.runner.calls == []
    assert banner(ops_env.client.get("/").text) is None


async def test_a_failed_first_check_shows_nothing(ops_env):
    git_env(ops_env, remote=CommandResult(128, "", "fatal: unable to access 'https://example.invalid/x'"))
    await check_now(ops_env)
    page = ops_env.client.get("/").text
    assert banner(page) is None
    assert "unable to access" not in page
    assert translate("en", "ops.msg.check_failed") not in page


async def test_a_later_failure_keeps_an_earlier_result(ops_env):
    git_env(ops_env)
    await check_now(ops_env)
    ops_env.runner.on(("git", "-C", str(ops_env.install_dir), "ls-remote"), CommandResult(128, "", "fatal: down"))
    assert await ops_env.client.app.state.auto_check.run_once() == 3600
    assert banner(ops_env.client.get("/").text) is not None


async def test_switched_off_there_is_no_check_and_no_bar(ops_env):
    git_env(ops_env)
    await check_now(ops_env)
    with sm_of(ops_env.client).begin() as s:
        st.set_setting(s, "update_check", False)
    assert banner(ops_env.client.get("/").text) is None
    ops_env.runner.calls.clear()
    await check_now(ops_env)
    assert [c for c in ops_env.runner.calls if "ls-remote" in c] == []


def test_the_check_is_on_by_default(ops_env):
    with sm_of(ops_env.client)() as s:
        assert st.get_setting(s, "update_check") is True


def test_the_switch_is_on_the_update_card_with_a_git_installation(ops_env):
    git_env(ops_env)
    page = ops_env.client.get("/settings?tab=maintenance").text
    assert 'action="/settings/update/auto"' in page
    assert translate("en", "ops.update.auto") in page


def test_no_switch_without_a_git_installation(ops_env):
    assert 'action="/settings/update/auto"' not in ops_env.client.get("/settings?tab=maintenance").text


@pytest.mark.parametrize("wanted", ["0", "1"])
def test_the_switch_saves_the_wanted_state_and_wakes_the_loop(ops_env, wanted):
    git_env(ops_env)
    app = ops_env.client.app
    app.state.update_check_wake.clear()
    token = csrf_of(ops_env.client, "/settings?tab=maintenance")
    response = ops_env.client.post(
        "/settings/update/auto",
        data={"csrf_token": token, "enabled": wanted, "next": "/settings?tab=maintenance"},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/settings?tab=maintenance#ops-update"
    with sm_of(ops_env.client)() as s:
        assert st.get_setting(s, "update_check") is (wanted == "1")
    assert app.state.update_check_wake.is_set()


def test_the_switch_needs_the_csrf_token(ops_env):
    git_env(ops_env)
    assert ops_env.client.post("/settings/update/auto", data={"enabled": "0"}).status_code == 403
    with sm_of(ops_env.client)() as s:
        assert st.get_setting(s, "update_check") is True


def test_a_wrong_value_changes_nothing(ops_env):
    git_env(ops_env)
    token = csrf_of(ops_env.client, "/settings?tab=maintenance")
    ops_env.client.post("/settings/update/auto", data={"csrf_token": token, "enabled": "toggle"})
    with sm_of(ops_env.client)() as s:
        assert st.get_setting(s, "update_check") is True


async def _never_sleep(_seconds: float) -> None:
    await asyncio.Event().wait()


async def test_the_app_runs_the_check_at_start(tmp_path, fake_runner):
    """Through the real start-up: the lifespan starts the loop, which checks at once."""
    install = tmp_path / "opt"
    ops = OpsContext(install_dir=install, runner=fake_runner)
    client = make_client(tmp_path / "w.db", ops=ops, run_engine=True, live_sleep=_never_sleep)
    app = client.app
    app.state.bambuddy_factory = lambda: None
    git_env(SimpleNamespace(install_dir=install, runner=fake_runner))
    async with app.router.lifespan_context(app):
        async with asyncio.timeout(5):
            while getattr(app.state, "version_info", None) is None:
                await asyncio.sleep(0.01)
    assert app.state.version_info.newest == "v0.2.0"
    assert app.state.installed_tag == "v0.1.0"


async def test_the_real_pause_ends_when_the_switch_wakes_it(tmp_path, fake_runner):
    """The pause of the real loop (app.py) ends by the wake event: switched on, the check runs now."""
    install = tmp_path / "opt"
    ops = OpsContext(install_dir=install, runner=fake_runner)
    client = make_client(tmp_path / "x.db", ops=ops, run_engine=True, live_sleep=_never_sleep)
    app = client.app
    app.state.bambuddy_factory = lambda: None
    git_env(SimpleNamespace(install_dir=install, runner=fake_runner))
    with sm_of(client).begin() as s:
        st.set_setting(s, "update_check", False)

    def asked() -> int:
        return len([c for c in fake_runner.calls if "ls-remote" in c])

    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.2)  # the loop has had its round: switched off, it asks nothing
        assert asked() == 0
        with sm_of(client).begin() as s:
            st.set_setting(s, "update_check", True)
        app.state.update_check_wake.set()  # what the switch route does
        async with asyncio.timeout(5):
            while asked() == 0:
                await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        assert not app.state.update_check_wake.is_set()  # used up: no round after round
        assert asked() == 1


async def test_a_running_update_skips_the_round(ops_env):
    from datetime import UTC, datetime, timedelta

    from reprintbuddy.ops.status import UpdateStatus, write_status

    git_env(ops_env)
    started = datetime.now(UTC) - timedelta(minutes=1)
    write_status(ops_env.db_path.parent, UpdateStatus(state="running", step="install", started_at=started))
    assert await ops_env.client.app.state.auto_check.run_once() == 3600
    assert [c for c in ops_env.runner.calls if "ls-remote" in c] == []


def test_a_test_app_never_looks_at_the_systems_install_dir(tmp_path):
    """With the engine on, the check would run a real `git ls-remote` wherever the default install
    directory is a git copy (a container, Linux CI); the test apps get an install dir of their own."""
    from reprintbuddy.ops.service import INSTALL_DIR

    assert make_client(tmp_path / "d.db").app.state.ops.install_dir != INSTALL_DIR
