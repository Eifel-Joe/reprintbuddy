"""Reprintbuddy writes to Bambuddy in exactly two ways: a single queue entry (Spec 3.1, 7.6) and
one new print file uploaded on the user's click (Spec 3.1, 5.1a; v0.3.9 p20).

The only other POST is the live channel's token (`/auth/ws-token`, Spec 7.6, B2 Task 9): it
changes nothing in the queue, the printers or the library. The upload goes through its own
helper `_post_file`, to one literal path, with the target folder as its only parameter: no
other file, folder or library call can be written.

A watch over the HTTP client, on its syntax tree with a text check as a second layer:

- every `_post(...)` names a literal path from `WRITE_PATHS` and sends no file;
- every `_post_file(...)` names a literal path from `UPLOAD_PATHS` and passes only `folder_id`
  and `files`; `_post_file` takes exactly those and builds its query from `folder_id` alone;
- the only HTTP call of the module is the one `.request(...)` inside `_send`;
- `_send` is called with anything but `"GET"` only from `_post` and `_post_file`, there only
  with `"POST"` and with their own `path` parameter; `files=` only from `_post_file`, which
  sends no `body=`;
- no helper is taken without calling it (`f = self._post`, `getattr(http, "post")`);
- no module under `src/reprintbuddy/` imports httpx beyond today's allow-list.

Around it:

- the live channel (`bambuddy/live.py`) is the only importer of `websockets`, imports nothing
  else that could open a connection, asks the client only for `ws_token` and `ws_url`, and
  sends nothing over the socket (`check_live`);
- every other module that imports httpx or holds an `httpx.AsyncClient` from `http_client_factory`
  (`connection.py`, `factory.py`, `web/app.py`, `web/rights.py`, `web/bambuddy_check.py`, ...) only
  builds or passes the client on and never sends with it itself (`check_holder`). Before v0.3.9
  only the modules with the text `client_factory` were looked at; the helper files of the
  connection, which import httpx but do not carry that text, were not.

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

# The only path a file may be POSTed to: one new print file into the library (Spec 5.1a).
UPLOAD_PATHS = {"/library/files"}
POSTERS = {"_post", "_post_file"}

# Modules that import httpx today. A new importer would be a way around the watch.
HTTPX_IMPORTERS = {
    "bambuddy/client.py",
    "bambuddy/connection.py",
    "bambuddy/factory.py",
    "web/app.py",
    "web/rights.py",
    "notify/homeassistant.py",  # v0.3.12: push messages, never Bambuddy (`check_home_assistant`)
    "ops/releasenotes.py",  # v0.3.13 p143: one GET to GitHub, never Bambuddy (`check_release_notes`)
}

# The one module that sends (checked with `check_source`, not with `check_holder`).
CLIENT_MODULE = "bambuddy/client.py"

# The second module that sends: push messages to Home Assistant (Spec 10.6). It is not a holder of
# the Bambuddy client; `check_home_assistant` watches that it can reach nothing else.
HOME_ASSISTANT_MODULE = "notify/homeassistant.py"
HOME_ASSISTANT_PATHS = {"/api/services/notify/", "/api/"}

# The third: the release notes (v0.3.13 p143, Spec 11.3a). One GET to GitHub's API, nothing else;
# `check_release_notes` watches it.
RELEASE_NOTES_MODULE = "ops/releasenotes.py"
RELEASE_NOTES_HOSTS = {"https://api.github.com", "https://github.com"}

# Modules that import websockets: the live channel only.
WEBSOCKETS_IMPORTERS = {"bambuddy/live.py"}

# Everything the live channel may import (module -> names; `None`: a plain `import module`).
LIVE_IMPORTS = {
    "__future__": {"annotations"},
    "asyncio": None,
    "contextlib": None,  # `suppress` around the closing handshake at a shutdown (v0.3.8 p80)
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

# Modules that import httpx or hold a client (`_holders()`), apart from the client itself, are all
# looked at with `check_holder`. These must be among them: the scope is not silently empty, and the
# helper files of the connection (v0.3.9 p75) are in.
CHECKED_AT_LEAST = HOLDERS_AT_LEAST | {
    "bambuddy/connection.py",
    "bambuddy/factory.py",
    "web/app.py",
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
GUARDED = HTTP_METHODS | {"_post", "_post_file", "_send"}


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

    for helper in ("_send", "_post", "_post_file"):
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
        keywords = {kw.arg for kw in call.keywords}
        if name == "_send":
            method = _text(_arg(call, 0, "method"))
            allowed = "POST" if where in POSTERS else "GET"
            if method != allowed:
                problems.append(f"_send with {method!r} in {where} (line {call.lineno})")
            if "files" in keywords and where != "_post_file":
                problems.append(f"_send with files in {where} (line {call.lineno})")
            if where == "_post_file" and "body" in keywords:
                problems.append(f"_send with body in _post_file (line {call.lineno})")
            if where in POSTERS:
                path = _arg(call, 1, "path")
                if not (isinstance(path, ast.Name) and path.id == "path"):
                    problems.append(f"_send in {where} with a path other than its parameter (line {call.lineno})")
            if where == "_post_file":
                problems += _query_from_folder_only(_arg(call, 99, "params"), call.lineno)
        elif name == "_post":
            path = _text(_arg(call, 0, "path"))
            if path not in WRITE_PATHS:
                problems.append(f"_post to {path!r} (line {call.lineno})")
            if "files" in keywords:
                problems.append(f"_post with files (line {call.lineno})")
        elif name == "_post_file":
            path = _text(_arg(call, 0, "path"))
            if path not in UPLOAD_PATHS:
                problems.append(f"_post_file to {path!r} (line {call.lineno})")
            if len(call.args) > 1 or None in keywords or not keywords <= {"path", "folder_id", "files"}:
                problems.append(f"_post_file with other arguments (line {call.lineno})")
    problems += _post_file_signature(text)
    problems += _posters_keep_their_inputs(text)
    problems += _request_arguments(scan)
    problems += _upload_files(scan)
    return problems


# What `.request(...)` inside `_send` may receive: nothing beyond the known parts.
REQUEST_KEYWORDS = {"params", "json", "files", "headers", "timeout", "follow_redirects"}


def _request_arguments(scan: _Scan) -> list[str]:
    problems = []
    for call, where in scan.calls:
        if _name(call.func) == "request" and where == "_send":
            keywords = {kw.arg for kw in call.keywords}
            if len(call.args) != 2 or None in keywords or not keywords <= REQUEST_KEYWORDS:
                problems.append(f"request with other arguments (line {call.lineno})")
    return problems


def _upload_files(scan: _Scan) -> list[str]:
    """`_post_file(..., files={"file": ...})`: one part, named `file`, written where it is sent."""
    problems = []
    for call, _ in scan.calls:
        if _name(call.func) != "_post_file":
            continue
        files = _arg(call, 99, "files")
        if not (
            isinstance(files, ast.Dict)
            and len(files.keys) == 1
            and _text(files.keys[0]) == "file"
        ):
            problems.append(f"_post_file with files other than one part 'file' (line {call.lineno})")
    return problems


def _posters_keep_their_inputs(text: str) -> list[str]:
    """In `_post` and `_post_file` the path is never changed, and in `_post_file` the query is
    made once, from `folder_id`, and only handed to `_send` (security review v0.3.9)."""
    problems: list[str] = []
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if not (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in POSTERS):
            continue
        parents = {id(child): parent for parent in ast.walk(node) for child in ast.iter_child_nodes(parent)}
        for inner in ast.walk(node):
            if isinstance(inner, ast.Name) and inner.id == "path" and not isinstance(inner.ctx, ast.Load):
                problems.append(f"path is changed in {node.name} (line {inner.lineno})")
            if isinstance(inner, ast.NamedExpr) and inner.target.id == "path":
                problems.append(f"path is changed in {node.name} (line {inner.lineno})")
        if node.name != "_post_file":
            continue
        uses = [n for n in ast.walk(node) if isinstance(n, ast.Name) and n.id == "params"]
        stores = [n for n in uses if isinstance(n.ctx, ast.Store)]
        loads = [n for n in uses if isinstance(n.ctx, ast.Load)]
        good_store = len(stores) == 1 and isinstance(parents.get(id(stores[0])), ast.Assign) and len(
            parents[id(stores[0])].targets
        ) == 1
        good_loads = all(
            isinstance(parents.get(id(n)), ast.keyword)
            and parents[id(n)].arg == "params"
            and isinstance(parents.get(id(parents[id(n)])), ast.Call)
            and _name(parents[id(parents[id(n)])].func) == "_send"
            for n in loads
        )
        if not (good_store and good_loads) or len(uses) != len(stores) + len(loads):
            problems.append(f"params is used other than made once and handed to _send (line {node.lineno})")
    return problems


def _query_from_folder_only(params: ast.AST | None, line: int) -> list[str]:
    """The query of an upload is built from `folder_id` alone (texts and names)."""
    if params is None:
        return []
    names = {n.id for n in ast.walk(params) if isinstance(n, ast.Name)}
    texts = {n.value for n in ast.walk(params) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    calls = [n for n in ast.walk(params) if isinstance(n, ast.Call)]
    if names - {"folder_id", "params"} or texts - {"folder_id"} or calls:
        return [f"_post_file query built from more than folder_id (line {line})"]
    if names == {"params"}:
        return []  # a local `params`: checked where it is assigned (`_post_file_signature`)
    return []


def _post_file_signature(text: str) -> list[str]:
    """`_post_file(self, path, *, folder_id, files)`, its query made of `folder_id` alone."""
    problems: list[str] = []
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_post_file":
            a = node.args
            shape = (
                [x.arg for x in a.posonlyargs],
                [x.arg for x in a.args],
                [x.arg for x in a.kwonlyargs],
                a.vararg,
                a.kwarg,
            )
            if shape != ([], ["self", "path"], ["folder_id", "files"], None, None):
                problems.append(f"_post_file has another signature (line {node.lineno})")
            for inner in ast.walk(node):
                if isinstance(inner, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "params" for t in inner.targets
                ):
                    found = _query_from_folder_only(inner.value, inner.lineno)
                    problems += [p for p in found if "params" not in p]
    return problems


def post_paths(text: str, helper: str = "_post") -> set[str | None]:
    return {
        _text(_arg(node, 0, "path"))
        for node in ast.walk(ast.parse(text))
        if isinstance(node, ast.Call) and _name(node.func) == helper
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


def _on_a_client(call: ast.Call) -> bool:
    """`getattr(<something called client or http>, ...)`: reaching a method of the client by text. Other
    `getattr` calls (settings of the app, optional attributes) are everyday code."""
    target = call.args[0] if call.args else None
    name = (_name(target) or "") if target is not None else ""
    return "client" in name.lower() or "http" in name.lower()


def check_holder(text: str) -> list[str]:
    """Every way a module holding an `httpx.AsyncClient` could send with it itself."""
    problems = []
    for node in ast.walk(ast.parse(text)):
        # the client's own helpers (`_post`, `_send`) and a name reached by text (`getattr`) too: a holder
        # that went round the public methods would be as much a write as one calling `.post` (v0.3.9 p75)
        if isinstance(node, ast.Call) and (
            _name(node.func) in GUARDED or (_name(node.func) == "getattr" and _on_a_client(node))
        ):
            problems.append(f"call of {_name(node.func)} (line {node.lineno})")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in GUARDED:
            problems.append(f"the name {node.value!r} appears as a text (line {node.lineno})")
    return problems


def _holders() -> set[str]:
    """Every module that takes part in sending through the client, other than the client itself:
    those that import httpx (the allow-list is checked on its own) and the web modules that
    hold a client made by `http_client_factory`."""
    held = {
        path.relative_to(SRC).as_posix()
        for path in (SRC / "web").rglob("*.py")
        if HOLDER_MARK in path.read_text(encoding="utf-8")
    }
    return (held | _importers("httpx") | HTTPX_IMPORTERS) - {CLIENT_MODULE, HOME_ASSISTANT_MODULE, RELEASE_NOTES_MODULE}


def check_home_assistant(text: str) -> list[str]:
    """Every way the Home Assistant module could reach Bambuddy or anything but its two calls: one
    HTTP call (`.request`) in the whole module, no import from the Bambuddy package, and no path
    of Home Assistant's API besides the notify services and the check `/api/`."""
    problems = []
    calls = 0
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("reprintbuddy.bambuddy"):
            problems.append(f"imports {node.module} (line {node.lineno})")
        if isinstance(node, ast.Import) and any(a.name.startswith("reprintbuddy.bambuddy") for a in node.names):
            problems.append(f"imports the Bambuddy package (line {node.lineno})")
        if isinstance(node, ast.Call) and _name(node.func) in GUARDED:
            if _name(node.func) == "request" and isinstance(node.func, ast.Attribute):
                calls += 1
            else:
                problems.append(f"call of {_name(node.func)} (line {node.lineno})")
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("/api")
            and node.value not in HOME_ASSISTANT_PATHS
        ):
            problems.append(f"path {node.value!r} (line {node.lineno})")
    if calls != 1:
        problems.append(f"{calls} HTTP calls instead of one")
    return problems


def check_release_notes(text: str) -> list[str]:
    """Every way the release notes module could send more than its one GET: exactly one HTTP call in
    the whole module, `.stream("GET", ...)`; no import from the Bambuddy package; no address but GitHub's."""
    problems = []
    calls = 0
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("reprintbuddy.bambuddy"):
            problems.append(f"imports {node.module} (line {node.lineno})")
        if isinstance(node, ast.Import) and any(a.name.startswith("reprintbuddy.bambuddy") for a in node.names):
            problems.append(f"imports the Bambuddy package (line {node.lineno})")
        if isinstance(node, ast.Call) and _name(node.func) in GUARDED:
            method = node.args[0] if node.args else None
            if (
                _name(node.func) == "stream"
                and isinstance(node.func, ast.Attribute)
                and isinstance(method, ast.Constant)
                and method.value == "GET"
            ):
                calls += 1
            else:
                problems.append(f"call of {_name(node.func)} (line {node.lineno})")
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith(("http://", "https://"))
            and node.value.rstrip("/") not in RELEASE_NOTES_HOSTS
        ):
            problems.append(f"address {node.value!r} (line {node.lineno})")
    if calls != 1:
        problems.append(f"{calls} HTTP calls instead of one")
    return problems


def test_the_release_notes_module_sends_nothing_but_one_get():
    assert check_release_notes((SRC / RELEASE_NOTES_MODULE).read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "bypass",
    [
        "from reprintbuddy.bambuddy.client import HttpBambuddyClient\n",
        "def f(client):\n    client.post('https://api.github.com/x')\n",
        "def f(client):\n    client.stream('POST', 'https://api.github.com/x')\n",
        "def f(client):\n    client.stream('GET', 'https://api.github.com/x')\n",  # a second call
        "OTHER = 'https://elsewhere.example/api'\n",
    ],
)
def test_the_release_notes_watch_catches_each_bypass(bypass):
    text = (SRC / RELEASE_NOTES_MODULE).read_text(encoding="utf-8") + "\n" + bypass
    assert check_release_notes(text) != []


def test_the_home_assistant_module_reaches_nothing_but_its_two_calls():
    assert check_home_assistant((SRC / HOME_ASSISTANT_MODULE).read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    "bypass",
    [
        "from reprintbuddy.bambuddy.client import HttpBambuddyClient\n",
        "async def f(http):\n    await http.post('/api/services/lock/unlock')\n",
        "async def f(http):\n    await http.request('POST', '/api/states/x')\n",
    ],
)
def test_the_home_assistant_watch_catches_each_bypass(bypass):
    text = (SRC / HOME_ASSISTANT_MODULE).read_text(encoding="utf-8") + "\n" + bypass
    assert check_home_assistant(text) != []


# ---- the real client -------------------------------------------------------------------


def test_only_the_allowed_posts():
    assert check_source(SOURCE) == []


def test_every_post_path_is_used():
    """The watch watches something: the write call and the token go through `_post`."""
    assert post_paths(SOURCE) == WRITE_PATHS


def test_the_upload_path_is_used():
    """The watch watches something: the upload goes through `_post_file`."""
    assert post_paths(SOURCE, "_post_file") == UPLOAD_PATHS


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
    assert CLIENT_MODULE in found  # the detection works at all
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
    assert CHECKED_AT_LEAST <= holders, holders  # the scope is not silently empty
    assert CLIENT_MODULE not in holders
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

    async def _post_file(self, path, *, folder_id, files):
        params = None if folder_id is None else {"folder_id": folder_id}
        return await self._send("POST", path, params=params, files=files)

    async def enqueue(self):
        return await self._post("/queue/", {})

    async def ws_token(self):
        return await self._post("/auth/ws-token")

    async def upload_file(self, folder_id, name, content):
        return await self._post_file("/library/files", folder_id=folder_id, files={"file": (name, content)})
'''


def _with(line: str) -> str:
    """`BASE` plus one method holding `line`."""
    method = f"\nasync def bad(self, http, suffix, url):\n    {line}\n"
    return BASE + textwrap.indent(method, "    ")


def test_base_passes():
    assert check_source(BASE) == []
    assert post_paths(BASE) == WRITE_PATHS
    assert post_paths(BASE, "_post_file") == UPLOAD_PATHS


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
    # the upload (v0.3.9 p20)
    "upload-elsewhere": ('await self._post_file("/library/folders/", folder_id=1, files={})', "_post_file to '/library/folders/'"),
    "upload-concatenated": ('await self._post_file("/library/files/" + suffix, folder_id=1, files={})', "_post_file to None"),
    "upload-variable-path": ("await self._post_file(suffix, folder_id=1, files={})", "_post_file to None"),
    "upload-more-arguments": ('await self._post_file("/library/files", folder_id=1, files={}, params={"x": 1})', "_post_file with other arguments"),
    "upload-spread-arguments": ('await self._post_file("/library/files", **suffix)', "_post_file with other arguments"),
    "upload-aliased": ("up = self._post_file", "_post_file is used without being called"),
    "upload-getattr": ('await getattr(self, "_post_file")("/library/files")', "the name '_post_file' appears as a text"),
    "post-with-files": ('await self._post("/queue/", {}, files={})', "_post with files"),
    "send-files-outside": ('await self._send("GET", "/x", files={})', "_send with files in bad"),
}


@pytest.mark.parametrize(("line", "reason"), BYPASSES.values(), ids=BYPASSES.keys())
def test_bypass_is_caught(line, reason):
    problems = check_source(_with(line))
    assert any(reason in problem for problem in problems), problems


def _base_with(old: str, new: str) -> str:
    assert old in BASE
    return BASE.replace(old, new, 1)


@pytest.mark.parametrize(
    ("old", "new", "reason"),
    [
        ('self._send("POST", path, params=params, files=files)', 'self._send("POST", "/library/folders/", params=params, files=files)', "_send in _post_file with a path other than its parameter"),
        ('self._send("POST", path, body=body)', 'self._send("POST", "/printers/1/x", body=body)', "_send in _post with a path other than its parameter"),
        ('self._send("POST", path, params=params, files=files)', 'self._send("POST", path, params=params, files=files, body={})', "_send with body in _post_file"),
        ('self._send("POST", path, params=params, files=files)', 'self._send("POST", path, params={"x": 1}, files=files)', "query built from more than folder_id"),
        ('{"folder_id": folder_id}', '{"folder_id": folder_id, "preserve_structure": 1}', "query built from more than folder_id"),
        ("params = None if folder_id is None", "params = extra if folder_id is None", "query built from more than folder_id"),
        ("async def _post_file(self, path, *, folder_id, files):", "async def _post_file(self, path, *, folder_id, files, **more):", "another signature"),
        ("async def _post_file(self, path, *, folder_id, files):", "async def _post_file(self, path, folder_id, files):", "another signature"),
        # security review v0.3.9: the path and the query must not be changed after the check
        ('        params = None if folder_id is None else {"folder_id": folder_id}\n',
         '        params = None if folder_id is None else {"folder_id": folder_id}\n        path = "/library/files/1/move"\n',
         "path is changed in _post_file"),
        ('        return await self._send("POST", path, body=body)',
         '        path = "/printers/1/stop"\n        return await self._send("POST", path, body=body)',
         "path is changed in _post"),
        ('        params = None if folder_id is None else {"folder_id": folder_id}\n',
         '        params = None if folder_id is None else {"folder_id": folder_id}\n        params["overwrite"] = 1\n',
         "params is used other than"),
        ('        params = None if folder_id is None else {"folder_id": folder_id}\n',
         '        params = None if folder_id is None else {"folder_id": folder_id}\n        params |= {"x": 1}\n',
         "params is used other than"),
        ('        params = None if folder_id is None else {"folder_id": folder_id}\n',
         '        params: dict = {"folder_id": folder_id, "x": "1"}\n',
         "params is used other than"),
        ('        params = None if folder_id is None else {"folder_id": folder_id}\n',
         '        params = None if folder_id is None else {"folder_id": folder_id}\n        params = params or {}\n',
         "params is used other than"),
        ('        params = None if folder_id is None else {"folder_id": folder_id}\n',
         '        params = None if folder_id is None else {"folder_id": folder_id}\n        params.update(x=1)\n',
         "params is used other than"),
        ("return await http.request(method, path, params=params, json=body)",
         "return await http.request(method, path, params=params, json=body, data={'x': 1})",
         "request with other arguments"),
        ('files={"file": (name, content)})', 'files={"file": (name, content), "folder_id": "9"})', "files other than"),
        ('files={"file": (name, content)})', "files=extra)", "files other than"),
    ],
)
def test_upload_helper_bypass_is_caught(old, new, reason):
    problems = check_source(_base_with(old, new))
    assert any(reason in problem for problem in problems), problems


def test_put_through_send_is_caught():
    changed = BASE.replace('_send("GET", path)', '_send("PUT", path)')
    # the line is looked up in the text, not written down: a change of `BASE` must not break this
    line = next(n for n, text in enumerate(changed.splitlines(), 1) if '_send("PUT", path)' in text)
    assert f"_send with 'PUT' in _get (line {line})" in check_source(changed)


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
    "getattr": ('await getattr(client, "patch")(url)', "call of getattr"),
    "getattr-variable": ("await getattr(client, name)(url)", "call of getattr"),
    "send-helper": ('await client._send("DELETE", "/queue/1")', "call of _send"),
    "post-helper": ('await client._post("/queue/1/start", {})', "call of _post"),
    "helper-as-text": ('helper = "_post"', "the name '_post' appears as a text"),
}


def test_holder_base_passes():
    assert check_holder(HOLDER_BASE) == []


@pytest.mark.parametrize(("line", "reason"), HOLDER_BYPASSES.values(), ids=HOLDER_BYPASSES.keys())
def test_holder_bypass_is_caught(line, reason):
    anchor = "        return await check_connection"
    bad = HOLDER_BASE.replace(anchor, f"        {line}\n{anchor}")
    problems = check_holder(bad)
    assert any(reason in problem for problem in problems), problems


@pytest.mark.parametrize("module", ["bambuddy/connection.py", "bambuddy/factory.py", "web/app.py"])
def test_a_write_added_to_a_helper_of_the_connection_is_caught(module):
    """v0.3.9 p75: the helper files that import httpx are looked at as the real files are."""
    assert module in _holders()
    real = (SRC / module).read_text(encoding="utf-8")
    assert check_holder(real) == []
    bad = real + "\n\nasync def sneaky(client):\n    return await client.post('/queue/1/start')\n"
    assert any("call of post" in problem for problem in check_holder(bad))
