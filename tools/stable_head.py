"""The head of the database steps of the last stable version, read from git (Spec 11.3a "Rückweg", 11.8).

The rule "only add" holds for every step after this head: the CI checks it (tests/test_migration_rules.py),
and the release workflow compares the rework marks of those steps with the notes of a pre-release
(tools/check_release_notes.py). The last stable version is the newest tag vX.Y.Z (no "-beta.N") that is
part of the history of `ref`; its steps are read with `git show`, parsed, never imported.

Without git or without such a tag (a copy without history) the answers are None: the callers decide
whether that is allowed (locally a test skips, in the CI it fails).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from reprintbuddy.ops.migrationsteps import parse_graph, single_head

STABLE_TAG = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
VERSIONS = "src/reprintbuddy/migrations/versions"


def _git(repo: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8", check=False
        )
    except OSError:
        return None
    return done.stdout if done.returncode == 0 else None


def last_stable_tag(repo: Path, ref: str = "HEAD") -> str | None:
    """The newest stable version tag in the history of `ref`."""
    listed = _git(repo, "tag", "--merged", ref, "--list", "v*")
    if listed is None:
        return None
    tags = [tag for tag in listed.split() if STABLE_TAG.match(tag)]
    return max(tags, key=lambda tag: tuple(int(part) for part in tag[1:].split("."))) if tags else None


def sources_at(repo: Path, ref: str) -> list[str] | None:
    """The texts of the migration steps as they are at `ref`."""
    names = _git(repo, "ls-tree", "--name-only", f"{ref}:{VERSIONS}")
    if names is None:
        return None
    sources = []
    for name in names.split():
        if name.endswith(".py"):
            text = _git(repo, "show", f"{ref}:{VERSIONS}/{name}")
            if text is None:
                return None
            sources.append(text)
    return sources


def stable_head(repo: Path, ref: str = "HEAD") -> str | None:
    """The head of the steps of the last stable version in the history of `ref`."""
    tag = last_stable_tag(repo, ref)
    if tag is None:
        return None
    sources = sources_at(repo, tag)
    return single_head(parse_graph(sources)) if sources is not None else None


if __name__ == "__main__":
    here = Path(__file__).resolve().parents[1]
    print(last_stable_tag(here), stable_head(here))
