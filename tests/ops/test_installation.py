"""v0.4.9 p500: what is installed and where it came from (`release.json`, git as the old case)."""

from __future__ import annotations

import json

import pytest

from reprintbuddy.ops import installation as inst
from reprintbuddy.ops.runner import CommandResult


@pytest.fixture
def d(tmp_path):
    folder = tmp_path / "opt"
    folder.mkdir()
    return folder


def git_answers(fake_runner, d, *, describe="v0.4.9\n", head="b" * 40 + "\n", origin="https://github.com/o/r.git\n"):
    git = ("git", "-C", str(d))
    fake_runner.on(git + ("describe",), CommandResult(0, describe) if describe else CommandResult(128, "", "fatal: no tag"))
    fake_runner.on(git + ("rev-parse",), CommandResult(0, head) if head else CommandResult(128))
    fake_runner.on(git + ("remote", "get-url", "origin"), CommandResult(0, origin) if origin else CommandResult(2))


def test_a_package_installation_is_read_from_its_marker_without_any_command(d, fake_runner):
    inst.write_marker(d, tag="v1.0.0", repo=("o", "r"), sha256="c" * 64)
    found = inst.read_installation(d, fake_runner)
    assert found == inst.Installation(
        kind="package", tag="v1.0.0", repo=("o", "r"), source_dir=d / "releases" / "v1.0.0", label="v1.0.0"
    )
    assert fake_runner.calls == []
    assert inst.is_installation(d)


def test_write_marker_leaves_no_temporary_file(d):
    inst.write_marker(d, tag="v1.0.0", repo=("o", "r"), sha256="c" * 64)
    assert sorted(path.name for path in d.iterdir()) == ["release.json"]
    assert json.loads((d / "release.json").read_text(encoding="utf-8")) == {
        "tag": "v1.0.0", "repo": "o/r", "sha256": "c" * 64,
    }


def test_the_marker_goes_before_an_old_git_copy(d, fake_runner):
    (d / ".git").mkdir()
    inst.write_marker(d, tag="v1.0.0", repo=("o", "r"), sha256="c" * 64)
    assert inst.read_installation(d, fake_runner).kind == "package"
    assert fake_runner.calls == []


def test_an_old_git_installation(d, fake_runner):
    (d / ".git").mkdir()
    git_answers(fake_runner, d)
    found = inst.read_installation(d, fake_runner)
    assert found == inst.Installation(kind="git", tag="v0.4.9", repo=("o", "r"), source_dir=d, label="v0.4.9")
    assert inst.is_installation(d)


def test_a_git_installation_from_elsewhere_has_no_repository(d, fake_runner):
    (d / ".git").mkdir()
    git_answers(fake_runner, d, origin="https://example.org/x.git\n")
    assert inst.read_installation(d, fake_runner).repo is None


def test_a_git_installation_without_tag_is_labelled_by_its_commit(d, fake_runner):
    (d / ".git").mkdir()
    git_answers(fake_runner, d, describe=None)
    found = inst.read_installation(d, fake_runner)
    assert found.tag is None
    assert found.label == "b" * 12


def test_a_git_installation_where_git_answers_nothing(d, fake_runner):
    (d / ".git").mkdir()
    git_answers(fake_runner, d, describe=None, head=None, origin=None)
    found = inst.read_installation(d, fake_runner)
    assert (found.kind, found.tag, found.repo, found.label) == ("git", None, None, "unknown")


@pytest.mark.parametrize(
    "content",
    ['{"tag": 5}', "not json", '{"tag": "v1.0.0", "repo": "no-slash", "sha256": "x"}', '["v1.0.0"]', '{"tag": "../x", "repo": "o/r"}'],
)
def test_a_broken_marker_without_git_is_no_crash(d, fake_runner, content):
    (d / "release.json").write_text(content, encoding="utf-8")
    found = inst.read_installation(d, fake_runner)
    assert found.kind == "package"
    assert found.tag is None or found.repo is None
    assert fake_runner.calls == []


def test_a_broken_marker_with_git_beside_it_takes_the_git_way(d, fake_runner):
    (d / ".git").mkdir()
    (d / "release.json").write_text("not json", encoding="utf-8")
    git_answers(fake_runner, d)
    assert inst.read_installation(d, fake_runner).kind == "git"


def test_an_empty_folder_is_no_installation(d, fake_runner):
    assert inst.read_installation(d, fake_runner) is None
    assert not inst.is_installation(d)
    assert not inst.is_installation(d / "missing")


def test_installed_version_asks_git_only_for_the_tag(d, fake_runner):
    """The page needs the version only; one `git describe` per process, as before v0.4.9."""
    (d / ".git").mkdir()
    git_answers(fake_runner, d)
    assert inst.installed_version(d, fake_runner) == "v0.4.9"
    assert [call[3] for call in fake_runner.calls] == ["describe"]


def test_installed_version_of_a_package_installation_and_of_none(d, fake_runner):
    assert inst.installed_version(d, fake_runner) is None
    inst.write_marker(d, tag="v1.0.0", repo=("o", "r"), sha256="c" * 64)
    assert inst.installed_version(d, fake_runner) == "v1.0.0"
    assert fake_runner.calls == []
