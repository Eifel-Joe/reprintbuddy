"""p532 (v1.2.0): the test run in the CI is split into a parallel part and a serial part.

Why: the suite has 7350 tests of about 0.1 s each; four workers on the 4 cores of a GitHub runner
take a fraction of the time of one process. A test that depends on the wall clock (a bound on the time
it took, a loop that counts ticks within a few hundred milliseconds) can fail when four workers load
the machine. Those tests carry the mark `serial` and run afterwards, alone, in a second step.

These tests keep the split honest: the mark is registered, a test with a bound on elapsed wall-clock
time cannot slip into the parallel part unmarked, and the workflow runs both parts.
"""

from __future__ import annotations

import ast
from pathlib import Path

import yaml

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
CI = ROOT / ".github" / "workflows" / "ci.yml"

_CLOCKS = {"monotonic", "perf_counter"}


def _is_serial_mark(node: ast.AST) -> bool:
    """`pytest.mark.serial` (also called, or inside a list as in `pytestmark = [...]`)."""
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, (ast.List, ast.Tuple)):
        return any(_is_serial_mark(item) for item in node.elts)
    return isinstance(node, ast.Attribute) and node.attr == "serial"


def _module_is_serial(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "pytestmark" for t in node.targets):
            if _is_serial_mark(node.value):
                return True
    return False


def _is_clock_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    return name in _CLOCKS


def _is_elapsed(node: ast.AST) -> bool:
    """`time.monotonic() - started`: a clock call minus something."""
    return isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub) and _is_clock_call(node.left)


def _bounds_elapsed_time(function: ast.AST) -> bool:
    """An `assert` with an upper bound on a time taken, inside the function. Two forms are found:
    `assert time.monotonic() - started < 5`, and `took = time.monotonic() - started` followed by
    `assert took < 5` (the name must be assigned from such a difference in the same function)."""
    elapsed_names = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and _is_elapsed(node.value):
            elapsed_names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    for node in ast.walk(function):
        if not isinstance(node, ast.Assert) or not isinstance(node.test, ast.Compare):
            continue
        compare = node.test
        left = compare.left
        subject = _is_elapsed(left) or (isinstance(left, ast.Name) and left.id in elapsed_names)
        if (
            subject
            and all(isinstance(op, (ast.Lt, ast.LtE)) for op in compare.ops)
            and all(isinstance(c, ast.Constant) and isinstance(c.value, (int, float)) for c in compare.comparators)
        ):
            return True
    return False


def _tests_with_a_clock_bound_not_marked() -> list[str]:
    found = []
    for path in sorted(TESTS.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if _module_is_serial(tree):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                if any(_is_serial_mark(d) for d in node.decorator_list):
                    continue
                if _bounds_elapsed_time(node):
                    found.append(f"{path.relative_to(TESTS).as_posix()}::{node.name}")
    return found


def test_the_mark_is_registered(pytestconfig):
    assert any(line.split(":")[0].strip() == "serial" for line in pytestconfig.getini("markers"))


def test_a_test_with_a_bound_on_elapsed_time_is_marked_serial():
    """Add `pytestmark = pytest.mark.serial` (module) or `@pytest.mark.serial` (test) with a line of why."""
    assert _tests_with_a_clock_bound_not_marked() == []


def test_the_checker_sees_the_pattern_and_the_mark():
    """The scan above is only worth something if it finds the pattern it is looking for."""
    bounded = ast.parse("def test_a():\n    assert time.monotonic() - started < 5\n").body[0]
    assert _bounds_elapsed_time(bounded)
    deadline = ast.parse("def test_b():\n    assert time.monotonic() < deadline\n").body[0]
    assert not _bounds_elapsed_time(deadline)
    assert _module_is_serial(ast.parse("import pytest\npytestmark = pytest.mark.serial\n"))
    assert _module_is_serial(ast.parse("import pytest\npytestmark = [pytest.mark.slow, pytest.mark.serial]\n"))
    assert not _module_is_serial(ast.parse("import pytest\npytestmark = pytest.mark.slow\n"))


def test_some_modules_carry_the_mark():
    marked = [p for p in TESTS.rglob("test_*.py") if _module_is_serial(ast.parse(p.read_text(encoding="utf-8")))]
    assert len(marked) >= 5, "the serial part would be empty or nearly so: is the mark still in use?"


def _pytest_steps() -> list[dict]:
    steps = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]["tests"]["steps"]
    return [s for s in steps if "pytest" in str(s.get("run", ""))]


def test_the_ci_runs_a_parallel_part_and_then_the_serial_part():
    parallel, serial = _pytest_steps()  # exactly two, in this order
    assert "-n 4" in parallel["run"]
    assert "--dist loadfile" in parallel["run"]
    assert '-m "not serial"' in parallel["run"]
    assert '-m serial' in serial["run"] and "-n " not in serial["run"]
    # a failure in the parallel part must not hide the result of the serial one (and the other way round)
    assert serial["if"] == "${{ !cancelled() }}"


def test_the_ci_keeps_the_names_the_branch_protection_waits_for():
    jobs = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]
    assert jobs["tests"]["name"] == "Tests (Python ${{ matrix.python }})"
    assert jobs["install-script"]["name"] == "Install script"
    assert jobs["docs"]["name"] == "Documentation"


def _marker_expressions() -> tuple[str, str]:
    """The two `-m` expressions of the CI steps, as written in the workflow."""
    import re

    parallel, serial = _pytest_steps()
    # the last `-m`: the first is `python -m pytest`
    pick = lambda step: re.findall(r' -m ("[^"]*"|\S+)', step["run"])[-1].strip('"')  # noqa: E731
    return pick(parallel), pick(serial)


def test_every_test_runs_in_exactly_one_of_the_two_steps(request):
    """Nothing falls between the two `-m` filters of the workflow and nothing runs twice.
    (`request.session.items` holds the tests of this run; with the full suite, all of them.)"""
    from _pytest.mark.expression import Expression

    parallel_expr, serial_expr = (Expression.compile(e) for e in _marker_expressions())
    items = request.session.items
    assert items
    for item in items:
        names = {mark.name for mark in item.iter_markers()}
        in_parallel = parallel_expr.evaluate(lambda name: name in names)
        in_serial = serial_expr.evaluate(lambda name: name in names)
        assert in_parallel != in_serial, item.nodeid


def test_the_ci_installs_the_dev_extra_and_it_brings_xdist():
    """The workflow installs `.[dev]` as before; `pytest-xdist` is part of that extra."""
    import tomllib

    steps = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]["tests"]["steps"]
    install = [s for s in steps if s.get("name") == "Install"]
    assert len(install) == 1 and install[0]["run"] == 'python -m pip install -e ".[dev]"'
    dev = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["optional-dependencies"]["dev"]
    assert any(spec.replace(" ", "").lower().startswith("pytest-xdist") for spec in dev)
