"""Reprintbuddy writes to Bambuddy in exactly two ways (Spec 3.1, B2 Task 6).

A watch over the source of the HTTP client: no other method, no other write path.
"""

import re
from pathlib import Path

import reprintbuddy.bambuddy.client as client_module

SOURCE = Path(client_module.__file__).read_text(encoding="utf-8")

FORBIDDEN = (
    ".put(",
    ".patch(",
    ".delete(",
    '"PUT"',
    '"PATCH"',
    '"DELETE"',
    "/reorder",
    "/start",
    "/stop",
    "/cancel",
    "/dispatch",
)

WRITE_PATHS = {"/queue/batches", "/queue/"}


def test_only_two_write_calls():
    for text in FORBIDDEN:
        assert text not in SOURCE, f"bambuddy/client.py contains {text!r}"

    uses = [m.start() for m in re.finditer(r"_post\(", SOURCE)]
    definitions = [m.start() + len("def ") for m in re.finditer(r"def _post\(", SOURCE)]
    assert len(definitions) == 1
    for pos in (pos for pos in uses if pos not in definitions):
        match = re.match(r'_post\(\s*"([^"]*)"', SOURCE[pos:])
        assert match, f"a _post call without a literal path: {SOURCE[pos:pos + 60]!r}"
        assert match.group(1) in WRITE_PATHS, f"write to {match.group(1)!r}"


def test_both_write_paths_are_used():
    """The watch above watches something: both write calls go through `_post`."""
    assert set(re.findall(r'_post\(\s*"([^"]*)"', SOURCE)) == WRITE_PATHS


def test_posts_only_through_post_helper():
    """A POST sent past `_post` would escape the watch above."""
    assert len(re.findall(r'_send\(\s*"POST"', SOURCE)) == 1
    methods = re.findall(r'_send\(\s*"([A-Z]+)"', SOURCE)
    assert set(methods) <= {"GET", "POST"}
    assert len(re.findall(r"_send\(", SOURCE)) == len(methods) + 1  # + the definition
    assert ".request(" in SOURCE and SOURCE.count(".request(") == 1  # only inside `_send`
