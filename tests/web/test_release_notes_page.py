"""v0.3.13 p143: the update card shows what the newer versions bring (Spec 11.3a "Neuerungen").

GitHub is a fake HTTP function on `OpsContext.http_get`; it counts every request, so the tests show
that only the check asks and a page view never does.
"""

from __future__ import annotations

import dataclasses
import json
import re
from datetime import date

import pytest
from conftest import sm_of
from test_ops_update import REMOTE_ONE, check, git_env
from test_update_banner import banner, check_now

from reprintbuddy import settings as st
from reprintbuddy.ops.runner import CommandResult
from reprintbuddy.web.formatting import format_date
from reprintbuddy.web.i18n import translate

ORIGIN = "https://github.com/owner/repo.git\n"
PAGE_URL = "https://github.com/owner/repo/releases"
NOTES = [
    {"tag_name": "v0.2.0", "body": "New **thing** and <script>alert(1)</script>.", "draft": False,
     "prerelease": False, "published_at": "2026-11-09T15:19:00Z"},
    {"tag_name": "v0.1.0", "body": "Old.", "draft": False, "prerelease": False, "published_at": "2026-11-05T10:00:00Z"},
]


class FakeGitHub:
    def __init__(self, status=200, releases=NOTES):
        self.status = status
        self.body = json.dumps(releases).encode()
        self.calls: list[str] = []

    def __call__(self, url, headers):
        self.calls.append(url)
        return self.status, self.body


def with_github(env, github=None, origin=ORIGIN):
    github = github or FakeGitHub()
    app = env.client.app
    app.state.ops = dataclasses.replace(app.state.ops, http_get=github)
    git_env(env)
    env.runner.on(("git", "-C", str(env.install_dir), "remote", "get-url", "origin"), CommandResult(0, origin))
    return github


def maintenance(env):
    return env.client.get("/settings?tab=maintenance").text


def notes_block(page: str) -> str | None:
    found = re.search(r'<details class="release-notes" id="ops-notes"[^>]*>(.*?)</details>', page, re.S)
    return found.group(0) if found else None


def test_the_button_check_brings_the_notes_onto_the_card(ops_env):
    github = with_github(ops_env)
    check(ops_env)
    assert len(github.calls) == 1
    block = notes_block(maintenance(ops_env))
    assert block is not None
    assert " open" not in block.split(">", 1)[0]  # closed at first
    assert "v0.2.0" in block and format_date(date(2026, 11, 9), "en") in block
    assert "<strong>thing</strong>" in block
    assert "&lt;script&gt;" in block and "<script>alert" not in block
    assert "Old." not in block  # the installed version's notes are not news
    assert f'href="{PAGE_URL}"' in block
    assert translate("en", "ops.notes.all_link") in block


async def test_the_automatic_check_brings_them_too(ops_env):
    github = with_github(ops_env)
    await check_now(ops_env)
    assert len(github.calls) == 1
    assert "<strong>thing</strong>" in notes_block(maintenance(ops_env))


def test_a_page_view_never_asks_github(ops_env):
    github = with_github(ops_env)
    for path in ("/", "/settings?tab=maintenance", "/settings"):
        ops_env.client.get(path)
    assert github.calls == []  # nothing checked yet: nothing asked
    check(ops_env)
    for path in ("/", "/settings?tab=maintenance", "/settings", "/articles"):
        ops_env.client.get(path)
    assert len(github.calls) == 1  # only the check asked


def test_nothing_newer_no_notes_and_no_request(ops_env):
    github = with_github(ops_env)
    ops_env.runner.on(("git", "-C", str(ops_env.install_dir), "ls-remote"), REMOTE_ONE)
    check(ops_env)
    assert github.calls == []
    assert notes_block(maintenance(ops_env)) is None


@pytest.mark.parametrize("status", [403, 429, 500])
def test_a_failure_keeps_the_version_and_the_button_and_says_so(ops_env, status):
    with_github(ops_env, FakeGitHub(status=status))
    check(ops_env)
    page = maintenance(ops_env)
    block = notes_block(page)
    assert translate("en", "ops.notes.failed") in block
    assert f'href="{PAGE_URL}"' in block
    assert 'action="/settings/update"' in page  # the update never depends on the notes


def test_without_a_github_origin_no_request_and_no_link(ops_env):
    github = with_github(ops_env, origin="https://git.example.invalid/owner/repo.git\n")
    check(ops_env)
    assert github.calls == []
    block = notes_block(maintenance(ops_env))
    assert translate("en", "ops.notes.failed") in block
    assert "<a " not in block


def test_a_cut_note_is_marked(ops_env):
    long = [dict(NOTES[0], body="x" * 25_000)]
    with_github(ops_env, FakeGitHub(releases=long))
    check(ops_env)
    assert translate("en", "ops.notes.cut") in notes_block(maintenance(ops_env))


def test_german_page_says_the_notes_are_english_only(ops_env):
    with_github(ops_env)
    check(ops_env)
    assert translate("en", "ops.notes.english_only") not in notes_block(maintenance(ops_env))
    with sm_of(ops_env.client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    block = notes_block(maintenance(ops_env))
    assert "Die Versionshinweise gibt es nur auf Englisch." in block
    assert "<strong>thing</strong>" in block  # the notes themselves stay English


def test_the_bar_leads_to_the_notes(ops_env):
    with_github(ops_env)
    check(ops_env)
    assert 'href="/settings?tab=maintenance#ops-notes"' in banner(ops_env.client.get("/").text)
