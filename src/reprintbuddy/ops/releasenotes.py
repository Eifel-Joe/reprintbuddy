"""What the newer versions bring: their release notes from GitHub (v0.3.13 p143, Spec 11.3a "Neuerungen").

When the check finds a newer release, the same round asks GitHub's REST API once, without a login, for
the releases of the repository the installation comes from (`origin`). Only the notes of the versions
after the installed one up to the newest are kept, newest first, at most `MAX_VERSIONS`. A page view
never asks: the result rides on `VersionInfo.notes` and lives in memory only.

The notes are text from outside. Nothing here turns them into HTML; the page renders them escaped
(`web/notes_markdown.py`). The request sends no cookie, no token and no address from the installation,
only a neutral User-Agent. A redirect is not followed. Any failure (network, GitHub's limit of 60
requests an hour, no GitHub origin, an unusable answer) gives `failed` notes; the check itself and the
update never depend on them. The reason goes to the log at INFO, never GitHub's answer text.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from reprintbuddy import __version__
from reprintbuddy.ops.runner import CommandRunner
from reprintbuddy.ops.versions import VersionInfo, is_newer, parse_release_tag

log = logging.getLogger(__name__)

API = "https://api.github.com"
TIMEOUT = 10.0  # seconds for the whole request (Spec 11.3a)
MAX_RESPONSE_BYTES = 1_000_000  # Spec 11.3a: "Antwort höchstens 1 MB"
MAX_NOTE_CHARS = 20_000  # per version; longer notes are cut and marked
MAX_VERSIONS = 10

# The parts of a GitHub address. GitHub's own rules for names are narrower; this is only what may
# go into the API path unescaped. "." and ".." are refused separately.
_NAME = r"[A-Za-z0-9_.-]+"
_ORIGINS = (
    re.compile(rf"^https://github\.com/(?P<owner>{_NAME})/(?P<repo>{_NAME}?)(?:\.git)?/?$", re.IGNORECASE),
    re.compile(rf"^git@github\.com:(?P<owner>{_NAME})/(?P<repo>{_NAME}?)(?:\.git)?/?$", re.IGNORECASE),
    re.compile(rf"^ssh://git@github\.com/(?P<owner>{_NAME})/(?P<repo>{_NAME}?)(?:\.git)?/?$", re.IGNORECASE),
)

# One GET: (url, headers) -> (status, body). Raises on a network failure, a timeout or a body over
# the limit. Handed in (`OpsContext.http_get`) so the tests never reach the network.
HttpGet = Callable[[str, Mapping[str, str]], tuple[int, bytes]]


class NotesError(Exception):
    """The answer could not be had or not be used."""


@dataclass(frozen=True)
class ReleaseNote:
    tag: str
    published: date | None
    body: str  # Markdown as GitHub keeps it; rendered escaped by the page
    cut: bool = False


@dataclass(frozen=True)
class ReleaseNotes:
    entries: tuple[ReleaseNote, ...] = ()
    page_url: str | None = None  # GitHub's page of all releases; None without a GitHub origin
    failed: bool = False


def github_repo(url: str) -> tuple[str, str] | None:
    """(owner, repo) of a GitHub address of `origin`, else None (another host, a local path, a login in it)."""
    for pattern in _ORIGINS:
        found = pattern.match(url.strip())
        if found:
            owner, repo = found["owner"], found["repo"]
            if repo.lower().endswith(".git"):
                repo = repo[:-4]
            if not repo or owner in (".", "..") or repo in (".", ".."):
                return None
            return owner, repo
    return None


def releases_page(owner: str, repo: str) -> str:
    return f"https://github.com/{owner}/{repo}/releases"


def httpx_get(url: str, headers: Mapping[str, str]) -> tuple[int, bytes]:
    """The production GET: TLS checked, no redirect followed, at most `TIMEOUT` seconds and
    `MAX_RESPONSE_BYTES` in all (httpx's timeouts count per step, so the whole is watched here)."""
    import httpx

    deadline = time.monotonic() + TIMEOUT
    with httpx.Client(timeout=httpx.Timeout(TIMEOUT), follow_redirects=False) as client:
        with client.stream("GET", url, headers=dict(headers)) as response:
            if response.status_code != 200:
                return response.status_code, b""
            data = bytearray()
            for chunk in response.iter_bytes():
                data += chunk
                if len(data) > MAX_RESPONSE_BYTES:
                    raise NotesError("answer too large")
                if time.monotonic() > deadline:
                    raise NotesError("timeout")
            return response.status_code, bytes(data)


def _published(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def select_notes(releases: Any, *, installed: str, newest: str) -> tuple[ReleaseNote, ...]:
    """The notes of the releases after `installed` up to `newest`, newest first, at most `MAX_VERSIONS`.

    Drafts, pre-releases and tags that are no release (`vX.Y.Z`) are left out. A long body is cut.
    An answer that is no list of releases raises `NotesError`.
    """
    if not isinstance(releases, list):
        raise NotesError("no list")
    found: list[tuple[tuple[int, int, int], ReleaseNote]] = []
    for item in releases:
        if not isinstance(item, dict) or not isinstance(item.get("tag_name"), str):
            raise NotesError("not a release")
        tag = item["tag_name"]
        number = parse_release_tag(tag)
        if number is None or item.get("draft") is True or item.get("prerelease") is True:
            continue
        if not is_newer(tag, installed) or is_newer(tag, newest):
            continue
        body = item.get("body")
        body = body if isinstance(body, str) else ""
        cut = len(body) > MAX_NOTE_CHARS
        found.append((number, ReleaseNote(tag, _published(item.get("published_at")), body[:MAX_NOTE_CHARS], cut)))
    found.sort(key=lambda pair: pair[0], reverse=True)
    return tuple(note for _, note in found[:MAX_VERSIONS])


def _origin(install_dir: Path, runner: CommandRunner) -> str:
    result = runner(("git", "-C", str(install_dir), "remote", "get-url", "origin"), timeout=10.0)
    return result.stdout.strip() if result.ok else ""


def fetch_notes(
    install_dir: Path, runner: CommandRunner, http_get: HttpGet, *, installed: str, newest: str
) -> ReleaseNotes:
    """Ask GitHub once for the notes between `installed` and `newest`. Never raises."""
    repo = github_repo(_origin(install_dir, runner))
    if repo is None:
        log.info("release notes: origin is no GitHub repository")
        return ReleaseNotes(failed=True)
    page = releases_page(*repo)
    url = f"{API}/repos/{repo[0]}/{repo[1]}/releases?per_page=100"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"Reprintbuddy/{__version__}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        status, body = http_get(url, headers)
        if status != 200:
            raise NotesError(f"HTTP {status}")
        if len(body) > MAX_RESPONSE_BYTES:
            raise NotesError("answer too large")
        entries = select_notes(json.loads(body), installed=installed, newest=newest)
        if not entries:
            raise NotesError("no release notes for the newer versions")
    except NotesError as error:
        log.info("release notes could not be loaded: %s", error)
        return ReleaseNotes(page_url=page, failed=True)
    except Exception as error:  # noqa: BLE001 - the network, the JSON, anything: the check goes on
        log.info("release notes could not be loaded: %s", type(error).__name__)
        return ReleaseNotes(page_url=page, failed=True)
    return ReleaseNotes(entries=entries, page_url=page)


def with_notes(info: VersionInfo, install_dir: Path, runner: CommandRunner, http_get: HttpGet) -> VersionInfo:
    """`info` with the notes of the newer versions, when there is one and the installed version is a
    release (a development build has no place to start from). Never raises."""
    if not info.update_available or info.newest is None or parse_release_tag(info.installed or "") is None:
        return info
    try:
        notes = fetch_notes(install_dir, runner, http_get, installed=info.installed or "", newest=info.newest)
    except Exception as error:  # noqa: BLE001 - e.g. the runner itself: the version stays shown
        log.info("release notes could not be loaded: %s", type(error).__name__)
        notes = ReleaseNotes(failed=True)
    return dataclasses.replace(info, notes=notes)
