from datetime import UTC, datetime

import pytest

from reprintbuddy.ops.runner import CommandResult
from reprintbuddy.ops.versions import (
    UpdateCheckError,
    check_versions,
    installed_tag,
    is_git_install,
    is_newer,
    local_tags,
    newest_release,
    is_prerelease_tag,
    parse_version,
    remote_tags,
)

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
LS_REMOTE = ("git", "-C", "/opt/x", "ls-remote", "--tags", "--refs", "origin")
DESCRIBE = ("git", "-C", "/opt/x", "describe", "--tags", "--exact-match", "HEAD")


@pytest.fixture
def d(tmp_path):
    return tmp_path / "opt"


def remote(*names):
    return CommandResult(0, "".join(f"abc\trefs/tags/{n}\n" for n in names))


def test_parse_version():
    assert parse_version("v1.2.3") == (1, 2, 3, 1, 0)
    assert parse_version("v1.2.3-beta.4") == (1, 2, 3, 0, 4)
    for bad in ("v1.2", "1.2.3", "v1.2.3-rc1", "latest", "v1.2.3\n", "v01.2.3x"):
        assert parse_version(bad) is None, bad


def test_release_tags_are_plain_ascii_without_leading_zeros():
    """\\d also matches other scripts' digits; a leading zero would make v01.2.3 and v1.2.3 two names."""
    for bad in ("v١.٠.٠", "v01.2.3", "v1.02.3", "v1.2.03", "v1.2.3 "):
        assert parse_version(bad) is None, bad
    assert parse_version("v0.10.100") == (0, 10, 100, 1, 0)


def test_prerelease_tags_are_strict():
    for bad in ("v1.0.0-beta.0", "v1.0.0-beta.01", "v1.0.0-beta", "v1.0.0-rc.1", "v1.0.0-beta.1 ", "v1.0.0-BETA.1"):
        assert parse_version(bad) is None, bad


def test_version_order():
    tags = ["v1.0.1", "v1.0.1-beta.2", "v1.0.0", "v1.0.1-beta.1", "v1.0.2-beta.1"]
    assert sorted(tags, key=parse_version) == ["v1.0.0", "v1.0.1-beta.1", "v1.0.1-beta.2", "v1.0.1", "v1.0.2-beta.1"]
    assert is_newer("v1.0.1", "v1.0.1-beta.2")
    assert not is_newer("v1.0.1-beta.2", "v1.0.1")
    assert is_newer("v1.0.1-beta.10", "v1.0.1-beta.9")
    assert is_newer("v1.0.1-beta.1", "v1.0.0")


def test_is_prerelease_tag():
    assert is_prerelease_tag("v1.0.1-beta.1")
    assert not is_prerelease_tag("v1.0.1")
    assert not is_prerelease_tag("v1.0.1-beta.0")


def test_newest_release():
    assert newest_release(["v0.9.0", "v0.10.0", "v0.2.1", "junk"]) == "v0.10.0"
    assert newest_release([]) is None
    assert newest_release(["junk", "v1.2"]) is None


def test_is_newer():
    assert is_newer("v0.2.0", "v0.1.9")
    assert not is_newer("v0.2.0", "v0.2.0")
    assert not is_newer("v0.1.0", "v0.2.0")
    assert is_newer("v0.1.0", None)
    assert is_newer("v0.1.0", "feature-branch")  # not a release form: counts as older


def test_is_git_install(d):
    assert not is_git_install(d)
    (d / ".git").mkdir(parents=True)
    assert is_git_install(d)


def test_installed_tag(d, fake_runner):
    fake_runner.on(["git"], CommandResult(0, "v0.2.0\n"))
    assert installed_tag(d, fake_runner) == "v0.2.0"
    assert fake_runner.calls[0] == ("git", "-C", str(d), "describe", "--tags", "--exact-match", "HEAD")
    fake_runner.on(["git"], CommandResult(128, "", "fatal: no tag"))
    assert installed_tag(d, fake_runner) is None


def test_local_tags(d, fake_runner):
    fake_runner.on(["git"], CommandResult(0, "v0.1.0\nv0.2.0\n"))
    assert local_tags(d, fake_runner) == ["v0.1.0", "v0.2.0"]
    assert fake_runner.calls[0] == ("git", "-C", str(d), "tag", "--list", "v*")


def test_remote_tags(d, fake_runner):
    fake_runner.on(["git"], CommandResult(0, "abc\trefs/tags/v0.1.0\ndef\trefs/tags/v0.2.0\n"))
    assert remote_tags(d, fake_runner) == ["v0.1.0", "v0.2.0"]
    assert fake_runner.calls[0] == ("git", "-C", str(d), "ls-remote", "--tags", "--refs", "origin")


def test_remote_tags_error(d, fake_runner):
    fake_runner.on(["git"], CommandResult(128, "", "fatal: unable to access 'x'\nmore"))
    with pytest.raises(UpdateCheckError, match="unable to access"):
        remote_tags(d, fake_runner)


def test_remote_tags_uses_a_short_timeout(d):
    seen = {}

    def runner(args, *, cwd=None, timeout=120.0):
        seen["timeout"] = timeout
        return CommandResult(0, "")

    remote_tags(d, runner)
    assert seen["timeout"] == 20.0


def test_check_versions(d, fake_runner):
    fake_runner.on(["git", "-C", str(d), "describe"], CommandResult(0, "v0.1.0\n"))
    fake_runner.on(["git", "-C", str(d), "ls-remote"], remote("v0.1.0", "v0.2.0"))
    info = check_versions(d, fake_runner, now=NOW)
    assert (info.installed, info.newest, info.checked_at) == ("v0.1.0", "v0.2.0", NOW)
    assert info.update_available


def test_check_versions_up_to_date(d, fake_runner):
    fake_runner.on(["git", "-C", str(d), "describe"], CommandResult(0, "v0.2.0\n"))
    fake_runner.on(["git", "-C", str(d), "ls-remote"], remote("v0.1.0", "v0.2.0"))
    assert not check_versions(d, fake_runner, now=NOW).update_available


def test_check_versions_dev_build(d, fake_runner):
    fake_runner.on(["git", "-C", str(d), "describe"], CommandResult(128))
    fake_runner.on(["git", "-C", str(d), "ls-remote"], remote("v0.1.0"))
    info = check_versions(d, fake_runner, now=NOW)
    assert info.installed is None
    assert info.update_available


def test_check_versions_no_release(d, fake_runner):
    fake_runner.on(["git", "-C", str(d), "describe"], CommandResult(0, "v0.1.0\n"))
    fake_runner.on(["git", "-C", str(d), "ls-remote"], remote("nightly"))
    info = check_versions(d, fake_runner, now=NOW)
    assert info.newest is None
    assert not info.update_available
