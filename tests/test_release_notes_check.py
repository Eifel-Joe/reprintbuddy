"""The notes of a pre-release begin with the rework line exactly when one of its database steps after the last
stable version is marked as rework (Spec 11.3a "Umbau nur angekündigt"). tools/check_release_notes.py checks
it; the release workflow runs it before it attaches the package, and the main session before each release.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
from release_repo_support import ReleaseRepo

from reprintbuddy.ops.releases import REWORK_LINE

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "check_release_notes.py"


def tool():
    spec = importlib.util.spec_from_file_location("check_release_notes_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WITH_LINE = REWORK_LINE + "\n\nWhat is new."
WITHOUT_LINE = "What is new."


@pytest.fixture
def repo(tmp_path):
    return ReleaseRepo(tmp_path)


def test_a_pre_release_that_only_adds_must_not_carry_the_line(repo):
    repo.add_step("add_table")
    repo.tag("v1.1.0-beta.1")
    check = tool().check
    assert check(repo.path, "v1.1.0-beta.1", WITHOUT_LINE) is None
    said = check(repo.path, "v1.1.0-beta.1", WITH_LINE)
    assert said is not None and "no step" in said


def test_a_pre_release_with_a_rework_step_must_carry_the_line(repo):
    repo.add_step("add_table")
    rework = repo.add_step("rework_marked")
    repo.tag("v1.1.0-beta.2")
    check = tool().check
    assert check(repo.path, "v1.1.0-beta.2", WITH_LINE) is None
    said = check(repo.path, "v1.1.0-beta.2", WITHOUT_LINE)
    assert said is not None and rework in said


def test_a_pre_release_without_steps_must_not_carry_the_line(repo):
    repo.tag("v1.1.0-beta.1")
    assert tool().check(repo.path, "v1.1.0-beta.1", WITHOUT_LINE) is None
    assert tool().check(repo.path, "v1.1.0-beta.1", WITH_LINE) is not None


def test_a_step_without_its_mark_counts_as_rework(repo):
    """A step after the stable head without `rework` is treated as rework on the way back (case 3)."""
    repo.add_step("no_note")
    repo.tag("v1.1.0-beta.1")
    assert tool().check(repo.path, "v1.1.0-beta.1", WITH_LINE) is None


def test_a_stable_release_never_carries_the_line(repo):
    repo.add_step("rework_marked")
    repo.tag("v1.1.0")
    check = tool().check
    assert check(repo.path, "v1.1.0", WITHOUT_LINE) is None
    assert check(repo.path, "v1.1.0", WITH_LINE) is not None


def test_a_pre_release_without_a_stable_version_before_it_is_refused(tmp_path):
    repo = ReleaseRepo(tmp_path)
    repo.run("tag", "-d", "v1.0.0")
    repo.tag("v1.0.0-beta.1")
    said = tool().check(repo.path, "v1.0.0-beta.1", WITHOUT_LINE)
    assert said is not None and "stable" in said


def test_the_command_line(repo, tmp_path):
    repo.add_step("rework_marked")
    repo.tag("v1.1.0-beta.1")
    notes = tmp_path / "notes.md"
    notes.write_text(WITHOUT_LINE, encoding="utf-8")
    args = [sys.executable, str(TOOL), "--repo", str(repo.path), "--tag", "v1.1.0-beta.1", "--notes-file", str(notes)]
    done = subprocess.run(args, capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 1 and "rework" in done.stderr
    notes.write_text(WITH_LINE, encoding="utf-8")
    done = subprocess.run(args, capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0, done.stderr


def test_the_last_stable_version_is_found_on_main_after_a_merge(tmp_path):
    """Review: stable versions are tagged on a merge commit in `main`, which is not in the history of `dev`.
    The last stable version is the newest stable tag below the version whose steps lie on the line of `ref`."""
    repo = ReleaseRepo(tmp_path)
    repo.run("checkout", "-q", "-b", "dev")
    first = repo.add_step("add_table")
    repo.tag("v1.1.0-beta.1")
    repo.run("checkout", "-q", "-b", "main", "v1.0.0")
    repo.run("merge", "-q", "--no-ff", "-m", "release", "dev")
    repo.tag("v1.1.0")
    repo.run("checkout", "-q", "dev")
    repo.add_step("rework_marked")
    repo.tag("v1.2.0-beta.1")
    stable_head = _stable_head_tool()
    assert stable_head.last_stable_tag(repo.path, "v1.2.0-beta.1") == "v1.1.0"
    assert stable_head.stable_head(repo.path, "v1.2.0-beta.1") == first
    assert stable_head.stable_head(repo.path, "HEAD") == first
    assert stable_head.last_stable_tag(repo.path, "v1.1.0-beta.1") == "v1.0.0"  # not a newer one


def _stable_head_tool():
    spec = importlib.util.spec_from_file_location("stable_head_under_test", ROOT / "tools" / "stable_head.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
