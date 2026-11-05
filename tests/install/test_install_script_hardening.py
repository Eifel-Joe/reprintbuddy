"""Behaviour of the install script that the first version of its tests did not pin down."""

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
from test_install_script import INSTALL_SH, REPO, bash, lines, os_release, run  # noqa: F401


def script() -> str:
    return INSTALL_SH.read_text(encoding="utf-8")


def function_body(name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n(.*?)\n\}}\n", script(), re.S | re.M)
    assert match, name
    return match.group(1)


def test_a_failing_hostname_lookup_does_not_abort_the_script():
    """Under `set -e -o pipefail` a pipeline that fails inside $(...) ends the script."""
    line = next(l for l in script().splitlines() if "hostname -I" in l)
    assert "| awk '{print $1}' || true)" in line


def test_the_health_loop_cannot_hang_on_curl():
    assert re.search(r"curl [^\n]*--max-time 5", script())


def test_the_repository_url_is_checked_before_the_language_is_asked():
    body = function_body("main")
    assert body.index("require_repo_url") < body.index("choose_language")


def test_a_rerun_does_not_ask_for_the_language():
    """Nothing uses the language when the database exists (init is skipped)."""
    assert "DB_FILE" in function_body("choose_language")


def test_permissions_are_repaired_on_every_run(bash, tmp_path):
    os_release(tmp_path)
    out = lines(run(bash, tmp_path, "--repo", REPO))
    assert "find /var/lib/reprintbuddy -xdev" in out
    assert "chown -h reprintbuddy:reprintbuddy" in out
    assert out.index("chmod 700 /var/lib/reprintbuddy") < out.index("find /var/lib/reprintbuddy -xdev")


def stub_systemctl(folder: Path, answer: str) -> dict:
    folder.mkdir(exist_ok=True)
    stub = folder / "systemctl"
    stub.write_text(f"#!/bin/sh\necho {answer}\n", encoding="utf-8", newline="\n")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return {"PATH": f"{folder.as_posix()}{os.pathsep}{os.environ['PATH']}"}


@pytest.mark.parametrize("answer", ["activating", "active", "deactivating", "reloading"])
def test_the_installer_waits_while_an_update_runs(bash, tmp_path, answer):
    os_release(tmp_path)
    result = run(bash, tmp_path, "--repo", REPO, env_extra=stub_systemctl(tmp_path / "bin", answer))
    assert result.returncode == 1
    assert "An update is running. Run the installer again when it has finished." in result.stderr + result.stdout
    assert not [l for l in lines(result).splitlines() if l.startswith("+")], "nothing may change"


@pytest.mark.parametrize("answer", ["inactive", "failed", ""])
def test_the_installer_goes_on_when_no_update_runs(bash, tmp_path, answer):
    os_release(tmp_path)
    result = run(bash, tmp_path, "--repo", REPO, env_extra=stub_systemctl(tmp_path / "bin", answer))
    assert result.returncode == 0, result.stderr


def test_the_installer_goes_on_without_systemctl(bash, tmp_path):
    os_release(tmp_path)
    assert run(bash, tmp_path, "--repo", REPO).returncode == 0
