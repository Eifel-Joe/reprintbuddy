"""Which version is installed, which is the newest release?

Versions are git tags `vMAJOR.MINOR.PATCH` and nothing else; every other tag is
ignored. Only the check against the remote (`remote_tags`) talks to the network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from reprintbuddy.ops.runner import CommandRunner

RELEASE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


class UpdateCheckError(Exception):
    """The remote could not be asked; the message is the first line git reported."""


def parse_release_tag(tag: str) -> tuple[int, int, int] | None:
    match = RELEASE_TAG.fullmatch(tag)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def newest_release(tags: Iterable[str]) -> str | None:
    best: tuple[tuple[int, int, int], str] | None = None
    for tag in tags:
        number = parse_release_tag(tag)
        if number is not None and (best is None or number > best[0]):
            best = (number, tag)
    return None if best is None else best[1]


def is_newer(candidate: str, installed: str | None) -> bool:
    """Is `candidate` a newer release than `installed`? A non-release `installed` counts as older."""
    new = parse_release_tag(candidate)
    if new is None:
        return False
    old = parse_release_tag(installed) if installed else None
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

    @property
    def update_available(self) -> bool:
        return self.newest is not None and is_newer(self.newest, self.installed)


def check_versions(install_dir: Path, runner: CommandRunner, *, now: datetime) -> VersionInfo:
    return VersionInfo(
        installed=installed_tag(install_dir, runner),
        newest=newest_release(remote_tags(install_dir, runner)),
        checked_at=now,
    )
