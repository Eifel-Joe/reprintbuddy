"""Which version is installed, which is the newest release?

Versions are tags `vMAJOR.MINOR.PATCH`, pre-releases `vMAJOR.MINOR.PATCH-beta.N` (v0.4.9, Spec 11.3a
"Vorabversionen"); every other tag is ignored. Nothing here talks to the network (`ops/releases.py` does).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

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
    """The check could not be made; the message is a short reason of our own (never GitHub's text)."""


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


def is_newer(candidate: str, installed: str | None) -> bool:
    """Is `candidate` a newer version than `installed`? A non-version `installed` counts as older."""
    new = parse_version(candidate)
    if new is None:
        return False
    old = parse_version(installed) if installed else None
    return old is None or new > old


@dataclass(frozen=True)
class VersionInfo:
    installed: str | None
    newest: str | None
    checked_at: datetime
    # The release notes of the newer versions (p143), from the same answer as the check (v0.4.9,
    # `releases.check_versions`); None when there is nothing newer or nothing was asked.
    notes: "ReleaseNotes | None" = None

    @property
    def update_available(self) -> bool:
        return self.newest is not None and is_newer(self.newest, self.installed)
