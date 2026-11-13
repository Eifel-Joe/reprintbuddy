"""v0.4.9 p500: what GitHub offers, read from one request to the release list (Spec 11.3a).

Never against the real GitHub: every test hands in a fake HTTP function.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from reprintbuddy.ops import releases as rl

BASE = "https://github.com/owner/repo/releases/download"


def asset(name, *, tag=None, state="uploaded", url=None, digest=None):
    item = {"name": name, "state": state, "browser_download_url": url or f"{BASE}/{tag}/{name}"}
    if digest is not None:
        item["digest"] = digest
    return item


def release(tag, *, body="Notes.", draft=False, prerelease=None, assets=None, published="2026-11-09T15:19:00Z"):
    if prerelease is None:
        prerelease = "-beta." in tag
    if assets is None:
        assets = [asset(rl.package_name(tag), tag=tag, digest="sha256:" + "a" * 64), asset(rl.checksum_name(tag), tag=tag)]
    return {"tag_name": tag, "body": body, "draft": draft, "prerelease": prerelease, "published_at": published, "assets": assets}


def tags(releases):
    return [item.tag for item in releases]


class FakeHttp:
    def __init__(self, status=200, body=b"[]", error: Exception | None = None):
        self.status, self.body, self.error = status, body, error
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        if self.error is not None:
            raise self.error
        return self.status, self.body


def answering(*items) -> FakeHttp:
    return FakeHttp(body=json.dumps(list(items)).encode())


def test_asset_names():
    assert rl.package_name("v1.0.0") == "reprintbuddy-v1.0.0.tar.gz"
    assert rl.checksum_name("v1.0.0-beta.2") == "reprintbuddy-v1.0.0-beta.2.tar.gz.sha256"


def test_a_complete_release_is_read_with_everything_the_update_needs():
    [found] = rl.parse_releases([release("v1.0.0", body="Some *news*.")])
    assert found.tag == "v1.0.0"
    assert found.prerelease is False
    assert found.published == datetime(2026, 11, 9, 15, 19, tzinfo=UTC)
    assert found.body == "Some *news*."
    assert found.package_url == f"{BASE}/v1.0.0/reprintbuddy-v1.0.0.tar.gz"
    assert found.checksum_url == f"{BASE}/v1.0.0/reprintbuddy-v1.0.0.tar.gz.sha256"
    assert found.digest == "sha256:" + "a" * 64


def test_a_pre_release_marked_as_such_is_read():
    [found] = rl.parse_releases([release("v1.0.1-beta.1")])
    assert found.prerelease is True


@pytest.mark.parametrize(
    "item",
    [
        release("v1.0.0", draft=True),
        release("v1.0.1-beta.1", prerelease=False),  # the pattern says pre-release, GitHub does not
        release("v1.0.0", prerelease=True),  # GitHub says pre-release, the pattern does not
        release("v1.0.0", assets=[asset("reprintbuddy-v1.0.0.tar.gz", tag="v1.0.0")]),  # no checksum
        release("v1.0.0", assets=[asset("reprintbuddy-v1.0.0.tar.gz.sha256", tag="v1.0.0")]),  # no package
        release(
            "v1.0.0",
            assets=[
                asset("reprintbuddy-v1.0.0.tar.gz", tag="v1.0.0", state="starter"),  # an upload that broke off
                asset("reprintbuddy-v1.0.0.tar.gz.sha256", tag="v1.0.0"),
            ],
        ),
        release(
            "v1.0.0",
            assets=[  # GitHub renamed a file with odd characters
                asset("reprintbuddy.v1.0.0.tar.gz", tag="v1.0.0"),
                asset("reprintbuddy-v1.0.0.tar.gz.sha256", tag="v1.0.0"),
            ],
        ),
        release(
            "v1.0.0",
            assets=[
                asset("reprintbuddy-v1.0.0.tar.gz", url="https://elsewhere.invalid/reprintbuddy-v1.0.0.tar.gz"),
                asset("reprintbuddy-v1.0.0.tar.gz.sha256", tag="v1.0.0"),
            ],
        ),
        release(
            "v1.0.0",
            assets=[
                asset("reprintbuddy-v1.0.0.tar.gz", url="http://github.com/o/r/releases/download/v1.0.0/x"),
                asset("reprintbuddy-v1.0.0.tar.gz.sha256", tag="v1.0.0"),
            ],
        ),
        release("v1.0.0", assets="none"),
        release("nightly"),
        release("v1.0.0-rc.1"),
    ],
)
def test_an_incomplete_or_mismatched_release_is_passed_over(item):
    assert tags(rl.parse_releases([item, release("v0.9.0")])) == ["v0.9.0"]


def test_a_body_that_is_no_text_leaves_the_release_without_notes():
    [found] = rl.parse_releases([release("v1.0.0", body=5)])
    assert found.body is None
    [found] = rl.parse_releases([release("v1.0.0", body=None)])
    assert found.body is None


@pytest.mark.parametrize("payload", [{"message": "Not Found"}, "text", None, [1, 2], [{"tag_name": 5}]])
def test_an_answer_that_is_no_list_of_releases_is_unusable(payload):
    with pytest.raises(rl.ReleaseCheckError):
        rl.parse_releases(payload)


def test_newest_offered_follows_the_channel():
    found = rl.parse_releases([release("v1.0.0"), release("v1.0.1-beta.1")])
    assert rl.newest_offered(found, "stable").tag == "v1.0.0"
    assert rl.newest_offered(found, "prerelease").tag == "v1.0.1-beta.1"
    found = rl.parse_releases([release("v1.0.1-beta.2"), release("v1.0.1")])
    assert rl.newest_offered(found, "stable").tag == "v1.0.1"
    assert rl.newest_offered(found, "prerelease").tag == "v1.0.1"
    assert rl.newest_offered([], "stable") is None
    assert rl.newest_offered(rl.parse_releases([release("v1.0.1-beta.1")]), "stable") is None


def test_one_request_to_the_release_list_without_a_login():
    http = answering(release("v1.0.0"))
    assert tags(rl.fetch_releases(("owner", "repo"), http)) == ["v1.0.0"]
    [(url, headers)] = http.calls
    assert url == "https://api.github.com/repos/owner/repo/releases?per_page=30"
    assert headers["User-Agent"].startswith("Reprintbuddy/")
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["Accept-Encoding"] == "identity"
    assert not any(name.lower() in ("authorization", "cookie") for name in headers)


@pytest.mark.parametrize(
    "http, reason",
    [
        (FakeHttp(status=403), "HTTP 403"),  # GitHub's limit
        (FakeHttp(status=404), "HTTP 404"),
        (FakeHttp(status=301), "HTTP 301"),  # a redirect is not followed
        (FakeHttp(body=b"{not json"), "no JSON"),
        (FakeHttp(body=json.dumps({"message": "x"}).encode()), "no list"),
        (FakeHttp(body=b"[" + b" " * 1_000_001 + b"]"), "answer too large"),
    ],
)
def test_a_failure_is_a_failed_check_without_github_text(http, reason):
    with pytest.raises(rl.ReleaseCheckError) as raised:
        rl.fetch_releases(("owner", "repo"), http)
    assert str(raised.value) == reason


def test_a_failed_check_is_an_update_check_error():
    """The pages catch `UpdateCheckError` for a failed check; this one is the same to them."""
    from reprintbuddy.ops.versions import UpdateCheckError

    assert issubclass(rl.ReleaseCheckError, UpdateCheckError)


# --- the check (the button and the automatic check) ----------------------------------------------

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def package_installation(tmp_path, tag="v1.0.0"):
    from reprintbuddy.ops.installation import write_marker

    folder = tmp_path / "opt"
    folder.mkdir()
    write_marker(folder, tag=tag, repo=("owner", "repo"), sha256="0" * 64)
    return folder


def test_check_offers_the_newest_with_its_notes_in_one_request(tmp_path, fake_runner):
    http = answering(release("v1.1.0", body="New."), release("v1.0.1"), release("v1.0.0"))
    info = rl.check_versions(package_installation(tmp_path), fake_runner, http, channel="stable", now=NOW)
    assert (info.installed, info.newest, info.checked_at) == ("v1.0.0", "v1.1.0", NOW)
    assert info.update_available
    assert [entry.tag for entry in info.notes.entries] == ["v1.1.0", "v1.0.1"]
    assert info.notes.page_url == "https://github.com/owner/repo/releases"
    assert len(http.calls) == 1
    assert fake_runner.calls == []


def test_check_up_to_date_has_no_notes(tmp_path, fake_runner):
    info = rl.check_versions(package_installation(tmp_path), fake_runner, answering(release("v1.0.0")), channel="stable", now=NOW)
    assert not info.update_available
    assert info.notes is None


def test_check_offers_pre_releases_only_on_their_channel(tmp_path, fake_runner):
    http = answering(release("v1.0.1-beta.1"), release("v1.0.0"))
    folder = package_installation(tmp_path)
    assert rl.check_versions(folder, fake_runner, http, channel="stable", now=NOW).newest == "v1.0.0"
    found = rl.check_versions(folder, fake_runner, http, channel="prerelease", now=NOW)
    assert found.newest == "v1.0.1-beta.1"
    assert [entry.prerelease for entry in found.notes.entries] == [True]


def test_check_keeps_an_installed_pre_release_on_the_stable_channel(tmp_path, fake_runner):
    """Spec 11.3a: back on "stable", nothing older is offered; the next newer stable release is."""
    folder = package_installation(tmp_path, tag="v1.0.1-beta.2")
    info = rl.check_versions(folder, fake_runner, answering(release("v1.0.1-beta.2"), release("v1.0.0")), channel="stable", now=NOW)
    assert (info.newest, info.update_available) == ("v1.0.0", False)
    info = rl.check_versions(folder, fake_runner, answering(release("v1.0.1"), release("v1.0.1-beta.2")), channel="stable", now=NOW)
    assert (info.newest, info.update_available) == ("v1.0.1", True)


def test_check_without_installation_asks_nobody(tmp_path, fake_runner):
    http = answering(release("v1.0.0"))
    with pytest.raises(rl.ReleaseCheckError):
        rl.check_versions(tmp_path / "none", fake_runner, http, channel="stable", now=NOW)
    assert http.calls == []


def test_check_of_a_git_copy_from_elsewhere_asks_nobody(tmp_path, fake_runner):
    from reprintbuddy.ops.runner import CommandResult

    folder = tmp_path / "opt"
    (folder / ".git").mkdir(parents=True)
    fake_runner.on(("git", "-C", str(folder), "describe"), CommandResult(0, "v0.4.9\n"))
    fake_runner.on(("git", "-C", str(folder), "remote", "get-url", "origin"), CommandResult(0, "https://example.org/x.git\n"))
    http = answering(release("v1.0.0"))
    with pytest.raises(rl.ReleaseCheckError):
        rl.check_versions(folder, fake_runner, http, channel="stable", now=NOW)
    assert http.calls == []


def test_check_of_a_git_copy_without_tag_offers_the_newest_without_notes(tmp_path, fake_runner):
    from reprintbuddy.ops.runner import CommandResult

    folder = tmp_path / "opt"
    (folder / ".git").mkdir(parents=True)
    fake_runner.on(("git", "-C", str(folder), "describe"), CommandResult(128))
    fake_runner.on(("git", "-C", str(folder), "remote", "get-url", "origin"), CommandResult(0, "https://github.com/owner/repo.git\n"))
    info = rl.check_versions(folder, fake_runner, answering(release("v1.0.0")), channel="stable", now=NOW)
    assert (info.installed, info.newest, info.update_available, info.notes) == (None, "v1.0.0", True, None)
