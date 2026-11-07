"""Reprintbuddy writes to Bambuddy in exactly one way: a single queue entry (Spec 3.1, 7.6).

The only other POST is the live channel's token (`/auth/ws-token`, Spec 7.6, B2 Task 9): it
changes nothing in the queue, the printers or the library.

A watch over the HTTP client, on its syntax tree with a text check as a second layer:

- every `_post(...)` names a literal path from `WRITE_PATHS`;
- the only HTTP call of the module is the one `.request(...)` inside `_send`;
- `_send` is called with anything but `"GET"` only from `_post`, and there only with `"POST"`;
- no helper is taken without calling it (`f = self._post`, `getattr(http, "post")`);
- no module under `src/reprintbuddy/` imports httpx beyond today's allow-list.

Around it:

- the live channel (`bambuddy/live.py`) is the only importer of `websockets`, imports nothing
  else that could open a connection, asks the client only for `ws_token` and `ws_url`, and
  sends nothing over the socket (`check_live`);
- the web modules that hold an `httpx.AsyncClient` from `http_client_factory` only pass it on
  and never send with it themselves (`check_holder`).

Each check is run on the real files and on small sources with one bypass each, so the watch
is shown to catch what it is meant to catch.
"""

from __future__ import annotations

import ast
import textwrap
from collections import Counter
from pathlib import Path

import pytest

import reprintbuddy
import reprintbuddy.bambuddy.client as client_module
import reprintbuddy.bambuddy.live as live_module

SOURCE = Path(client_module.__file__).read_text(encoding="utf-8")
LIVE_SOURCE = Path(live_module.__file__).read_text(encoding="utf-8")
SRC = Path(reprintbuddy.__file__).resolve().parent

# The only paths Reprintbuddy may POST to (one entry per call): the one write call (a single
# queue entry) and the live channel's token, which writes nothing (Spec 3.1, 7.6). Batches
# (`/queue/batches`) are gone since v0.3.5: every print is its own entry.
WRITE_PATHS = {"/queue/", "/auth/ws-token"}

# Modules that import httpx today. A new importer would be a way around the watch.
HTTPX_IMPORTERS = {
    "bambuddy/client.py",
    "bambuddy/connection.py",
    "bambuddy/factory.py",
    "web/app.py",
    "web/rights.py",
}

# Modules that import websockets: the live channel only.
WEBSOCKETS_IMPORTERS = {"bambuddy/live.py"}

# Everything the live channel may import (module -> names; `None`: a plain `import module`).
LIVE_IMPORTS = {
    "__future__": {"annotations"},
    "asyncio": None,
    "inspect": None,
    "json": None,
    "logging": None,
    "time": None,
    "collections.abc": {"Awaitable", "Callable"},
    "typing": {"Any", "Protocol"},
    "urllib.parse": {"urlsplit"},
    "websockets.asyncio.client": {"connect"},
    "websockets.exceptions": {"ConnectionClosed", "InvalidStatus"},
    "reprintbuddy.bambuddy.client": {"BambuddyRejected"},
}

# What the live channel may call on the client.
LIVE_CLIENT_CALLS = {"ws_token", "ws_url"}

# Web modules that hold an `httpx.AsyncClient` without importing httpx are found by this text.
HOLDER_MARK = "client_factory"
HOLDERS_AT_LEAST = {"web/bambuddy_check.py", "web/rights.py"}

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
    """Every way `text` (the client's source) could write past the allowed calls."""
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


def imports(text: str, package: str) -> bool:
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == package for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == package:
            return True
        value = _text(node)
        if value is not None and value.split(".")[0] == package:  # __import__, import_module
            return True
    return False


def imports_httpx(text: str) -> bool:
    return imports(text, "httpx")


def _importers(package: str) -> set[str]:
    return {
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if imports(path.read_text(encoding="utf-8"), package)
    }


# Calls that open a connection, send something or reach a name by text, besides httpx' methods.
OPENERS = HTTP_METHODS | {
    "_post", "_send", "_get", "serve", "open_connection", "create_connection", "urlopen",
    "socket", "__import__", "import_module", "getattr",
}


def check_live(text: str) -> list[str]:
    """Every way the live channel could open or send anything but its one websocket."""
    tree = ast.parse(text)
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in LIVE_IMPORTS or LIVE_IMPORTS[alias.name] is not None:
                    problems.append(f"import {alias.name} (line {node.lineno})")
        elif isinstance(node, ast.ImportFrom):
            allowed = LIVE_IMPORTS.get(node.module or "")
            for alias in node.names:
                if allowed is None or alias.name not in allowed:
                    problems.append(f"from {node.module} import {alias.name} (line {node.lineno})")
        elif isinstance(node, ast.Call) and _name(node.func) in OPENERS:
            problems.append(f"call of {_name(node.func)} (line {node.lineno})")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in OPENERS:
            problems.append(f"the name {node.value!r} appears as a text (line {node.lineno})")

    # `self._client` is only stored, or asked for `ws_token()` / `ws_url()`.
    parents = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and node.attr == "_client"):
            continue
        if isinstance(node.ctx, ast.Store):
            continue
        outer = parents.get(id(node))
        call = parents.get(id(outer))
        if not (
            isinstance(outer, ast.Attribute)
            and outer.attr in LIVE_CLIENT_CALLS
            and isinstance(call, ast.Call)
            and call.func is outer
        ):
            problems.append(f"_client used other than for {sorted(LIVE_CLIENT_CALLS)} (line {node.lineno})")
    return problems


def check_holder(text: str) -> list[str]:
    """Every way a module holding an `httpx.AsyncClient` could send with it itself."""
    problems = []
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Call) and _name(node.func) in HTTP_METHODS:
            problems.append(f"call of {_name(node.func)} (line {node.lineno})")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in HTTP_METHODS:
            problems.append(f"the name {node.value!r} appears as a text (line {node.lineno})")
    return problems


def _holders() -> set[str]:
    return {
        path.relative_to(SRC).as_posix()
        for path in (SRC / "web").rglob("*.py")
        if HOLDER_MARK in path.read_text(encoding="utf-8")
    }


# ---- the real client -------------------------------------------------------------------


def test_only_the_allowed_posts():
    assert check_source(SOURCE) == []


def test_every_post_path_is_used():
    """The watch watches something: the write call and the token go through `_post`."""
    assert post_paths(SOURCE) == WRITE_PATHS


def test_batch_route_is_nowhere_in_the_code():
    """Spec 7.6 (v0.3.5): no batch is created or searched any more, by any module."""
    hits = sorted(
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if "/queue/batches" in path.read_text(encoding="utf-8")
    )
    assert hits == []


def test_only_known_modules_import_httpx():
    found = _importers("httpx")
    assert "bambuddy/client.py" in found  # the detection works at all
    assert found <= HTTPX_IMPORTERS, f"new httpx importers: {sorted(found - HTTPX_IMPORTERS)}"


def test_only_the_live_channel_imports_websockets():
    assert _importers("websockets") == WEBSOCKETS_IMPORTERS


def test_live_channel_opens_nothing_else():
    assert check_live(LIVE_SOURCE) == []


def test_live_channel_asks_the_client_for_token_and_address():
    """The watch watches something: the live channel does call both."""
    called = {
        node.func.attr
        for node in ast.walk(ast.parse(LIVE_SOURCE))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "_client"
    }
    assert called == LIVE_CLIENT_CALLS


def test_client_holders_only_pass_the_client_on():
    holders = _holders()
    assert HOLDERS_AT_LEAST <= holders, holders  # the scope is not silently empty
    problems = {path: check_holder((SRC / path).read_text(encoding="utf-8")) for path in holders}
    assert problems == {path: [] for path in holders}


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

    async def enqueue(self):
        return await self._post("/queue/", {})

    async def ws_token(self):
        return await self._post("/auth/ws-token")
'''


def _with(line: str) -> str:
    """`BASE` plus one method holding `line`."""
    method = f"\nasync def bad(self, http, suffix, url):\n    {line}\n"
    return BASE + textwrap.indent(method, "    ")


def test_base_passes():
    assert check_source(BASE) == []
    assert post_paths(BASE) == WRITE_PATHS


BYPASSES = {
    "batch-create": ('await self._post("/queue/batches", {})', "_post to '/queue/batches'"),
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


# ---- the live channel's watch catches each bypass ----------------------------------------

LIVE_BASE = '''
from __future__ import annotations

import asyncio
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed


class LiveListener:
    def __init__(self, client, connect=connect):
        self._client = client
        self._connect = connect

    async def run(self):
        token = await self._client.ws_token()
        async with self._connect(self._client.ws_url(token)) as connection:
            return await connection.recv()
'''

LIVE_BYPASSES = {
    "socket-import": ("import socket", "import socket"),
    "httpx-import": ("import httpx", "import httpx"),
    "server-import": ("from websockets.asyncio.server import serve", "import serve"),
    "sync-client": ("from websockets.sync.client import connect", "from websockets.sync.client"),
    "send-on-socket": ("await connection.send('x')", "call of send"),
    "client-post": ("await self._client._post('/queue/', {})", "call of _post"),
    "client-enqueue": ("await self._client.enqueue(req)", "_client used other than"),
    "client-passed-on": ("helper(self._client)", "_client used other than"),
    "client-aliased": ("c = self._client", "_client used other than"),
    "getattr": ("getattr(self._client, 'ws_token')", "call of getattr"),
    "open-connection": ("await asyncio.open_connection('h', 1)", "call of open_connection"),
    "dynamic-import": ("__import__('socket')", "call of __import__"),
}


def _live_with(line: str) -> str:
    if line.startswith(("import ", "from ")):
        return LIVE_BASE + "\n" + line + "\n"
    method = f"\nasync def bad(self, connection, req, helper):\n    {line}\n"
    return LIVE_BASE + textwrap.indent(method, "    ")


def test_live_base_passes():
    assert check_live(LIVE_BASE) == []


@pytest.mark.parametrize(("line", "reason"), LIVE_BYPASSES.values(), ids=LIVE_BYPASSES.keys())
def test_live_bypass_is_caught(line, reason):
    problems = check_live(_live_with(line))
    assert any(reason in problem for problem in problems), problems


# ---- the client holders' watch catches each bypass ---------------------------------------

HOLDER_BASE = '''
async def run_check(request, url, api_key):
    async with request.app.state.http_client_factory() as client:
        return await check_connection(url, api_key, client)
'''

HOLDER_BYPASSES = {
    "post": ("await client.post(url)", "call of post"),
    "request": ('await client.request("DELETE", url)', "call of request"),
    "stream": ('client.stream("PUT", url)', "call of stream"),
    "send": ('await client.send(client.build_request("POST", url))', "call of send"),
    "getattr": ('await getattr(client, "patch")(url)', "the name 'patch' appears as a text"),
}


def test_holder_base_passes():
    assert check_holder(HOLDER_BASE) == []


@pytest.mark.parametrize(("line", "reason"), HOLDER_BYPASSES.values(), ids=HOLDER_BYPASSES.keys())
def test_holder_bypass_is_caught(line, reason):
    anchor = "        return await check_connection"
    bad = HOLDER_BASE.replace(anchor, f"        {line}\n{anchor}")
    problems = check_holder(bad)
    assert any(reason in problem for problem in problems), problems
