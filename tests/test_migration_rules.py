"""Database steps after the last stable version only add, and their downgrade brings back exactly what was
there (Spec 11.3a "Umbauschritte in Vorabversionen: nur hinzufügen", 11.8).

The CI runs this on every pull request: for each step after the head of the newest stable tag, a database
at the step before is filled (one row in every table), taken one step forward and back, and compared.
A step marked `rework = True` is announced in the release notes instead (tools/check_release_notes.py);
for it only the way back is compared, if it has one. The check itself is tested against the steps in
tests/beta_steps.py: two that only add, and several that break the rule without saying so.
"""

import importlib.util
import os
from pathlib import Path

import pytest
from beta_steps import VIOLATIONS, beta_scripts, revision_of
from migration_rules import (
    adds_only_violations,
    fill_every_table,
    roundtrip_violations,
    snapshot_of,
)

from reprintbuddy.db import MIGRATIONS_DIR, make_engine, upgrade
from reprintbuddy.ops.migrationsteps import StepInfo, downgrade, head_of_dir, steps_between

ROOT = Path(__file__).resolve().parents[1]


def _tool():
    spec = importlib.util.spec_from_file_location("stable_head_under_test", ROOT / "tools" / "stable_head.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STABLE_HEAD = _tool().stable_head(ROOT)


HEAD = head_of_dir(MIGRATIONS_DIR / "versions")


def steps_after_stable() -> list[StepInfo]:
    """The steps after the stable head, oldest first."""
    if STABLE_HEAD is None:
        return []
    found = steps_between(STABLE_HEAD, HEAD)
    assert found is not None, f"the stable head {STABLE_HEAD} is not in the history of this code"
    return list(reversed(found))


def pairs_after_stable() -> list[tuple[str, StepInfo]]:
    """(the revision before, the step) for every step after the stable head."""
    steps = steps_after_stable()
    downs = [STABLE_HEAD] + [step.revision for step in steps[:-1]]
    return list(zip(downs, steps))


def mark_violations(steps: list[StepInfo]) -> list[str]:
    """A step after the stable head says what it adds (de and en) and whether it is rework."""
    found = []
    for step in steps:
        if step.note is None:
            found.append(f"step {step.revision}: no return_note with \"de\" and \"en\"")
        if step.rework is None:
            found.append(f"step {step.revision}: no rework = True/False")
    return found


def check_step(tmp_path: Path, scripts: Path | None, down: str, revision: str, *, rework: bool = False):
    """(what the step did beyond adding, what its downgrade did not bring back); None for a rework step's
    way back that raises."""
    path = tmp_path / "rules.db"
    engine = make_engine(path)
    upgrade(engine, scripts, revision=down)
    engine.dispose()
    fill_every_table(path)
    before = snapshot_of(path)
    engine = make_engine(path)
    upgrade(engine, scripts, revision=revision)
    engine.dispose()
    adds = [] if rework else adds_only_violations(before, snapshot_of(path))
    try:
        downgrade(path, down, scripts)
    except Exception:  # noqa: BLE001 - a rework step may have no way back; the backup is its way back
        if rework:
            return adds, None
        raise
    return adds, roundtrip_violations(before, snapshot_of(path))


def test_the_stable_head_is_known_in_ci():
    if STABLE_HEAD is None:
        if os.environ.get("CI"):
            pytest.fail("no stable version tag in the history: the CI must fetch the tags (fetch-depth: 0)")
        pytest.skip("no stable version tag in this copy (no git history)")
    assert steps_between(STABLE_HEAD, HEAD) is not None


@pytest.mark.parametrize(("down", "step"), pairs_after_stable(), ids=lambda item: getattr(item, "revision", item))
def test_every_step_after_the_last_stable_head_only_adds(tmp_path, down, step):
    adds, back = check_step(tmp_path, None, down, step.revision, rework=step.rework is True)
    assert adds == []
    assert back in ([], None)


def test_every_step_after_the_last_stable_head_carries_note_and_mark():
    assert mark_violations(steps_after_stable()) == []


def test_the_filled_database_has_a_row_in_every_table(tmp_path):
    path = tmp_path / "filled.db"
    engine = make_engine(path)
    upgrade(engine)
    engine.dispose()
    fill_every_table(path)
    empty = [name for name, rows in snapshot_of(path).rows.items() if not rows]
    assert empty == []


@pytest.mark.parametrize("names", [("add_table",), ("add_column",)])
def test_steps_that_only_add_pass_the_check(tmp_path, names):
    scripts = beta_scripts(tmp_path, *names)
    assert check_step(tmp_path, scripts, HEAD, revision_of(1)) == ([], [])


EXPECTED = {
    "index_existing": "index ix_t_orders_reference on existing column(s) reference of table orders",
    "unique_existing": "unique index ux_t_orders_id_reference on existing column(s)",
    "trigger_existing": "trigger t_orders_insert on existing table orders",
    "rebuild": "table orders: rebuilt",
    "rewrite_rows": "table orders: existing rows rewritten",
    "rename": "table orders: existing columns changed",
    "bad_downgrade": "table t_left: left behind",
}


@pytest.mark.parametrize("name", VIOLATIONS)
def test_the_check_catches_a_step_that_breaks_the_rule(tmp_path, name):
    scripts = beta_scripts(tmp_path, name)
    adds, back = check_step(tmp_path, scripts, HEAD, revision_of(1))
    said = adds + (back or [])
    assert any(EXPECTED[name] in line for line in said), said


def test_a_marked_rework_step_is_not_held_to_the_rule(tmp_path):
    scripts = beta_scripts(tmp_path, "rework_marked")
    assert check_step(tmp_path, scripts, HEAD, revision_of(1), rework=True) == ([], None)


def test_a_step_without_marks_is_named(tmp_path):
    scripts = beta_scripts(tmp_path, "add_table", "no_note")
    steps = list(reversed(steps_between(HEAD, revision_of(2), scripts)))
    assert mark_violations(steps) == [
        f"step {revision_of(2)}: no return_note with \"de\" and \"en\"",
        f"step {revision_of(2)}: no rework = True/False",
    ]
