"""The database steps (Alembic revisions) as the way back to a stable version sees them (Spec 11.3a "Rückweg").

Two sources, read in two ways:

- The steps of the stable version come from its downloaded package. They are parsed (`ast`), never
  imported: the package is not installed yet, and nothing of it may run before its checksum and its
  place are settled. Only the literal assignments `revision = "…"` and `down_revision = "…"` count.
- The steps of the installed pre-release come from its own migration folder through Alembic, with the
  two marks a step after the head of the last stable version carries (Spec 11.3a "nur hinzufügen"):
  `return_note = {"de": …, "en": …}` (what the step adds, for the confirmation page) and
  `rework = True/False` (the step changes existing data; the way back then restores the backup).

All imports are at the top (as in `updater.py`): pip replaces the package files while the update helper
runs, and on the way back to 1.0.0 this module is gone afterwards. All Alembic work of the way back lies
before pip (`updater._run_return`). Tests: tests/ops/test_migrationsteps.py, test_updater_imports.py.
"""

from __future__ import annotations

import ast
import sqlite3
import tarfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from alembic.script import ScriptDirectory
from alembic.script.revision import RevisionError
from alembic.util import CommandError

from reprintbuddy.db import MIGRATIONS_DIR, downgrade_file

NOTE_ATTR = "return_note"
REWORK_ATTR = "rework"
LANGUAGES = ("de", "en")


class StepsError(Exception):
    """The steps cannot be read: no head, two heads, a gap, a merge, an unusable package."""


@dataclass(frozen=True)
class StepInfo:
    revision: str
    note: dict[str, str] | None  # None: the step carries no usable `return_note`
    rework: bool | None  # None: the step carries no mark at all


def _literal(node: ast.expr) -> object:
    try:
        return ast.literal_eval(node)
    except ValueError:
        return _NOT_LITERAL


NOT_LITERAL = _NOT_LITERAL = object()


def literals(source: str) -> dict[str, object]:
    """The module-level assignments of a step whose values are literals (others map to a marker)."""
    found: dict[str, object] = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            found[node.targets[0].id] = _literal(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            found[node.target.id] = _literal(node.value)
    return found


def parse_graph(sources: Iterable[str]) -> dict[str, str | None]:
    """`revision` -> `down_revision` of every source that names both as literals; others are passed over.

    A merge step (a tuple as `down_revision`) is refused: the rule "only add" and the way back assume
    one line of steps."""
    graph: dict[str, str | None] = {}
    for source in sources:
        try:
            values = literals(source)
        except SyntaxError as error:
            raise StepsError(f"a migration step cannot be parsed: {error.msg}") from error
        revision, down = values.get("revision", _NOT_LITERAL), values.get("down_revision", _NOT_LITERAL)
        if not isinstance(revision, str) or down is _NOT_LITERAL:
            continue
        if isinstance(down, (tuple, list)):
            raise StepsError(f"step {revision} merges several steps")
        if down is not None and not isinstance(down, str):
            continue
        graph[revision] = down
    return graph


def single_head(graph: dict[str, str | None]) -> str:
    """The one step no other step builds on. Gaps, loops and several heads are errors."""
    if not graph:
        raise StepsError("no migration steps")
    for revision, down in graph.items():
        if down is not None and down not in graph:
            raise StepsError(f"step {revision} builds on {down}, which is missing")
    heads = set(graph) - {down for down in graph.values() if down is not None}
    if len(heads) != 1:
        raise StepsError(f"{len(heads)} heads instead of one")
    return heads.pop()


def head_of_dir(versions_dir: Path) -> str:
    """The head of a folder of migration steps (the unpacked stable version)."""
    try:
        sources = [path.read_text(encoding="utf-8") for path in sorted(Path(versions_dir).glob("*.py"))]
    except OSError as error:
        raise StepsError(f"the migration steps cannot be read: {error}") from error
    return single_head(parse_graph(sources))


def head_of_package(archive: Path, tag: str) -> str:
    """The head of the steps inside a downloaded package, read from the archive without unpacking it."""
    prefix = f"reprintbuddy-{tag}/src/reprintbuddy/migrations/versions/"
    sources: list[str] = []
    try:
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar.getmembers():
                rest = member.name[len(prefix):] if member.name.startswith(prefix) else ""
                if not member.isfile() or not rest.endswith(".py") or "/" in rest:
                    continue
                handle = tar.extractfile(member)
                if handle is not None:
                    sources.append(handle.read().decode("utf-8", errors="replace"))
    except (tarfile.TarError, OSError, EOFError) as error:
        raise StepsError(f"the package cannot be read: {type(error).__name__}") from error
    except Exception as error:  # noqa: BLE001 - gzip and zlib raise their own errors for a broken file
        raise StepsError(f"the package cannot be read: {type(error).__name__}") from error
    return single_head(parse_graph(sources))


def _note(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    if not all(isinstance(value.get(lang), str) and value[lang].strip() for lang in LANGUAGES):
        return None
    return {lang: value[lang].strip() for lang in LANGUAGES}


def steps_between(lower: str, upper: str, script_location: Path | None = None) -> list[StepInfo] | None:
    """The steps after `lower` up to `upper`, newest first; None when `lower` is not an ancestor of `upper`
    or either is unknown to these steps. Loads the step modules (Alembic): only before pip."""
    scripts = ScriptDirectory(str(script_location or MIGRATIONS_DIR))
    try:
        found = list(scripts.iterate_revisions(upper, lower))
    except (RevisionError, CommandError):
        return None
    steps = []
    for script in found:
        module = script.module
        rework = getattr(module, REWORK_ATTR, None)
        steps.append(
            StepInfo(
                revision=script.revision,
                note=_note(getattr(module, NOTE_ATTR, None)),
                rework=rework if isinstance(rework, bool) else None,
            )
        )
    return steps


def current_revision(db_path: Path) -> str | None:
    """The revision a database stands on, read only; None for a missing file or one without Alembic's table."""
    if not Path(db_path).is_file():
        return None
    try:
        connection = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True, timeout=30)
        try:
            row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    return str(row[0]) if row else None


def downgrade(db_path: Path, revision: str, script_location: Path | None = None) -> None:
    """Take the database at `db_path` back to `revision` with the downgrade of each step (Alembic, one
    transaction, foreign keys checked afterwards: `db.downgrade`)."""
    downgrade_file(db_path, revision, script_location)
