"""A test must not read one item from a response stream with a throw-away iterator.

CI v1.2.0-beta.1: `first = next(response.iter_bytes(65536))` in tests/web/test_backup_download_cleanup.py
dropped the iterator at once, and httpx closes the response, and with it the connection, when the iterator
goes. The "download under way" was cancelled right after its first block and the test raced against the
server's clean-up (one run in four on the CI). The same trap was found and avoided before
(tests/web/test_live_shutdown.py, p451). The form is easy to see in the source, so a test looks for it.

What it cannot see: that a server which is faster than the reader has sent everything before the reader looks
(no clock, no pattern: the test must hold the server, as test_backup_download_cleanup.py does)."""

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ITERATORS = {
    "iter_bytes", "iter_lines", "iter_raw", "iter_text",
    "aiter_bytes", "aiter_lines", "aiter_raw", "aiter_text",
}


def dropped_iterators(source: str) -> list[int]:
    """The lines of `next(x.iter_...(...))` and `anext(x.aiter_...(...))`: one item, then the iterator is gone."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"next", "anext"}):
            continue
        if not node.args:
            continue
        inner = node.args[0]
        if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute) and inner.func.attr in ITERATORS:
            found.append(node.lineno)
    return found


def test_the_checker_sees_a_dropped_iterator_and_not_a_kept_one():
    assert dropped_iterators("first = next(response.iter_bytes(65536))\n") == [1]
    assert dropped_iterators("async def f(r):\n    return await anext(r.aiter_lines())\n") == [2]
    assert dropped_iterators("assert next(http.stream_response.iter_text())\n") == [1]
    kept = "blocks = response.iter_bytes()\nfirst = next(blocks)\n"
    assert dropped_iterators(kept) == []
    assert dropped_iterators("rows = list(response.iter_lines())\nnext(iter(rows))\n") == []
    assert dropped_iterators("first = next(items)\n") == []


def test_no_test_reads_a_response_stream_with_a_throw_away_iterator():
    found = []
    for path in sorted(TESTS.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        found += [f"{path.relative_to(TESTS)}:{line}" for line in dropped_iterators(source)]
    assert not found, (
        "keep the iterator in a name (`blocks = response.iter_bytes()`, then `next(blocks)`): "
        "httpx closes the connection when a dropped iterator goes. " + ", ".join(found)
    )
