"""Reprintbuddy writes to Bambuddy in exactly two ways (Spec 3.1, B2 Task 6).

A watch over the HTTP client, on its syntax tree with a text check as a second layer:

- every `_post(...)` names a literal path from `WRITE_PATHS`;
- the only HTTP call of the module is the one `.request(...)` inside `_send`;
- `_send` is called with anything but `"GET"` only from `_post`, and there only with `"POST"`;
- no helper is taken without calling it (`f = self._post`, `getattr(http, "post")`);
- no module under `src/reprintbuddy/` imports httpx beyond today's allow-list.

`check_source` is run on the real file and on small sources with one bypass each, so the
watch is shown to catch what it is meant to catch.
"""

from __future__ import annotations

import ast
import textwrap
from collections import Counter
from pathlib import Path

import pytest

import reprintbuddy
import reprintbuddy.bambuddy.client as client_module

SOURCE = Path(client_module.__file__).read_text(encoding="utf-8")
SRC = Path(reprintbuddy.__file__).resolve().parent

# The only paths Reprintbuddy may write to (one entry per write call).
WRITE_PATHS = {"/queue/batches", "/queue/"}

# Modules that import httpx today. A new importer would be a way around the watch.
HTTPX_IMPORTERS = {
    "bambuddy/client.py",
    "bambuddy/connection.py",
    "bambuddy/factory.py",
    "web/app.py",
    "web/rights.py",
}

# Second layer: text that has no business in the client.
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
    "/pause",
    "/resume",
)

# Method names of httpx that send or prepare a request.
HTTP_METHODS = {"post", "put", "patch", "delete", "request", "send", "stream", "build_request"}
GUARDED = HTTP_METHODS | {"_post", "_send"}


def _name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def _arg(call: ast.Call, index: int, keyword: str) -> ast.AST | None:
    if len(call.args) > index:
        return call.args[index]
    for kw in call.keywords:
        if kw.arg == keyword:
            return kw.value
    return None


def _text(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


class _Scan(ast.NodeVisitor):
    def __init__(self) -> None:
        self.stack: list[str] = []
        self.defs: Counter[str] = Counter()
        self.calls: list[tuple[ast.Call, str | None]] = []
        self.callees: set[int] = set()
        self.refs: list[ast.AST] = []
        self.texts: list[str] = []

    def _function(self, node) -> None:
        self.defs[node.name] += 1
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append((node, self.stack[-1] if self.stack else None))
        self.callees.add(id(node.func))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in GUARDED:
            self.refs.append(node)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in GUARDED:
            self.refs.append(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and node.value in GUARDED:
            self.texts.append(node.value)


def check_source(text: str) -> list[str]:
    """Every way `text` (the client's source) could write past the two allowed calls."""
    problems = [f"contains {fragment!r}" for fragment in FORBIDDEN if fragment in text]
    scan = _Scan()
    scan.visit(ast.parse(text))

    for helper in ("_send", "_post"):
        if scan.defs[helper] != 1:
            problems.append(f"{helper} is defined {scan.defs[helper]} times")
    for ref in scan.refs:
        if id(ref) not in scan.callees:
            problems.append(f"{_name(ref)} is used without being called (line {ref.lineno})")
    for value in scan.texts:
        problems.append(f"the name {value!r} appears as a text")

    http_calls = [(call, where) for call, where in scan.calls if _name(call.func) in HTTP_METHODS]
    if [(_name(call.func), where) for call, where in http_calls] != [("request", "_send")]:
        problems.append(
            "HTTP calls other than the one .request inside _send: "
            + ", ".join(f"{_name(c.func)} in {w} (line {c.lineno})" for c, w in http_calls)
        )

    for call, where in scan.calls:
        name = _name(call.func)
        if name == "_send":
            method = _text(_arg(call, 0, "method"))
            allowed = "POST" if where == "_post" else "GET"
            if method != allowed:
                problems.append(f"_send with {method!r} in {where} (line {call.lineno})")
        elif name == "_post":
            path = _text(_arg(call, 0, "path"))
            if path not in WRITE_PATHS:
                problems.append(f"_post to {path!r} (line {call.lineno})")
    return problems


def post_paths(text: str) -> set[str | None]:
    return {
        _text(_arg(node, 0, "path"))
        for node in ast.walk(ast.parse(text))
        if isinstance(node, ast.Call) and _name(node.func) == "_post"
    }


def imports_httpx(text: str) -> bool:
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "httpx" for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "httpx":
            return True
        value = _text(node)
        if value is not None and value.split(".")[0] == "httpx":  # __import__, import_module
            return True
    return False


# ---- the real client -------------------------------------------------------------------


def test_only_two_write_calls():
    assert check_source(SOURCE) == []


def test_both_write_paths_are_used():
    """The watch watches something: both write calls go through `_post`."""
    assert post_paths(SOURCE) == WRITE_PATHS


def test_only_known_modules_import_httpx():
    found = {
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if imports_httpx(path.read_text(encoding="utf-8"))
    }
    assert "bambuddy/client.py" in found  # the detection works at all
    assert found <= HTTPX_IMPORTERS, f"new httpx importers: {sorted(found - HTTPX_IMPORTERS)}"


# ---- the watch catches each bypass -----------------------------------------------------

BASE = '''
class Client:
    async def _send(self, method, path, *, params=None, body=None):
        async with self._http() as http:
            return await http.request(method, path, params=params, json=body)

    async def _get(self, path):
        return await self._send("GET", path)

    async def _post(self, path, body):
        return await self._send("POST", path, body=body)

    async def ensure_batch(self):
        return await self._post("/queue/batches", {})

    async def enqueue(self):
        return await self._post("/queue/", {})
'''


def _with(line: str) -> str:
    """`BASE` plus one method holding `line`."""
    method = f"\nasync def bad(self, http, suffix, url):\n    {line}\n"
    return BASE + textwrap.indent(method, "    ")


def test_base_passes():
    assert check_source(BASE) == []
    assert post_paths(BASE) == WRITE_PATHS


BYPASSES = {
    "path-concatenated": ('await self._post("/queue/" + suffix, {})', "_post to None"),
    "path-implicitly-joined": ('await self._post("/queue/" "reorder", {})', "_post to '/queue/reorder'"),
    "path-keyword-elsewhere": ('await self._post(path="/printers/1", body={})', "_post to '/printers/1'"),
    "path-variable": ("await self._post(suffix, {})", "_post to None"),
    "http-post": ("await http.post(url)", "post in bad"),
    "http-stream": ('await http.stream("POST", url)', "stream in bad"),
    "http-send-build-request": ('await http.send(http.build_request("POST", url))', "build_request in bad"),
    "second-request": ('await http.request("GET", url)', "request in bad"),
    "send-post-outside-post": ('await self._send("POST", "/printers/1/x")', "_send with 'POST' in bad"),
    "send-post-keyword": ('await self._send(method="POST", path="/queue/")', "_send with 'POST' in bad"),
    "send-method-variable": ("await self._send(suffix, url)", "_send with None in bad"),
    "post-aliased": ("write = self._post", "_post is used without being called"),
    "getattr-post": ('await getattr(http, "post")(url)', "the name 'post' appears as a text"),
    "pause": ('await self._get("/queue/1/pause")', "contains '/pause'"),
    "resume": ('await self._get("/printers/1/resume")', "contains '/resume'"),
}


@pytest.mark.parametrize(("line", "reason"), BYPASSES.values(), ids=BYPASSES.keys())
def test_bypass_is_caught(line, reason):
    problems = check_source(_with(line))
    assert any(reason in problem for problem in problems), problems


def test_put_through_send_is_caught():
    problems = check_source(BASE.replace('_send("GET", path)', '_send("PUT", path)'))
    assert "_send with 'PUT' in _get (line 8)" in problems


@pytest.mark.parametrize(
    "text",
    ["import httpx", "import httpx as h", "from httpx import AsyncClient", "from httpx._client import X",
     '__import__("httpx")', 'importlib.import_module("httpx")'],
)
def test_httpx_import_is_seen(text):
    assert imports_httpx(text)


def test_other_imports_are_not_httpx():
    assert not imports_httpx("import json\nfrom pathlib import Path\nx = 'httpxx'")
