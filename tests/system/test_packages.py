"""The packages of the end-to-end run (tests/system/packages.py): the last stable version as it was released,
and the state under test as a pre-release after it."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

import packages
from reprintbuddy.ops.releases import REWORK_LINE, starts_with_rework

INIT = "src/reprintbuddy/__init__.py"
INSTALL = "install/install.sh"
VERSIONS = "src/reprintbuddy/migrations/versions"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", repo.as_posix(), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        check=True, capture_output=True, text=True,
    )
    return done.stdout


def _step(revision: str, down: str | None, rework: bool | None) -> str:
    mark = "" if rework is None else f"rework = {rework}\n"
    return (
        f'revision = "{revision}"\ndown_revision = {down!r}\n{mark}\n'
        "def upgrade():\n    pass\n\n\ndef downgrade():\n    pass\n"
    )


def _write(repo: Path, name: str, text: str) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _version_files(repo: Path, version: str) -> None:
    _write(repo, INIT, f'"""Reprintbuddy."""\n\n__version__ = "{version}"\n')
    _write(repo, INSTALL, f'#!/usr/bin/env bash\nset -e\nRELEASE_TAG="v{version}"\necho "$RELEASE_TAG"\n')
    _write(repo, "pyproject.toml", "[project]\nname = 'x'\n")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    if not shutil.which("git"):
        pytest.skip("git is not installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _version_files(repo, "1.1.0")
    _write(repo, f"{VERSIONS}/0001_a.py", _step("0001", None, None))
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "one")
    _git(repo, "tag", "v1.1.0")
    _version_files(repo, "1.2.0-beta.1")
    _git(repo, "commit", "-q", "-am", "beta")
    _git(repo, "tag", "v1.2.0-beta.1")
    _version_files(repo, "1.2.0")
    _git(repo, "commit", "-q", "-am", "stable")
    _git(repo, "tag", "v1.2.0")
    _write(repo, "src/reprintbuddy/new.py", "X = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "work in progress")
    return repo


def _members(data: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        return {
            item.name: archive.extractfile(item).read() if item.isfile() else b""  # type: ignore[union-attr]
            for item in archive.getmembers()
        }


def test_the_stable_version_is_the_newest_stable_tag_and_the_prerelease_follows_it(repo: Path) -> None:
    assert packages.last_stable(repo) == "v1.2.0"
    assert packages.prerelease_after("v1.2.0") == "v1.2.1-beta.1"
    assert packages.prerelease_after("v2.0.13") == "v2.0.14-beta.1"


def test_without_a_stable_tag_the_run_cannot_start(tmp_path: Path) -> None:
    if not shutil.which("git"):
        pytest.skip("git is not installed")
    repo = tmp_path / "bare"
    repo.mkdir()
    _git(repo, "init", "-q")
    _version_files(repo, "0.1.0")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "x")
    with pytest.raises(packages.PackageBuildError, match="no stable version"):
        packages.last_stable(repo)


def test_the_stable_package_is_git_archive_of_its_tag_under_one_folder(repo: Path) -> None:
    data = packages.archive(repo, "v1.2.0", "v1.2.0")
    members = _members(data)
    assert all(name == "reprintbuddy-v1.2.0" or name.startswith("reprintbuddy-v1.2.0/") for name in members)
    assert "reprintbuddy-v1.2.0/pyproject.toml" in members
    assert b'__version__ = "1.2.0"' in members[f"reprintbuddy-v1.2.0/{INIT}"]
    assert "reprintbuddy-v1.2.0/src/reprintbuddy/new.py" not in members  # the tag, not the work after it
    # the same bytes on a second build, as the release workflow's `gzip -n` gives (no time stamp)
    assert packages.archive(repo, "v1.2.0", "v1.2.0") == data
    assert data[4:8] == b"\0\0\0\0"  # gzip header: no modification time


def test_a_stable_tag_whose_code_says_another_version_is_refused(repo: Path) -> None:
    with pytest.raises(packages.PackageBuildError, match="1.2.0"):
        packages.archive(repo, "v1.2.0", "v1.3.0")


def test_the_prerelease_is_the_state_under_test_with_two_lines_changed(repo: Path) -> None:
    data = packages.archive(repo, "HEAD", "v1.2.1-beta.1", set_version=True)
    members = _members(data)
    top = "reprintbuddy-v1.2.1-beta.1"
    assert f"{top}/src/reprintbuddy/new.py" in members  # the state under test
    assert members[f"{top}/{INIT}"] == b'"""Reprintbuddy."""\n\n__version__ = "1.2.1-beta.1"\n'
    assert members[f"{top}/{INSTALL}"] == (
        b'#!/usr/bin/env bash\nset -e\nRELEASE_TAG="v1.2.1-beta.1"\necho "$RELEASE_TAG"\n'
    )
    plain = _members(packages.archive(repo, "HEAD", "v1.2.1-beta.1", check_version=False))
    changed = {name for name in plain if plain[name] != members.get(name)}
    assert changed == {f"{top}/{INIT}", f"{top}/{INSTALL}"}
    assert set(plain) == set(members)


def test_a_version_line_that_is_not_there_once_is_refused(repo: Path) -> None:
    _write(repo, INIT, '__version__ = "1.0.0"\n__version__ = "1.0.0"\n')
    _git(repo, "commit", "-q", "-am", "twice")
    with pytest.raises(packages.PackageBuildError, match="__version__"):
        packages.archive(repo, "HEAD", "v1.2.1-beta.1", set_version=True)


def test_the_checksum_file_is_the_line_sha256sum_writes() -> None:
    line = packages.checksum_line("reprintbuddy-v1.0.0.tar.gz", b"abc")
    assert line == hashlib.sha256(b"abc").hexdigest() + "  reprintbuddy-v1.0.0.tar.gz\n"


def test_the_notes_of_the_prerelease_announce_a_rework_only_when_a_step_reworks(repo: Path) -> None:
    assert packages.rework_ahead(repo) is False
    _write(repo, f"{VERSIONS}/0002_b.py", _step("0002", "0001", False))
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "only adds")
    assert packages.rework_ahead(repo) is False
    _write(repo, f"{VERSIONS}/0003_c.py", _step("0003", "0002", None))  # no mark counts as rework
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "reworks")
    assert packages.rework_ahead(repo) is True
    assert starts_with_rework(packages.notes("v1.2.1-beta.1", rework=True))
    assert not starts_with_rework(packages.notes("v1.2.1-beta.1", rework=False))
    assert packages.notes("v1.2.1-beta.1", rework=True).startswith(REWORK_LINE)


def test_build_writes_both_releases_and_the_installers(repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    built = packages.build(repo, out)
    assert built == {"stable": "v1.2.0", "prerelease": "v1.2.1-beta.1", "rework": False}
    manifest = json.loads((out / "releases.json").read_text(encoding="utf-8"))
    assert [item["tag"] for item in manifest] == ["v1.2.1-beta.1", "v1.2.0"]  # newest first, as GitHub lists them
    assert [item["prerelease"] for item in manifest] == [True, False]
    for tag in ("v1.2.0", "v1.2.1-beta.1"):
        package = out / tag / f"reprintbuddy-{tag}.tar.gz"
        line = (out / tag / f"reprintbuddy-{tag}.tar.gz.sha256").read_text(encoding="utf-8")
        assert line == packages.checksum_line(package.name, package.read_bytes())
    assert 'RELEASE_TAG="v1.2.0"' in (out / "install-v1.2.0.sh").read_text(encoding="utf-8")
    assert 'RELEASE_TAG="v1.2.1-beta.1"' in (out / "install-v1.2.1-beta.1.sh").read_text(encoding="utf-8")
    assert json.loads((out / "plan.json").read_text(encoding="utf-8")) == built
