"""The health check after a restart, against a real local server.

It uses only the standard library: pip replaces the installed packages while the helper
runs, so anything imported lazily afterwards could be a different version.
"""

import ast
import json
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from reprintbuddy import settings as st
from reprintbuddy.db import make_engine, make_sessionmaker, upgrade
from reprintbuddy.ops import updater
from reprintbuddy.ops.updater import http_health_waiter


class Server:
    def __init__(self, status=200, body=b'{"status": "ok"}', tls_pair=None):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.paths.append(self.path)
                self.send_response(status if self.path == "/healthz" else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.paths = []
        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        if tls_pair:
            import tempfile

            with tempfile.TemporaryDirectory() as folder:
                cert, key = Path(folder, "c.pem"), Path(folder, "k.pem")
                cert.write_text(tls_pair[0], encoding="utf-8")
                key.write_text(tls_pair[1], encoding="utf-8")
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(str(cert), str(key))
            self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def database(tmp_path, **values):
    path = tmp_path / "h.db"
    engine = make_engine(path)
    upgrade(engine)
    with make_sessionmaker(engine)() as session:
        for key, value in values.items():
            st.set_setting(session, key, value)
    engine.dispose()
    return path


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_health_waiter(tmp_path):
    with Server() as server:
        db = database(tmp_path, port=server.port, host="127.0.0.1")
        assert http_health_waiter(db)() is True
    assert server.paths == ["/healthz"]


def test_wildcard_host_means_this_machine(tmp_path):
    with Server() as server:
        db = database(tmp_path, port=server.port)  # host stays the default 0.0.0.0
        assert http_health_waiter(db)() is True


def test_health_waiter_gives_up(tmp_path):
    with Server(status=503) as server:
        db = database(tmp_path, port=server.port)
        assert http_health_waiter(db, timeout_s=0.3, interval_s=0.1)() is False
        assert len(server.paths) >= 2  # it kept asking until the time was up


def test_a_wrong_body_is_not_healthy(tmp_path):
    with Server(body=json.dumps({"status": "starting"}).encode()) as server:
        db = database(tmp_path, port=server.port)
        assert http_health_waiter(db, timeout_s=0.3, interval_s=0.1)() is False


def test_health_waiter_survives_a_closed_port(tmp_path):
    db = database(tmp_path, port=free_port())
    assert http_health_waiter(db, timeout_s=0.3, interval_s=0.1)() is False


def test_health_waiter_follows_the_saved_https_address(tmp_path):
    from conftest import make_tls_pair

    cert, key = make_tls_pair()
    with Server(tls_pair=(cert, key)) as server:
        db = database(tmp_path, port=server.port, host="127.0.0.1", tls_cert=cert, tls_key=key)
        assert http_health_waiter(db)() is True  # self-made certificate: not verified


def test_health_waiter_ignores_proxy_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    with Server() as server:
        db = database(tmp_path, port=server.port)
        assert http_health_waiter(db)() is True


def test_missing_database_is_not_healthy(tmp_path):
    assert http_health_waiter(tmp_path / "none.db", timeout_s=0.2, interval_s=0.1)() is False
    assert not (tmp_path / "none.db").exists()


STDLIB = set(__import__("sys").stdlib_module_names)


@pytest.mark.parametrize("module", ["updater", "backup", "runner", "service", "status", "versions"])
def test_update_helper_modules_import_only_the_standard_library(module):
    """Imports done lazily after `pip install` could pick up a different version of a package."""
    source = (Path(updater.__file__).parent / f"{module}.py").read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module)
    for name in imported:
        top = name.split(".")[0]
        assert top in STDLIB or name.startswith("reprintbuddy.ops."), (module, name)
