"""The head of the database steps of the last stable version, read from git (Spec 11.3a "Rückweg", 11.8).

The rule "only add" holds for every step after this head: the CI checks it (tests/test_migration_rules.py),
and the release workflow compares the rework marks of those steps with the notes of a pre-release
(tools/check_release_notes.py). The last stable version is the newest tag vX.Y.Z (no "-beta.N") whose steps lie
on the line of `ref` (and that is older than `ref` when `ref` is a version tag); its steps are read with
`git show`, parsed, never imported.

Without git or without such a tag (a copy without history) the answers are None: the callers decide
whether that is allowed (locally a test skips, in the CI it fails).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from reprintbuddy.ops.migrationsteps import StepsError, in_line, parse_graph, single_head
from reprintbuddy.ops.versions import parse_version

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


def stable_tags(repo: Path) -> list[str]:
    """Every stable version tag, newest first."""
    listed = _git(repo, "tag", "--list", "v*")
    tags = [tag for tag in (listed or "").split() if STABLE_TAG.match(tag)]
    return sorted(tags, key=lambda tag: parse_version(tag), reverse=True)  # type: ignore[arg-type, return-value]


def _head_at(repo: Path, ref: str) -> tuple[dict[str, str | None], str] | None:
    sources = sources_at(repo, ref)
    if sources is None:
        return None
    try:
        graph = parse_graph(sources)
        return graph, single_head(graph)
    except StepsError:
        return None


def last_stable_tag(repo: Path, ref: str = "HEAD") -> str | None:
    """The newest stable version whose steps lie on the line of `ref` (older than `ref` when it is a version tag).

    Not the git history of `ref`: a stable version is tagged on a merge commit in `main`, which is not in the
    history of `dev` (review of p539). What counts is whether its head is one of the steps `ref` builds on."""
    found = _head_at(repo, ref)
    if found is None:
        return None
    graph, head = found
    below = parse_version(ref)
    for tag in stable_tags(repo):
        if below is not None and not parse_version(tag) < below:  # type: ignore[operator]
            continue
        tagged = _head_at(repo, tag)
        if tagged is not None and in_line(graph, tagged[1], head):
            return tag
    return None


def stable_head(repo: Path, ref: str = "HEAD") -> str | None:
    """The head of the steps of the last stable version on the line of `ref`."""
    tag = last_stable_tag(repo, ref)
    if tag is None:
        return None
    found = _head_at(repo, tag)
    return found[1] if found else None


if __name__ == "__main__":
    here = Path(__file__).resolve().parents[1]
    print(last_stable_tag(here), stable_head(here))
