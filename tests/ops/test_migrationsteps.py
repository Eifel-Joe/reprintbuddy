"""Reading migration steps and their heads without running them (Spec 11.3a "Rückweg zur stabilen Version").

The way back reads the head of the stable version from its downloaded package (the Alembic files, parsed,
never imported) and walks the steps of the installed pre-release between that head and the database.
"""

import io
import sqlite3
import tarfile
from pathlib import Path

import pytest

from reprintbuddy.db import make_engine, upgrade
from reprintbuddy.ops.migrationsteps import (
    StepInfo,
    StepsError,
    current_revision,
    downgrade,
    head_of_dir,
    head_of_package,
    parse_graph,
    single_head,
    steps_between,
)

VERSIONS = Path(__file__).resolve().parents[2] / "src" / "reprintbuddy" / "migrations" / "versions"


def step(revision: str, down: str | None) -> str:
    return f'revision = "{revision}"\ndown_revision = {down!r}\n\ndef upgrade():\n    pass\n'


def test_parse_graph_reads_revision_and_parent():
    graph = parse_graph([step("a", None), step("b", "a"), step("c", "b")])
    assert graph == {"a": None, "b": "a", "c": "b"}


def test_a_step_whose_revision_is_not_a_literal_is_passed_over_and_the_gap_is_an_error():
    sources = [step("a", None), 'revision = compute()\ndown_revision = "a"\n', step("c", "b")]
    graph = parse_graph(sources)
    assert graph == {"a": None, "c": "b"}
    with pytest.raises(StepsError):
        single_head(graph)


def test_a_merge_step_is_refused():
    with pytest.raises(StepsError):
        parse_graph([step("a", None), 'revision = "m"\ndown_revision = ("a", "b")\n'])


def test_two_heads_are_an_error():
    with pytest.raises(StepsError):
        single_head({"a": None, "b": "a", "c": "a"})


def test_single_head():
    assert single_head({"a": None, "b": "a", "c": "b"}) == "c"


def test_the_head_of_the_versions_folder_of_this_code():
    assert head_of_dir(VERSIONS) == "0022"


def _package(path: Path, tag: str, versions: dict[str, str]) -> Path:
    top = f"reprintbuddy-{tag}"
    with tarfile.open(path, "w:gz") as archive:
        files = {f"{top}/pyproject.toml": "[project]\n"}
        files |= {f"{top}/src/reprintbuddy/migrations/versions/{name}": text for name, text in versions.items()}
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return path


def test_the_head_of_a_package_is_read_without_unpacking_it(tmp_path):
    versions = {path.name: path.read_text(encoding="utf-8") for path in VERSIONS.glob("*.py")}
    archive = _package(tmp_path / "p.tar.gz", "v1.0.0", versions)
    assert head_of_package(archive, "v1.0.0") == "0022"
    assert not (tmp_path / "reprintbuddy-v1.0.0").exists()


def test_a_package_without_migration_steps_is_an_error(tmp_path):
    archive = _package(tmp_path / "p.tar.gz", "v1.0.0", {})
    with pytest.raises(StepsError):
        head_of_package(archive, "v1.0.0")


def test_a_package_that_is_no_archive_is_an_error(tmp_path):
    broken = tmp_path / "p.tar.gz"
    broken.write_bytes(b"not a tar")
    with pytest.raises(StepsError):
        head_of_package(broken, "v1.0.0")


def test_steps_between_two_revisions_newest_first():
    found = steps_between("0020", "0022")
    assert [item.revision for item in found] == ["0022", "0021"]
    assert all(isinstance(item, StepInfo) for item in found)
    # the steps of 1.0.0 carry no mark: before the rule (Spec 11.3a) there was none
    assert found[0].note is None and found[0].rework is None


def test_steps_between_a_revision_and_itself_is_empty():
    assert steps_between("0022", "0022") == []


@pytest.mark.parametrize(("lower", "upper"), [("0022", "0020"), ("9999", "0022"), ("0020", "9999")])
def test_steps_between_is_none_when_lower_is_no_ancestor(lower, upper):
    assert steps_between(lower, upper) is None


def test_downgrade_and_current_revision(tmp_path):
    path = tmp_path / "d.db"
    engine = make_engine(path)
    upgrade(engine)
    engine.dispose()
    assert current_revision(path) == "0022"
    downgrade(path, "0021")
    assert current_revision(path) == "0021"


def test_current_revision_of_a_missing_or_empty_database(tmp_path):
    assert current_revision(tmp_path / "missing.db") is None
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    assert current_revision(empty) is None


def test_in_line_follows_the_steps_below_the_head():
    from reprintbuddy.ops.migrationsteps import in_line

    graph = {"a": None, "b": "a", "c": "b"}
    assert in_line(graph, "c", "c") and in_line(graph, "a", "c")
    assert not in_line(graph, "c", "b") and not in_line(graph, "x", "c")
