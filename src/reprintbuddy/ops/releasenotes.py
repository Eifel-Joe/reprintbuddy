"""What the newer versions bring: their release notes from GitHub (v0.3.13 p143, Spec 11.3a "Neuerungen").

Since v0.4.9 the check itself reads GitHub's release list once, without a login (`ops/releases.py`); the
notes of the versions after the installed one up to the newest come from that same answer
(`notes_for`), newest first, at most `MAX_VERSIONS`. A page view never asks: the result rides on
`VersionInfo.notes` and lives in memory only. This module keeps the request itself (`httpx_get`), its
limits and headers, and the reading of a GitHub address.

The notes are text from outside. Nothing here turns them into HTML; the page renders them escaped
(`web/notes_markdown.py`). The request sends no cookie, no token and no address from the installation,
only a neutral User-Agent. A redirect is not followed. A failed request is a failed check now; the notes
alone fail (`failed`) only when the newest version has no usable text. The update never depends on them.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from reprintbuddy import __version__
from reprintbuddy.ops.versions import is_newer, parse_version

if TYPE_CHECKING:
    from reprintbuddy.ops.releases import Channel, Release

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
    published: datetime | None  # UTC; the page shows the local day
    body: str  # Markdown as GitHub keeps it; rendered escaped by the page
    cut: bool = False
    prerelease: bool = False  # v0.4.9: marked "pre-release" on the card


@dataclass(frozen=True)
class ReleaseNotes:
    entries: tuple[ReleaseNote, ...] = ()
    page_url: str | None = None  # GitHub's page of all releases; None without a GitHub origin
    failed: bool = False


def api_headers() -> dict[str, str]:
    """The headers of every request to GitHub: no cookie, no token, no address of the installation."""
    return {
        "Accept": "application/vnd.github+json",
        "Accept-Encoding": "identity",
        "User-Agent": f"Reprintbuddy/{__version__}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


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


def httpx_get(url: str, headers: Mapping[str, str], *, transport: Any = None) -> tuple[int, bytes]:
    """The production GET: TLS checked, no redirect followed, at most `TIMEOUT` seconds and
    `MAX_RESPONSE_BYTES` in all. `transport` is for the tests only.

    Review of v0.3.13: httpx's timeouts count per step (connect, each read), so a server that answers
    slowly step by step could hold the check far longer. The request runs in its own daemon thread and
    is given up after `TIMEOUT` seconds as a whole; the thread itself ends by httpx's step timeouts.
    The size is counted on the raw bytes (the request asks for no compression), so a small packed
    answer cannot grow in memory before the limit is seen. Tests: test_releasenotes.py "review".
    """
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["result"] = _get(url, headers, transport)
        except BaseException as error:  # noqa: BLE001 - handed to the waiting thread
            outcome["error"] = error

    worker = threading.Thread(target=run, name="release-notes", daemon=True)
    worker.start()
    worker.join(TIMEOUT)
    if worker.is_alive():
        raise NotesError("timeout")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


def _get(url: str, headers: Mapping[str, str], transport: Any) -> tuple[int, bytes]:
    import httpx

    deadline = time.monotonic() + TIMEOUT
    options: dict[str, Any] = {"timeout": httpx.Timeout(TIMEOUT), "follow_redirects": False, "trust_env": False}
    if transport is not None:
        options["transport"] = transport
    with httpx.Client(**options) as client:
        with client.stream("GET", url, headers=dict(headers)) as response:
            if response.status_code != 200:
                return response.status_code, b""
            data = bytearray()
            for chunk in response.iter_raw():
                data += chunk
                if len(data) > MAX_RESPONSE_BYTES:
                    raise NotesError("answer too large")
                if time.monotonic() > deadline:
                    raise NotesError("timeout")
            return response.status_code, bytes(data)


def published_at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def notes_for(
    releases: "list[Release]", *, installed: str, newest: str, channel: "Channel", page_url: str | None
) -> ReleaseNotes:
    """v0.4.9: the notes of the versions after `installed` up to `newest`, from the release list the check
    already has (one request for both, Spec 11.3a). Pre-releases only on their channel, marked; the newest
    first, at most `MAX_VERSIONS`, each cut at `MAX_NOTE_CHARS`. `failed` only when the newest has no
    usable text: the card then shows the version with the fixed link, as before."""
    found: list[tuple[Any, ReleaseNote]] = []
    newest_has_text = False
    for item in releases:
        if item.prerelease and channel != "prerelease":
            continue
        if not is_newer(item.tag, installed) or is_newer(item.tag, newest):
            continue
        if item.tag == newest:
            newest_has_text = item.body is not None
        body = item.body or ""
        cut = len(body) > MAX_NOTE_CHARS
        note = ReleaseNote(item.tag, item.published, body[:MAX_NOTE_CHARS], cut, item.prerelease)
        found.append((parse_version(item.tag), note))
    found.sort(key=lambda pair: pair[0], reverse=True)
    entries = tuple(note for _, note in found[:MAX_VERSIONS])
    if not newest_has_text:
        return ReleaseNotes(entries=entries, page_url=page_url, failed=True)
    return ReleaseNotes(entries=entries, page_url=page_url)
