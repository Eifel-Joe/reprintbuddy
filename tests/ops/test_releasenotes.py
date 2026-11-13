"""v0.3.13 p143: the release notes of the newer versions (Spec 11.3a "Neuerungen").

Never against the real GitHub: every test hands in a fake HTTP function.
"""

from __future__ import annotations

import time

import pytest

from reprintbuddy.ops import releasenotes as rn


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


def tags(notes):
    return [entry.tag for entry in notes]


# --- the request ------------------------------------------------------------------------------------


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
