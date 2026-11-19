"""The helpers of the end-to-end run (tests/system/scenario.py) that can be tried outside the container.

The run itself (installer, systemd, update helper, stop from outside) only runs in the end-to-end job of the CI."""

from __future__ import annotations

import json
import shutil
import socket
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

import scenario


def test_the_test_authority_is_appended_once(tmp_path: Path) -> None:
    bundle = tmp_path / "cacert.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\nA\n-----END CERTIFICATE-----\n", encoding="ascii")
    pem = "-----BEGIN CERTIFICATE-----\nB\n-----END CERTIFICATE-----\n"
    assert scenario.append_once(bundle, pem) is True
    assert scenario.append_once(bundle, pem) is False
    assert bundle.read_text(encoding="ascii").count("B\n") == 1
    assert bundle.read_text(encoding="ascii").startswith("-----BEGIN CERTIFICATE-----\nA\n")


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is not installed")
def test_the_authority_of_the_run_passes_the_strict_check_of_python(tmp_path: Path) -> None:
    """Python 3.13 checks certificates strictly (VERIFY_X509_STRICT): the run's own authority must pass it, for
    the update helper (urllib) and for the pages (httpx) alike."""
    files = scenario.make_authority(tmp_path)
    server_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server_context.load_cert_chain(files.server_bundle)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = ssl.create_default_context(cafile=str(files.ca))
        assert client.verify_flags & ssl.VERIFY_X509_STRICT
        for name in ("github.com", "api.github.com"):
            with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=10) as raw:
                with client.wrap_socket(raw, server_hostname=name) as tls:
                    tls.sendall(f"GET / HTTP/1.0\r\nHost: {name}\r\n\r\n".encode())
                    assert tls.recv(100).startswith(b"HTTP/1.0 200")
        with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=10) as raw:
            with pytest.raises(ssl.SSLCertVerificationError):
                client.wrap_socket(raw, server_hostname="example.org")
    finally:
        server.shutdown()
        server.server_close()


def test_the_stop_waits_for_the_step_in_the_status_file(tmp_path: Path) -> None:
    status = tmp_path / "update-status.json"
    seen: list[str] = []
    steps = iter(["backup", "fetch", "checkout", "stop", "database"])

    def advance() -> None:
        step = next(steps)
        seen.append(step)
        status.write_text(json.dumps({"state": "running", "step": step}), encoding="utf-8")

    advance()
    found = scenario.wait_for_step(status, ("stop", "database"), timeout=5, tick=advance)
    assert found == "stop"
    assert seen == ["backup", "fetch", "checkout", "stop"]


def test_the_wait_for_a_step_gives_up_when_the_run_is_over(tmp_path: Path) -> None:
    status = tmp_path / "update-status.json"
    status.write_text(json.dumps({"state": "succeeded", "step": "done"}), encoding="utf-8")
    with pytest.raises(scenario.RunFailed, match="ended"):
        scenario.wait_for_step(status, ("install",), timeout=5)
    status.write_text(json.dumps({"state": "running", "step": "fetch"}), encoding="utf-8")
    with pytest.raises(scenario.RunFailed, match="install"):
        scenario.wait_for_step(status, ("install",), timeout=0.3)


@pytest.mark.parametrize(
    ("at", "state", "reason", "ok"),
    [
        ("install", "rolled_back", "interrupted_rolled_back", True),
        ("install", "failed", "interrupted_untouched", False),
        ("stop", "failed", "interrupted_untouched", True),
        ("stop", "rolled_back", "interrupted_rolled_back", True),  # the signal came only after "swap"
        ("stop", "rollback_failed", "interrupted_partial", False),
        ("stop", "succeeded", "", False),
    ],
)
def test_what_a_stop_from_outside_may_leave(at: str, state: str, reason: str, ok: bool) -> None:
    status = {"state": state, "reason": reason, "kind": "return"}
    if ok:
        scenario.check_interrupted(status, at)
    else:
        with pytest.raises(scenario.RunFailed):
            scenario.check_interrupted(status, at)


def test_the_texts_come_from_the_installed_version(tmp_path: Path) -> None:
    locales = tmp_path / "releases" / "v1.2.1-beta.1" / "src" / "reprintbuddy" / "locales"
    locales.mkdir(parents=True)
    (locales / "en.json").write_text(json.dumps({"a.b": "Text &amp; more"}), encoding="utf-8")
    texts = scenario.Texts(tmp_path, "v1.2.1-beta.1")
    assert texts["a.b"] == "Text &amp; more"
    with pytest.raises(scenario.RunFailed, match="x.y"):
        texts["x.y"]


@pytest.mark.skipif(sys.platform == "win32", reason="a pseudo terminal exists on Linux only")
def test_a_password_is_typed_on_a_terminal() -> None:
    """`reprintbuddy restore-backup` asks with getpass on the terminal: the run types it on a pseudo terminal."""
    code = "import getpass, sys; secret = getpass.getpass('Password of the backup: '); print('got', len(secret)); sys.exit(7)"
    returncode, output = scenario.answer_on_terminal([sys.executable, "-c", code], "Password of the backup:", "s3cret", timeout=30)
    assert returncode == 7
    assert "got 6" in output
    assert "s3cret" not in output
