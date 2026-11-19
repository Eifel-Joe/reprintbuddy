"""The steps of the end-to-end run in the interface (tests/system/websteps.py), against a real `reprintbuddy serve`.

What needs the installation, systemd and the update helper (update, way back, a restart after a restore) runs only
in the end-to-end job of the CI (.github/workflows/e2e.yml). Here the service is a process of this test: after a
restore it cannot restart itself (no sudo), so the test restarts it, as systemd does in the container.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

import websteps
from fakebambuddy import FakeBambuddy
from webclient import Browser

USER, PASSWORD, BACKUP_PASSWORD = "e2e-admin", "e2e-login-password-1", "e2e-backup-password-1"
KEY = "e2e-key-0123456789abcdef"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Service:
    def __init__(self, tmp_path: Path) -> None:
        self.db = tmp_path / "data" / "reprintbuddy.db"
        self.db.parent.mkdir()
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.cwd = tmp_path
        self.process: subprocess.Popen[bytes] | None = None
        self.cli("init", "--language", "en")
        self.cli("set-host", "127.0.0.1")
        self.cli("set-port", str(self.port))

    def cli(self, *args: str) -> None:
        command, rest = args[0], args[1:]
        subprocess.run([sys.executable, "-m", "reprintbuddy", command, "--db", str(self.db), *rest], check=True, cwd=self.cwd, capture_output=True)

    def start(self) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-m", "reprintbuddy", "serve", "--db", str(self.db)], cwd=self.cwd,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(self.url + "/healthz", timeout=2):
                    return
            except OSError:
                time.sleep(0.2)
        raise AssertionError("the service did not answer")

    def stop(self) -> None:
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(30)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None


@pytest.fixture
def bambuddy(tmp_path: Path):
    fake = FakeBambuddy(KEY, log=tmp_path / "bambuddy.log")
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def service(tmp_path: Path):
    running = Service(tmp_path)
    running.start()
    yield running
    running.stop()


def test_setup_backup_and_restore_through_the_pages(service: Service, bambuddy: FakeBambuddy) -> None:
    web = Browser(service.url)
    websteps.set_up(web, USER, PASSWORD, bambuddy.url, KEY)
    assert websteps.settings_reachable(web)
    websteps.add_part(web, "E2E part A")
    assert websteps.has_part(web, "E2E part A")

    backup = websteps.download_backup(web, login_password=PASSWORD, backup_password=BACKUP_PASSWORD)
    assert backup[:2] == b"PK"

    websteps.add_part(web, "E2E part B")
    assert websteps.has_part(web, "E2E part B")
    done = websteps.restore(web, backup, backup_password=BACKUP_PASSWORD, login_password=PASSWORD)
    assert done.status == 200
    # here nobody can restart the service (no sudo): the page asks for it; in the container systemd restarts it
    service.stop()
    service.start()
    web = Browser(service.url)
    websteps.log_in(web, USER, PASSWORD)
    assert websteps.has_part(web, "E2E part A")
    assert not websteps.has_part(web, "E2E part B")
    log = websteps.log_text(web)
    assert "Restored from a backup" in log
    assert bambuddy.unexpected == []


def test_a_wrong_password_is_seen_as_a_failed_step(service: Service, bambuddy: FakeBambuddy) -> None:
    web = Browser(service.url)
    websteps.set_up(web, USER, PASSWORD, bambuddy.url, KEY)
    with pytest.raises(websteps.StepFailed, match="backup"):
        websteps.download_backup(web, login_password="not-the-password", backup_password=BACKUP_PASSWORD)
    web = Browser(service.url)
    with pytest.raises(websteps.StepFailed, match="log in"):
        websteps.log_in(web, USER, "not-the-password")


def test_the_card_of_the_update_is_read_as_json(service: Service, bambuddy: FakeBambuddy) -> None:
    web = Browser(service.url)
    websteps.set_up(web, USER, PASSWORD, bambuddy.url, KEY)
    status = websteps.update_status(web)
    assert status["state"] == "idle"
