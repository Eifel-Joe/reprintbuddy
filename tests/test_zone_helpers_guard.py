"""Spec 10.5a: the helpers of the display zone - the browser's zone of a request - belong to the pages.

Domain, ops, notify, the engine and the command line count days in the zone of the server
(`to_server_local`, `server_date`, `end_of_server_day`). Review finding 9 (1.2.2): a task started inside a
request copies the request's context, the browser's zone included, and keeps it after the request is over.
Harmless while nothing outside `reprintbuddy.web` reads the display zone; this test keeps it so.
"""

from __future__ import annotations

import ast
from pathlib import Path

import reprintbuddy

SRC = Path(reprintbuddy.__file__).parent
DISPLAY_HELPERS = frozenset({
    "to_local", "local_date", "at_local_time", "end_of_local_day", "booked_day",
    "display_zone", "browser_zone", "shown_in",
})


def violations(source: str) -> list[str]:
    """The uses of a display helper in `source`: imported from a module of Reprintbuddy, or read as an
    attribute of `localtime` (`from reprintbuddy import localtime`, `reprintbuddy.localtime`)."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("reprintbuddy"):
            found += [f"{node.lineno}: import {a.name}" for a in node.names if a.name in DISPLAY_HELPERS]
        elif isinstance(node, ast.Attribute) and node.attr in DISPLAY_HELPERS:
            base = node.value
            if (isinstance(base, ast.Name) and base.id == "localtime") or (
                isinstance(base, ast.Attribute) and base.attr == "localtime"
            ):
                found.append(f"{node.lineno}: localtime.{node.attr}")
    return found


def outside_the_pages() -> list[Path]:
    """Every module of the package but `web/` and `localtime.py` itself."""
    return [
        path for path in sorted(SRC.rglob("*.py"))
        if path.relative_to(SRC).parts[0] != "web" and path.relative_to(SRC).as_posix() != "localtime.py"
    ]


def test_the_scan_sees_each_way_of_using_a_helper():
    assert violations("from reprintbuddy.localtime import to_local, server_date\n") == ["1: import to_local"]
    assert violations("from reprintbuddy.web.formatting import to_local\n") == ["1: import to_local"]
    assert violations("from reprintbuddy import localtime\nx = localtime.local_date(t)\n") == ["2: localtime.local_date"]
    assert violations("import reprintbuddy.localtime\nreprintbuddy.localtime.shown_in(None)\n") == [
        "2: localtime.shown_in"]
    assert violations("from reprintbuddy.localtime import server_date, end_of_server_day, to_server_local\n") == []


def test_the_files_are_there():
    names = {path.relative_to(SRC).as_posix() for path in outside_the_pages()}
    assert {"domain/stock.py", "domain/reorder.py", "cli.py"} <= names
    assert not any(name.startswith("web/") for name in names) and "localtime.py" not in names


def test_only_the_pages_use_the_zone_of_the_browser():
    found = {
        path.relative_to(SRC).as_posix(): hits
        for path in outside_the_pages()
        if (hits := violations(path.read_text(encoding="utf-8")))
    }
    assert found == {}
