"""What GitHub offers: the release list of the repository, read with one request (v0.4.9 p500, Spec 11.3a).

Every version is a published GitHub release with its package (`reprintbuddy-<tag>.tar.gz`, the whole state
of the version) and a checksum file next to it. One request without a login
(`GET /repos/{owner}/{repo}/releases?per_page=30`) gives the versions, whether each is a pre-release, the
addresses of package and checksum, GitHub's own checksum of the package and the notes. The check of the
pages and the update helper both read it; neither talks git to the remote any more.

A release counts only when it is published (no draft), its pre-release mark agrees with its tag (`-beta.N`
exactly when GitHub marks it as a pre-release) and package and checksum are both there and fully uploaded.
Anything else is passed over and named in the log; nothing is guessed. The answer is text from outside:
only the fields named here are read, and nothing of GitHub's own answer text reaches a page or the log.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal

from pathlib import Path

from reprintbuddy.ops.installation import read_installation
from reprintbuddy.ops.releasenotes import (
    API,
    MAX_RESPONSE_BYTES,
    HttpGet,
    api_headers,
    notes_for,
    published_at,
    releases_page,
)
from reprintbuddy.ops.runner import CommandRunner
from reprintbuddy.ops.versions import UpdateCheckError, VersionInfo, is_newer, is_prerelease_tag, parse_version

log = logging.getLogger(__name__)

Channel = Literal["stable", "prerelease"]
CHANNELS: tuple[Channel, ...] = ("stable", "prerelease")
PER_PAGE = 30  # only the newest are needed; GitHub lists the newest first
DOWNLOAD_PREFIX = "https://github.com/"


class ReleaseCheckError(UpdateCheckError):
    """The release list could not be had or not be used; the message is a short reason of our own."""


@dataclass(frozen=True)
class Release:
    tag: str
    prerelease: bool
    published: datetime | None
    body: str | None  # Markdown as GitHub keeps it; None when it is no text
    package_url: str
    checksum_url: str
    digest: str | None  # GitHub's own checksum of the package, as GitHub writes it (format not documented)


# Spec 11.3a "Umbau nur angekündigt" (decided 14.11.2026): the notes of a pre-release whose database steps
# rework existing data begin with this line, word for word. The update card sees it and warns before the
# update; tools/check_release_notes.py compares it with the rework marks of the steps before a release.
REWORK_LINE = (
    "**Database rework:** going back to the stable version restores the backup from before this update; "
    "entries made since then are lost."
)


def starts_with_rework(body: str | None) -> bool:
    """Do these notes begin with the rework line? Blank lines, a byte order mark and the spaces around the
    first line do not count; anything else in front of it does."""
    for line in (body or "").lstrip("﻿").splitlines():
        if line.strip():
            return line.strip() == REWORK_LINE
    return False


def return_target(releases: list[Release], installed: str | None) -> Release | None:
    """The stable version an installed pre-release goes back to (Spec 11.3a "Rückweg zur stabilen Version"):
    the newest stable release older than it. None when a stable version is installed (or nothing says which),
    when there is none older, or when a newer stable release exists - then the ordinary update is the way
    back, without loss. The channel plays no part."""
    current = parse_version(installed) if installed else None
    if current is None or not is_prerelease_tag(installed):
        return None
    stable = [item for item in releases if not item.prerelease]
    if any(parse_version(item.tag) > current for item in stable):  # type: ignore[operator]
        return None
    older = [item for item in stable if parse_version(item.tag) < current]  # type: ignore[operator]
    return max(older, key=lambda item: parse_version(item.tag)) if older else None  # type: ignore[arg-type, return-value]


def package_name(tag: str) -> str:
    return f"reprintbuddy-{tag}.tar.gz"


def checksum_name(tag: str) -> str:
    return package_name(tag) + ".sha256"


def _uploaded(assets: Any, name: str) -> dict[str, Any] | None:
    """The asset called `name`, when it is fully uploaded and served from GitHub over HTTPS."""
    if not isinstance(assets, list):
        return None
    for item in assets:
        if not isinstance(item, dict) or item.get("name") != name:
            continue
        url = item.get("browser_download_url")
        if item.get("state", "uploaded") != "uploaded" or not isinstance(url, str) or not url.startswith(DOWNLOAD_PREFIX):
            return None
        return item
    return None


def parse_releases(data: Any) -> list[Release]:
    """The usable releases of an answer of the release list, in the order of the answer.

    An answer that is no list of releases raises `ReleaseCheckError`."""
    if not isinstance(data, list):
        raise ReleaseCheckError("no list")
    found: list[Release] = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("tag_name"), str):
            raise ReleaseCheckError("not a release")
        tag = item["tag_name"]
        if item.get("draft") is not False or parse_version(tag) is None:
            continue
        marked = item.get("prerelease") is True
        if marked != is_prerelease_tag(tag):
            log.info("release %s passed over: its pre-release mark does not agree with its tag", tag)
            continue
        package = _uploaded(item.get("assets"), package_name(tag))
        checksum = _uploaded(item.get("assets"), checksum_name(tag))
        if package is None or checksum is None:
            log.info("release %s passed over: package or checksum missing", tag)
            continue
        body = item.get("body")
        digest = package.get("digest")
        found.append(
            Release(
                tag=tag,
                prerelease=marked,
                published=published_at(item.get("published_at")),
                body=body if isinstance(body, str) else None,
                package_url=package["browser_download_url"],
                checksum_url=checksum["browser_download_url"],
                digest=digest if isinstance(digest, str) else None,
            )
        )
    return found


def newest_offered(releases: list[Release], channel: Channel) -> Release | None:
    """The newest release the channel offers: only stable releases, or pre-releases as well."""
    best: Release | None = None
    for item in releases:
        if item.prerelease and channel != "prerelease":
            continue
        if best is None or parse_version(item.tag) > parse_version(best.tag):  # type: ignore[operator]
            best = item
    return best


def fetch_releases(repo: tuple[str, str], http_get: HttpGet) -> list[Release]:
    """Ask GitHub once for the release list of `repo`. Raises `ReleaseCheckError` with a reason of our own."""
    url = f"{API}/repos/{repo[0]}/{repo[1]}/releases?per_page={PER_PAGE}"
    try:
        status, body = http_get(url, api_headers())
    except ReleaseCheckError:
        raise
    except Exception as error:  # noqa: BLE001 - the network, a timeout, a body over the limit
        raise ReleaseCheckError(type(error).__name__) from None
    if status != 200:
        raise ReleaseCheckError(f"HTTP {status}")
    if len(body) > MAX_RESPONSE_BYTES:
        raise ReleaseCheckError("answer too large")
    try:
        data = json.loads(body)
    except ValueError:
        raise ReleaseCheckError("no JSON") from None
    return parse_releases(data)


def check_versions(
    install_dir: Path, runner: CommandRunner, http_get: HttpGet, *, channel: Channel, now: datetime
) -> VersionInfo:
    """The check for new versions (the button and the automatic check): one request to GitHub's release
    list gives the newest version the channel offers and, when it is newer, the notes up to it (Spec 11.3a).

    Raises `UpdateCheckError` when there is no installation, its repository is unknown (a git copy from
    elsewhere, a broken `release.json`) or GitHub cannot be asked; nothing is asked then without a
    repository."""
    found = read_installation(install_dir, runner)
    if found is None:
        raise ReleaseCheckError("no installation")
    if found.repo is None:
        raise ReleaseCheckError("no GitHub repository")
    releases = fetch_releases(found.repo, http_get)
    newest = newest_offered(releases, channel)
    back = return_target(releases, found.tag)
    info = VersionInfo(
        installed=found.tag, newest=newest.tag if newest else None, checked_at=now, return_to=back.tag if back else None
    )
    if newest is None or found.tag is None or not is_newer(newest.tag, found.tag):
        return info
    notes = notes_for(releases, installed=found.tag, newest=newest.tag, channel=channel, page_url=releases_page(*found.repo))
    return replace(info, notes=notes, rework_ahead=starts_with_rework(newest.body))
