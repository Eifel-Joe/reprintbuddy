"""p543 (v0.4.11): right after an update by package the card names the new installed version.

The order of a real update: the helper restarts the service (step "restart"), the new process starts and
the open page reloads when the step changes ("health"), so the new process reads the installed version
while `release.json` still names the old one (or, after an installation made with git, does not exist
yet). Only then the helper writes `release.json` (`_settle`) and reports "succeeded"; the page reloads
once more. Each test here does just that: a page view that remembers the old version, then the new
`release.json`, then the page.
"""

from datetime import UTC, datetime

from test_ops_update import REMOTE_TWO, check, git_env, package_env
from test_update_banner import banner

from reprintbuddy.ops.installation import write_marker
from reprintbuddy.ops.versions import VersionInfo

PAGE = "/settings?tab=maintenance"


def settle(env, tag: str) -> None:
    """What the update helper's `_settle` writes after the new version answered."""
    write_marker(env.install_dir, tag=tag, repo=("owner", "repo"), sha256="1" * 64)


def test_p543_after_an_update_from_git_the_card_names_the_new_version(ops_env):
    git_env(ops_env)
    assert "Installed version: v0.1.0" in ops_env.client.get(PAGE).text  # remembered by this process
    settle(ops_env, "v0.2.0")
    page = ops_env.client.get(PAGE).text
    assert "Installed version: v0.2.0" in page
    assert "Installed version: v0.1.0" not in page


def test_p543_after_an_update_from_a_package_the_card_names_the_new_version(ops_env):
    package_env(ops_env, installed="v0.1.0")
    assert "Installed version: v0.1.0" in ops_env.client.get(PAGE).text
    settle(ops_env, "v0.2.0")
    page = ops_env.client.get(PAGE).text
    assert "Installed version: v0.2.0" in page
    assert "Installed version: v0.1.0" not in page


def test_p543_a_check_made_before_the_update_no_longer_offers_the_installed_version(ops_env):
    package_env(ops_env, installed="v0.1.0", remote=REMOTE_TWO)
    check(ops_env)
    assert banner(ops_env.client.get("/").text) is not None  # v0.2.0 is offered
    settle(ops_env, "v0.2.0")
    assert banner(ops_env.client.get("/").text) is None  # the bar of every page, before the card is seen
    page = ops_env.client.get(PAGE).text
    assert "Installed version: v0.2.0" in page
    assert "Update to v0.2.0" not in page


def test_p543_a_check_that_read_the_old_version_and_ends_after_the_update_is_not_kept(ops_env):
    """The automatic check of the new process may read the installation before `_settle` and end after it."""
    from reprintbuddy.web.routes import ops as routes

    package_env(ops_env, installed="v0.1.0")
    ops_env.client.get(PAGE)
    settle(ops_env, "v0.2.0")
    app = ops_env.client.app
    routes.store_versions(app.state, VersionInfo("v0.1.0", "v0.2.0", datetime.now(UTC)), actor="system")
    assert banner(ops_env.client.get("/").text) is None
    page = ops_env.client.get(PAGE).text
    assert "Installed version: v0.2.0" in page
    assert "Update to v0.2.0" not in page


def test_p543_no_update_starts_to_the_version_the_last_update_installed(ops_env):
    from reprintbuddy.ops.service import START_UPDATE_CMD
    from conftest import csrf_of
    from test_ops_update import post

    package_env(ops_env, installed="v0.1.0", remote=REMOTE_TWO)
    check(ops_env)
    token = csrf_of(ops_env.client, "/settings")  # before: no page view in between that could notice it
    settle(ops_env, "v0.2.0")
    response = post(ops_env, "/settings/update", token)
    assert response.headers["location"] == "/settings?tab=maintenance&ops=no_update#ops-update"
    assert START_UPDATE_CMD not in ops_env.runner.calls
