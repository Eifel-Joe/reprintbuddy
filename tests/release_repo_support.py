"""A small git repository with version tags and database steps, for the checks of the release way.

`ReleaseRepo(tmp_path)` holds the migration steps of this code, committed and tagged v1.0.0 (the head of this code);
`add_step(name)` appends a step of tests/beta_steps.py and commits it, `tag(name)` tags the commit.
Tests: tests/test_release_notes_check.py, tests/test_release_workflow.py.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from beta_steps import STEPS, revision_of

from reprintbuddy.db import MIGRATIONS_DIR
from reprintbuddy.db import MIGRATIONS_DIR as _MIGRATIONS_DIR
from reprintbuddy.ops.migrationsteps import head_of_dir as _head_of_dir

HEAD = _head_of_dir(_MIGRATIONS_DIR / "versions")  # the head of this code: the stable head in these tests

VERSIONS = Path("src") / "reprintbuddy" / "migrations" / "versions"


class ReleaseRepo:
    def __init__(self, tmp_path: Path):
        self.git = shutil.which("git")
        if not self.git:
            pytest.skip("git is not installed")
        self.path = tmp_path / "repo"
        shutil.copytree(MIGRATIONS_DIR / "versions", self.path / VERSIONS, ignore=shutil.ignore_patterns("__pycache__"))
        self.head = HEAD
        self.count = 0
        self.run("init", "-q")
        self.commit("stable")
        self.tag("v1.0.0")

    def run(self, *args: str) -> str:
        done = subprocess.run(
            [self.git, "-C", self.path.as_posix(), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
            check=True, capture_output=True, text=True,
        )
        return done.stdout

    def commit(self, message: str) -> None:
        self.run("add", ".")
        self.run("commit", "-q", "-m", message)

    def tag(self, name: str) -> None:
        self.run("tag", name)

    def add_step(self, name: str) -> str:
        self.count += 1
        revision = revision_of(self.count)
        text = STEPS[name].replace("@REV@", revision).replace("@DOWN@", self.head)
        (self.path / VERSIONS / f"{revision}_{name}.py").write_text(text, encoding="utf-8", newline="\n")
        self.head = revision
        self.commit(name)
        return revision
