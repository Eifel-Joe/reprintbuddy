"""What is installed and where it came from (v0.4.9 p500, Spec 11.3a "Paket je Release").

One seam for the question the pages and the update helper both ask. Since v0.4.9 the installer and the
update unpack the package of a release into `<install dir>/releases/<tag>/` and write `release.json` next
to it (version, repository, checksum) as their last act, so the file names what really runs. An
installation made with git (up to v0.4.9) has no such file: its version and repository still come from
git (`describe`, `remote get-url origin`). That old way stays for good, because an old installation's own
update helper can jump by git to any later version.

Nothing here raises for a broken marker or a git that answers nothing: such an installation simply has no
version or no repository, and the check says it failed (Altfälle: no crash, nothing lost silently).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from reprintbuddy.ops.releasenotes import github_repo
from reprintbuddy.ops.runner import CommandRunner
from reprintbuddy.ops.versions import parse_version

MARKER = "release.json"
RELEASES_DIR = "releases"
_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True)
class Installation:
    kind: Literal["package", "git"]
    tag: str | None  # the installed version, None when nothing says it
    repo: tuple[str, str] | None  # (owner, name) on GitHub, None when not known
    source_dir: Path  # the unpacked code of the installed version (pip installs from here)
    label: str  # the name of the backup before an update: the tag, else 12 characters of the commit


def is_installation(install_dir: Path) -> bool:
    return (install_dir / MARKER).is_file() or (install_dir / ".git").exists()


def _repo(text: object) -> tuple[str, str] | None:
    if not isinstance(text, str):
        return None
    owner, _, name = text.partition("/")
    for part in (owner, name):
        if not _NAME.match(part) or part in (".", ".."):
            return None
    return owner, name


def _read_marker(install_dir: Path) -> dict[str, object] | None:
    try:
        data = json.loads((install_dir / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _package(install_dir: Path, data: dict[str, object] | None) -> Installation:
    tag = data.get("tag") if data else None
    tag = tag if isinstance(tag, str) and parse_version(tag) is not None else None
    repo = _repo(data.get("repo")) if data else None
    source = install_dir / RELEASES_DIR / tag if tag else install_dir
    return Installation(kind="package", tag=tag, repo=repo, source_dir=source, label=tag or "unknown")


def _git(install_dir: Path, runner: CommandRunner) -> Installation:
    git = ("git", "-C", str(install_dir))
    described = runner((*git, "describe", "--tags", "--exact-match", "HEAD"))
    tag = described.stdout.strip() if described.ok and described.stdout.strip() else None
    head = runner((*git, "rev-parse", "HEAD"))
    commit = head.stdout.strip() if head.ok else ""
    origin = runner((*git, "remote", "get-url", "origin"), timeout=10.0)
    repo = github_repo(origin.stdout.strip()) if origin.ok else None
    label = tag or (commit[:12] if commit else "unknown")
    return Installation(kind="git", tag=tag, repo=repo, source_dir=install_dir, label=label)


def read_installation(install_dir: Path, runner: CommandRunner) -> Installation | None:
    """The installation in `install_dir`, or None when there is none (a development checkout).

    `release.json` goes before `.git`; a marker that cannot be read gives way to git when a git copy is
    there, else it is a package installation that names nothing."""
    has_marker = (install_dir / MARKER).is_file()
    data = _read_marker(install_dir) if has_marker else None
    if data is not None:
        return _package(install_dir, data)
    if (install_dir / ".git").exists():
        return _git(install_dir, runner)
    if has_marker:
        return _package(install_dir, None)
    return None


def installed_version(install_dir: Path, runner: CommandRunner) -> str | None:
    """Only the installed version, for the pages: `release.json`, else one `git describe` (no other git
    question on a page view)."""
    data = _read_marker(install_dir) if (install_dir / MARKER).is_file() else None
    if data is not None:
        return _package(install_dir, data).tag
    if (install_dir / ".git").exists():
        described = runner(("git", "-C", str(install_dir), "describe", "--tags", "--exact-match", "HEAD"))
        return described.stdout.strip() if described.ok and described.stdout.strip() else None
    return None


def write_marker(install_dir: Path, *, tag: str, repo: tuple[str, str], sha256: str) -> None:
    """Write `release.json` at once or not at all (a temporary file, then a rename)."""
    text = json.dumps({"tag": tag, "repo": f"{repo[0]}/{repo[1]}", "sha256": sha256}) + "\n"
    handle, name = tempfile.mkstemp(prefix=".release-", suffix=".json", dir=install_dir)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, install_dir / MARKER)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
