"""v0.3.13 p143: the release notes of the newer versions (Spec 11.3a "Neuerungen").

Never against the real GitHub: every test hands in a fake HTTP function.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reprintbuddy.ops import releasenotes as rn
from reprintbuddy.ops.runner import CommandResult
from reprintbuddy.ops.versions import VersionInfo

INSTALL = Path("/opt/app")


def release(tag, body="Notes.", *, draft=False, prerelease=False, published="2026-11-09T15:19:00Z"):
    return {"tag_name": tag, "body": body, "draft": draft, "prerelease": prerelease, "published_at": published}


class FakeHttp:
    """Answers every GET with one scripted reply (a status and a body) or raises."""

    def __init__(self, status=200, body=b"[]", error: Exception | None = None):
        self.status, self.body, self.error = status, body, error
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        if self.error is not None:
            raise self.error
        return self.status, self.body


def answering(*releases) -> FakeHttp:
    return FakeHttp(body=json.dumps(list(releases)).encode())


def runner_with_origin(url: str | None):
    def runner(command, timeout=None):
        if command[-3:] == ("remote", "get-url", "origin"):
            return CommandResult(0, f"{url}\n") if url is not None else CommandResult(2, "", "No such remote")
        return CommandResult(0)

    return runner


# --- which repository ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/owner/repo.git", ("owner", "repo")),
        ("https://github.com/owner/repo", ("owner", "repo")),
        ("https://github.com/owner/repo/", ("owner", "repo")),
        ("git@github.com:owner/my.repo.git", ("owner", "my.repo")),
        ("ssh://git@github.com/owner/repo.git", ("owner", "repo")),
        ("https://GitHub.com/Owner-1/Repo_2.git", ("Owner-1", "Repo_2")),
        ("https://gitlab.example/owner/repo.git", None),  # not GitHub: no notes
        ("https://github.com.evil.example/owner/repo.git", None),
        ("https://user:secret@github.com/owner/repo.git", None),  # never send what looks like a login on
        ("https://github.com/owner", None),
        ("https://github.com/owner/repo/extra", None),
        ("https://github.com/../repo", None),
        ("https://github.com/owner/..", None),
        ("/srv/mirror/repo.git", None),
        ("", None),
    ],
)
def test_github_repo(url, expected):
    assert rn.github_repo(url) == expected


# --- which notes -------------------------------------------------------------------------------


def tags(notes):
    return [entry.tag for entry in notes]


def test_only_the_versions_after_the_installed_up_to_the_newest_newest_first():
    releases = [release(t) for t in ("v0.1.0", "v0.3.0", "v0.2.0", "v0.4.0", "v0.1.1", "v0.5.0")]
    assert tags(rn.select_notes(releases, installed="v0.1.0", newest="v0.4.0")) == ["v0.4.0", "v0.3.0", "v0.2.0", "v0.1.1"]


def test_drafts_prereleases_and_foreign_tags_are_left_out():
    releases = [
        release("v0.2.0", draft=True),
        release("v0.3.0", prerelease=True),
        release("v0.4.0-beta"),
        release("nightly"),
        release("v0.5.0"),
    ]
    assert tags(rn.select_notes(releases, installed="v0.1.0", newest="v0.5.0")) == ["v0.5.0"]


def test_at_most_ten_versions_the_newest_ones():
    releases = [release(f"v0.1.{n}") for n in range(1, 16)]
    assert tags(rn.select_notes(releases, installed="v0.1.0", newest="v0.1.15")) == [f"v0.1.{n}" for n in range(15, 5, -1)]


def test_a_long_note_is_cut():
    entry = rn.select_notes([release("v0.2.0", "x" * 25_000)], installed="v0.1.0", newest="v0.2.0")[0]
    assert len(entry.body) == rn.MAX_NOTE_CHARS
    assert entry.cut is True


def test_date_and_body_are_taken_over():
    entry = rn.select_notes([release("v0.2.0", "Some *news*.")], installed="v0.1.0", newest="v0.2.0")[0]
    assert entry.published == datetime(2026, 11, 9, 15, 19, tzinfo=UTC)  # the page shows it in local time
    assert entry.body == "Some *news*."
    assert entry.cut is False


@pytest.mark.parametrize("item", [
    {"tag_name": "v0.2.0", "body": None},  # GitHub sends null for an empty body
    {"tag_name": "v0.2.0"},
    {"tag_name": "v0.2.0", "body": "x", "published_at": "not a date"},
    {"tag_name": "v0.2.0", "body": "x", "published_at": None},
])
def test_missing_parts_of_one_release_are_tolerated(item):
    entry = rn.select_notes([item], installed="v0.1.0", newest="v0.2.0")[0]
    assert entry.tag == "v0.2.0"


@pytest.mark.parametrize("payload", [{"message": "Not Found"}, "text", None, [1, 2], [{"tag_name": 5}]])
def test_an_answer_that_is_no_list_of_releases_is_unusable(payload):
    with pytest.raises(rn.NotesError):
        rn.select_notes(payload, installed="v0.1.0", newest="v0.2.0")


# --- fetching ------------------------------------------------------------------------------------


def fetch(http, origin="https://github.com/owner/repo.git", installed="v0.1.0", newest="v0.2.0"):
    return rn.fetch_notes(INSTALL, runner_with_origin(origin), http, installed=installed, newest=newest)


def test_one_request_to_the_releases_of_the_origin_without_a_login():
    http = answering(release("v0.2.0"))
    notes = fetch(http)
    assert notes.failed is False
    assert tags(notes.entries) == ["v0.2.0"]
    assert notes.page_url == "https://github.com/owner/repo/releases"
    [(url, headers)] = http.calls
    assert url == "https://api.github.com/repos/owner/repo/releases?per_page=30"  # review: 10 are shown
    assert headers["User-Agent"].startswith("Reprintbuddy/")
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["Accept-Encoding"] == "identity"  # review: the size limit counts what arrives
    assert not any(name.lower() in ("authorization", "cookie") for name in headers)


@pytest.mark.parametrize("http", [
    FakeHttp(status=403),  # GitHub's limit
    FakeHttp(status=429),
    FakeHttp(status=404),
    FakeHttp(status=500),
    FakeHttp(status=301),  # a redirect is not followed
    FakeHttp(body=b"{not json"),
    FakeHttp(body=json.dumps({"message": "x"}).encode()),
    FakeHttp(error=rn.NotesError("too large")),
    FakeHttp(error=TimeoutError()),
    FakeHttp(error=OSError("unreachable")),
])
def test_a_failure_gives_failed_notes_with_the_fixed_link(http):
    notes = fetch(http)
    assert notes.failed is True
    assert notes.entries == ()
    assert notes.page_url == "https://github.com/owner/repo/releases"


def test_no_release_for_the_newer_versions_counts_as_failed():
    notes = fetch(answering(release("v0.1.0")))
    assert notes.failed is True


def test_a_newest_tag_without_its_release_yet_counts_as_failed():
    """Review: the card would say "up to v0.3.0" and list only v0.2.0."""
    notes = fetch(answering(release("v0.2.0")), newest="v0.3.0")
    assert notes.failed is True


def test_the_whole_request_has_ten_seconds(monkeypatch):
    """Review: httpx's timeouts count per step; the request as a whole is watched."""
    import threading

    import httpx

    release_handler = threading.Event()

    def slow(request):
        release_handler.wait(5)  # a server that answers late, step by step
        return httpx.Response(200, content=b"[]")

    monkeypatch.setattr(rn, "TIMEOUT", 0.2)
    started = time.monotonic()
    with pytest.raises(rn.NotesError):
        rn.httpx_get("https://api.github.com/x", {}, transport=httpx.MockTransport(slow))
    assert time.monotonic() - started < 2
    release_handler.set()


def test_a_body_over_the_limit_stops_the_download(monkeypatch):
    import httpx

    monkeypatch.setattr(rn, "MAX_RESPONSE_BYTES", 10)
    big = httpx.MockTransport(lambda request: httpx.Response(200, content=iter([b"x" * 50, b"x" * 50])))
    with pytest.raises(rn.NotesError):
        rn.httpx_get("https://api.github.com/x", {}, transport=big)


def test_a_redirect_is_not_followed():
    import httpx

    calls = []

    def moved(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://elsewhere.invalid/"})

    assert rn.httpx_get("https://api.github.com/x", {}, transport=httpx.MockTransport(moved))[0] == 302
    assert calls == ["https://api.github.com/x"]


@pytest.mark.parametrize("origin", ["https://gitlab.example/owner/repo.git", None])
def test_an_origin_that_is_no_github_repository_asks_nobody(origin):
    http = answering(release("v0.2.0"))
    notes = fetch(http, origin=origin)
    assert notes.failed is True
    assert notes.page_url is None
    assert http.calls == []


def test_the_reason_goes_to_the_log_but_not_the_answer(caplog):
    caplog.set_level("INFO", logger="reprintbuddy.ops.releasenotes")
    fetch(FakeHttp(status=403, body=b"SECRET-BODY"))
    assert "403" in caplog.text
    assert "SECRET-BODY" not in caplog.text


# --- together with the check --------------------------------------------------------------------


def info(installed, newest):
    return VersionInfo(installed=installed, newest=newest, checked_at=datetime(2026, 11, 9, tzinfo=UTC))


def test_notes_are_fetched_only_when_a_newer_release_exists():
    http = answering(release("v0.2.0"))
    assert rn.with_notes(info("v0.2.0", "v0.2.0"), INSTALL, runner_with_origin("https://github.com/o/r"), http).notes is None
    assert http.calls == []
    found = rn.with_notes(info("v0.1.0", "v0.2.0"), INSTALL, runner_with_origin("https://github.com/o/r"), http)
    assert tags(found.notes.entries) == ["v0.2.0"]
    assert found.update_available


def test_a_development_build_gets_no_notes():
    http = answering(release("v0.2.0"))
    assert rn.with_notes(info(None, "v0.2.0"), INSTALL, runner_with_origin("https://github.com/o/r"), http).notes is None
    assert http.calls == []


def test_an_error_while_fetching_never_breaks_the_check():
    def broken(url, headers):
        raise RuntimeError("anything")

    found = rn.with_notes(info("v0.1.0", "v0.2.0"), INSTALL, runner_with_origin("https://github.com/o/r"), broken)
    assert found.newest == "v0.2.0" and found.update_available
    assert found.notes.failed is True


# --- v0.4.9: the notes come from the release list of the check (Spec 11.3a "Neuerungen") ---------


def listed(*items):
    from reprintbuddy.ops import releases as rl

    full = []
    for tag, body in items:
        prerelease = "-beta." in tag
        full.append({
            "tag_name": tag, "body": body, "draft": False, "prerelease": prerelease,
            "published_at": "2026-11-09T15:19:00Z",
            "assets": [
                {"name": rl.package_name(tag), "browser_download_url": f"https://github.com/o/r/releases/download/{tag}/{rl.package_name(tag)}"},
                {"name": rl.checksum_name(tag), "browser_download_url": f"https://github.com/o/r/releases/download/{tag}/{rl.checksum_name(tag)}"},
            ],
        })
    return rl.parse_releases(full)


def test_notes_for_leaves_pre_releases_out_on_the_stable_channel():
    found = listed(("v0.2.0", "A"), ("v0.3.0-beta.1", "B"), ("v0.3.0", "C"))
    notes = rn.notes_for(found, installed="v0.1.0", newest="v0.3.0", channel="stable", page_url="P")
    assert tags(notes.entries) == ["v0.3.0", "v0.2.0"]
    assert [entry.prerelease for entry in notes.entries] == [False, False]
    assert notes.failed is False
    assert notes.page_url == "P"


def test_notes_for_marks_pre_releases_on_their_channel():
    found = listed(("v0.2.0", "A"), ("v0.3.0-beta.1", "B"), ("v0.3.0-beta.2", "C"))
    notes = rn.notes_for(found, installed="v0.1.0", newest="v0.3.0-beta.2", channel="prerelease", page_url="P")
    assert tags(notes.entries) == ["v0.3.0-beta.2", "v0.3.0-beta.1", "v0.2.0"]
    assert [entry.prerelease for entry in notes.entries] == [True, True, False]


def test_notes_for_keeps_ten_and_cuts_long_ones():
    found = listed(*[(f"v0.1.{n}", "x" * 25_000) for n in range(1, 16)])
    notes = rn.notes_for(found, installed="v0.1.0", newest="v0.1.15", channel="stable", page_url="P")
    assert tags(notes.entries) == [f"v0.1.{n}" for n in range(15, 5, -1)]
    assert all(entry.cut and len(entry.body) == rn.MAX_NOTE_CHARS for entry in notes.entries)


def test_notes_for_fails_only_when_the_newest_has_no_usable_text():
    notes = rn.notes_for(listed(("v0.2.0", "A"), ("v0.3.0", None)), installed="v0.1.0", newest="v0.3.0", channel="stable", page_url="P")
    assert notes.failed is True
    assert notes.page_url == "P"
    notes = rn.notes_for(listed(("v0.2.0", None), ("v0.3.0", "C")), installed="v0.1.0", newest="v0.3.0", channel="stable", page_url="P")
    assert notes.failed is False
    assert [entry.body for entry in notes.entries] == ["C", ""]
