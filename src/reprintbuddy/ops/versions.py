"""Which version is installed, which is the newest release?

Versions are tags `vMAJOR.MINOR.PATCH`, pre-releases `vMAJOR.MINOR.PATCH-beta.N` (v0.4.9, Spec 11.3a
"Vorabversionen"); every other tag is ignored. Only the check against the remote talks to the network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from reprintbuddy.ops.runner import CommandRunner

if TYPE_CHECKING:
    from reprintbuddy.ops.releasenotes import ReleaseNotes

# ASCII digits only (`\d` also matches the digits of other scripts) and no leading zeros,
# so that every version has exactly one tag name.
_NUMBER = r"(0|[1-9][0-9]*)"
RELEASE_TAG = re.compile(rf"^v{_NUMBER}\.{_NUMBER}\.{_NUMBER}$")  # a stable release only
# A stable release or a pre-release; N counts from 1, without a leading zero either.
VERSION_TAG = re.compile(rf"^v{_NUMBER}\.{_NUMBER}\.{_NUMBER}(?:-beta\.([1-9][0-9]*))?$")

# (major, minor, patch, 1 for a stable release / 0 for a pre-release, N of the pre-release or 0): a
# pre-release sorts before the stable release with the same numbers, pre-releases among them by N
# (1.0.1-beta.2 < 1.0.1). A plain tuple, so that any later comparison (a way back to a stable
# release, for example) needs nothing but this key.
VersionKey = tuple[int, int, int, int, int]


class UpdateCheckError(Exception):
    """The remote could not be asked; the message is the first line git reported."""


def parse_version(tag: str) -> VersionKey | None:
    match = VERSION_TAG.fullmatch(tag)
    if match is None:
        return None
    major, minor, patch, beta = match.groups()
    if beta is None:
        return int(major), int(minor), int(patch), 1, 0
    return int(major), int(minor), int(patch), 0, int(beta)


def is_prerelease_tag(tag: str) -> bool:
    key = parse_version(tag)
    return key is not None and key[3] == 0


def newest_release(tags: Iterable[str]) -> str | None:
    """The newest stable release among git tags (the git way of the update)."""
    best: tuple[VersionKey, str] | None = None
    for tag in tags:
        number = parse_version(tag)
        if number is not None and number[3] == 1 and (best is None or number > best[0]):
            best = (number, tag)
    return None if best is None else best[1]


def is_newer(candidate: str, installed: str | None) -> bool:
    """Is `candidate` a newer version than `installed`? A non-version `installed` counts as older."""
    new = parse_version(candidate)
    if new is None:
        return False
    old = parse_version(installed) if installed else None
    return old is None or new > old


def is_git_install(install_dir: Path) -> bool:
    return (install_dir / ".git").exists()


def installed_tag(install_dir: Path, runner: CommandRunner) -> str | None:
    """The tag the working copy is on, or None for a development build (local command only)."""
    result = runner(("git", "-C", str(install_dir), "describe", "--tags", "--exact-match", "HEAD"))
    return result.stdout.strip() if result.ok and result.stdout.strip() else None


def local_tags(install_dir: Path, runner: CommandRunner) -> list[str]:
    result = runner(("git", "-C", str(install_dir), "tag", "--list", "v*"))
    return result.stdout.split() if result.ok else []


def remote_tags(install_dir: Path, runner: CommandRunner) -> list[str]:
    result = runner(
        ("git", "-C", str(install_dir), "ls-remote", "--tags", "--refs", "origin"), timeout=20.0
    )
    if not result.ok:
        lines = (result.stderr or "").strip().splitlines()
        raise UpdateCheckError(lines[0] if lines else f"git exited with {result.returncode}")
    tags = []
    for line in result.stdout.splitlines():
        _, _, ref = line.partition("\t")
        if ref.startswith("refs/tags/"):
            tags.append(ref[len("refs/tags/"):])
    return tags


@dataclass(frozen=True)
class VersionInfo:
    installed: str | None
    newest: str | None
    checked_at: datetime
    # The release notes of the newer versions (p143), fetched in the same round as the check
    # (`releasenotes.with_notes`); None when there is nothing newer or nothing was asked.
    notes: "ReleaseNotes | None" = None

    @property
    def update_available(self) -> bool:
        return self.newest is not None and is_newer(self.newest, self.installed)


def check_versions(install_dir: Path, runner: CommandRunner, *, now: datetime) -> VersionInfo:
    return VersionInfo(
        installed=installed_tag(install_dir, runner),
        newest=newest_release(remote_tags(install_dir, runner)),
        checked_at=now,
    )
