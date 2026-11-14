"""v0.4.9 p500: the workflow that attaches package and checksum to every published release (Spec 11.3a).

The workflow file is read as YAML; its shell steps run here for real (bash, git, gzip, sha256sum) with a
stand-in for `gh`. Nothing reaches GitHub.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from reprintbuddy.ops import package as pk
from reprintbuddy.ops.releases import checksum_name, package_name
from reprintbuddy.ops.versions import parse_version

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release-package.yml"


def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def step(name_start: str) -> dict:
    steps = workflow()["jobs"]["package"]["steps"]
    found = [item for item in steps if str(item.get("name", "")).startswith(name_start)]
    assert len(found) == 1, name_start
    return found[0]


@pytest.fixture
def bash():
    found = shutil.which("bash")
    if not found:
        pytest.skip("bash is not installed")
    return found


def run_step(bash, name_start, *, cwd, env):
    script = cwd / "step.sh"
    script.write_text(step(name_start)["run"], encoding="utf-8", newline="\n")
    return subprocess.run(
        [bash, script.as_posix()], cwd=cwd, env=dict(os.environ, **env), capture_output=True, text=True,
        encoding="utf-8", stdin=subprocess.DEVNULL, timeout=120,
    )


def test_it_runs_when_a_release_is_published_and_by_hand_for_a_tag():
    on = workflow()[True]  # YAML 1.1 reads the key `on` as true
    assert on["release"] == {"types": ["published"]}
    assert on["workflow_dispatch"]["inputs"]["tag"]["required"] is True


def test_only_the_packaging_job_may_write_and_only_contents():
    data = workflow()
    assert data["permissions"] == {"contents": "read"}
    assert data["jobs"]["package"]["permissions"] == {"contents": "write"}
    assert list(data["jobs"]) == ["package"]


def test_the_tag_reaches_the_shell_only_through_the_environment():
    """No `${{ ... }}` inside a `run:` (a tag name typed by hand must not become shell code)."""
    for item in workflow()["jobs"]["package"]["steps"]:
        assert "${{" not in str(item.get("run", "")), item.get("name")
    assert workflow()["jobs"]["package"]["env"]["TAG"] == "${{ github.event.release.tag_name || inputs.tag }}"


def test_the_package_is_built_from_the_tag_itself():
    checkout = [item for item in workflow()["jobs"]["package"]["steps"] if str(item.get("uses", "")).startswith("actions/checkout@")]
    assert len(checkout) == 1
    assert checkout[0]["with"]["ref"] == "refs/tags/${{ env.TAG }}"


def test_a_second_run_replaces_the_files_instead_of_failing():
    """`gh release upload --clobber` deletes a file of the same name before the upload (gh manual)."""
    upload = step("Attach")["run"]
    assert 'gh release upload "$TAG" "reprintbuddy-$TAG.tar.gz" "reprintbuddy-$TAG.tar.gz.sha256" --clobber' in upload


# --- the check of tag and pre-release mark (Spec 11.3a "Vorabversionen") ------------------------------

GH_STUB = """#!/bin/bash
# Stands in for `gh release view <tag> --json isPrerelease,isDraft --jq ...`: answers from the environment.
[ "$1 $2" = "release view" ] || exit 9
[ "$3" = "$TAG" ] || exit 8
echo "$MARK"
"""


def check_tag(bash, tmp_path, tag, mark):
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    (stubs / "gh").write_text(GH_STUB, encoding="utf-8", newline="\n")
    (stubs / "gh").chmod(0o755)
    env = {"TAG": tag, "MARK": mark, "PATH": f"{stubs.as_posix()}{os.pathsep}{os.environ['PATH']}"}
    return run_step(bash, "Check the tag", cwd=tmp_path, env=env)


@pytest.mark.parametrize(
    "tag, mark",
    [("v1.0.0", "false false"), ("v1.0.1-beta.2", "true false")],
)
def test_a_tag_whose_mark_agrees_goes_on(bash, tmp_path, tag, mark):
    done = check_tag(bash, tmp_path, tag, mark)
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize(
    "tag, mark, words",
    [
        ("v1.0.0", "true false", "pre-release mark"),  # marked as a pre-release, tagged as a release
        ("v1.0.1-beta.2", "false false", "pre-release mark"),  # tagged as a pre-release, not marked
        ("v1.0.0", "false true", "draft"),  # started by hand for a draft
        ("v1.0.0-rc.1", "true false", "not a version tag"),
        ("main", "false false", "not a version tag"),
        ("v1.0.0; echo injected", "false false", "not a version tag"),
    ],
)
def test_a_tag_whose_mark_does_not_agree_stops_the_workflow(bash, tmp_path, tag, mark, words):
    done = check_tag(bash, tmp_path, tag, mark)
    assert done.returncode != 0
    assert words in done.stdout + done.stderr
    assert "injected" not in done.stdout.replace("echo injected", "")


def test_the_tag_rule_of_the_workflow_is_the_one_of_the_code():
    found = re.findall(r"grep -Eq '([^']*)'", step("Check the tag")["run"])
    assert len(found) == 1
    pattern = re.compile(found[0])
    for tag in ("v0.4.9", "v1.0.1-beta.1", "v1.0.0-beta.0", "v01.0.0", "v1.0", "v1.0.0-rc.1", "v1.0.0 "):
        assert bool(pattern.fullmatch(tag)) is (parse_version(tag) is not None), tag


# --- the version in the code and the build --------------------------------------------------------------


def test_the_version_in_the_code_must_be_the_tag(bash, tmp_path):
    (tmp_path / "src" / "reprintbuddy").mkdir(parents=True)
    (tmp_path / "src" / "reprintbuddy" / "__init__.py").write_text('"""x"""\n\n__version__ = "1.0.0"\n', encoding="utf-8")
    assert run_step(bash, "Check the version", cwd=tmp_path, env={"TAG": "v1.0.0"}).returncode == 0
    done = run_step(bash, "Check the version", cwd=tmp_path, env={"TAG": "v1.0.1"})
    assert done.returncode != 0 and "v1.0.1" in done.stdout + done.stderr


@pytest.fixture
def tagged(tmp_path):
    git = shutil.which("git")
    if not git:
        pytest.skip("git is not installed")
    repo = tmp_path / "repo"
    repo.mkdir()

    def g(*args):
        subprocess.run([git, "-C", repo.as_posix(), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                       check=True, capture_output=True)

    g("init", "-q")
    (repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8", newline="\n")
    (repo / "install").mkdir()
    (repo / "install" / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8", newline="\n")
    g("add", ".")
    g("commit", "-q", "-m", "release")
    g("tag", "v1.0.0")
    return repo


def test_the_build_gives_the_package_the_update_unpacks_and_a_checksum_that_checks_out(bash, tmp_path, tagged):
    done = run_step(bash, "Build", cwd=tagged, env={"TAG": "v1.0.0"})
    assert done.returncode == 0, done.stderr
    archive = tagged / package_name("v1.0.0")
    checksum = (tagged / checksum_name("v1.0.0")).read_text(encoding="utf-8")
    expected = pk.expected_sha256(checksum, package_name("v1.0.0"))
    assert expected == hashlib.sha256(archive.read_bytes()).hexdigest()
    target = pk.unpack(archive, tmp_path / "releases", "v1.0.0")
    assert (target / "pyproject.toml").is_file() and (target / "install" / "install.sh").is_file()


def test_a_second_build_of_the_same_tag_is_the_same_file(bash, tmp_path, tagged):
    """A run started again by hand replaces the files with identical ones: the checksum stays true."""
    run_step(bash, "Build", cwd=tagged, env={"TAG": "v1.0.0"})
    first = (tagged / package_name("v1.0.0")).read_bytes()
    os.utime(tagged / "pyproject.toml", (1, 1))  # the time of a file in the checkout changes nothing
    run_step(bash, "Build", cwd=tagged, env={"TAG": "v1.0.0"})
    assert (tagged / package_name("v1.0.0")).read_bytes() == first


# --- the notes against the rework marks (Spec 11.3a "Umbau nur angekündigt") ---------------------------

GH_NOTES_STUB = """#!/bin/bash
# Stands in for `gh release view <tag> --json body --jq .body`: answers from the environment.
[ "$1 $2" = "release view" ] || exit 9
[ "$3" = "$TAG" ] || exit 8
[ "$4 $5 $6 $7" = "--json body --jq .body" ] || exit 7
printf '%s\n' "$NOTES"
"""


def test_the_notes_are_checked_before_the_package_is_built():
    names = [str(item.get("name", "")) for item in workflow()["jobs"]["package"]["steps"]]
    check = names.index(step("Check the notes")["name"])
    assert check < names.index(step("Build")["name"])
    assert check < names.index(step("Attach")["name"])
    install = names.index(step("Install the code")["name"])
    assert install < check


def notes_check(bash, tmp_path, repo, tag, notes):
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    (stubs / "gh").write_text(GH_NOTES_STUB, encoding="utf-8", newline="\n")
    (stubs / "python").write_text(f'#!/bin/bash\nexec "{Path(sys.executable).as_posix()}" "$@"\n', encoding="utf-8", newline="\n")
    for name in ("gh", "python"):
        (stubs / name).chmod(0o755)
    (repo.path / "tools").mkdir(exist_ok=True)
    for name in ("check_release_notes.py", "stable_head.py"):
        shutil.copy(ROOT / "tools" / name, repo.path / "tools" / name)
    work = tmp_path / "runner"
    work.mkdir(exist_ok=True)
    env = {"TAG": tag, "NOTES": notes, "RUNNER_TEMP": work.as_posix(),
           "PATH": f"{stubs.as_posix()}{os.pathsep}{os.environ['PATH']}"}
    return run_step(bash, "Check the notes", cwd=repo.path, env=env)


def test_a_pre_release_whose_notes_miss_the_rework_line_gets_no_package(bash, tmp_path):
    from release_repo_support import ReleaseRepo

    from reprintbuddy.ops.releases import REWORK_LINE

    repo = ReleaseRepo(tmp_path)
    repo.add_step("rework_marked")
    repo.tag("v1.1.0-beta.1")
    done = notes_check(bash, tmp_path, repo, "v1.1.0-beta.1", "What is new.")
    assert done.returncode != 0 and "rework" in done.stdout + done.stderr
    done = notes_check(bash, tmp_path, repo, "v1.1.0-beta.1", REWORK_LINE + "\n\nWhat is new.")
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_tag_from_before_the_notes_check_is_built_without_it(bash, tmp_path):
    """Review: the workflow started by hand for an older tag checks out that tag, which has no checker."""
    from release_repo_support import ReleaseRepo

    repo = ReleaseRepo(tmp_path)
    notes_check(bash, tmp_path, repo, "v1.0.0", "Notes.")  # puts the stubs in place
    (repo.path / "tools" / "check_release_notes.py").unlink()  # as in a tag from before the check
    env = {"TAG": "v1.0.0", "NOTES": "Notes.", "RUNNER_TEMP": (tmp_path / "runner").as_posix(),
           "PATH": f"{(tmp_path / 'stubs').as_posix()}{os.pathsep}{os.environ['PATH']}"}
    done = run_step(bash, "Check the notes", cwd=repo.path, env=env)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "::notice::" in done.stdout
