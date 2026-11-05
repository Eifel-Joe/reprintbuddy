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


# --- the less privileged process must not get at root's terminal (TIOCSTI) ---------------

CLI_SH = INSTALL_SH.parent / "reprintbuddy-cli.sh"
SERVICE_USER_PREFIX = "+ setsid --wait runuser -u reprintbuddy -- env HOME=/var/lib/reprintbuddy"


def test_service_user_commands_run_in_a_new_session(bash, tmp_path):
    os_release(tmp_path)
    out = lines(run(bash, tmp_path, "--repo", REPO))
    service_user_lines = [l for l in out.splitlines() if " runuser " in l]
    assert service_user_lines, "no command ran as the service user?"
    for line in service_user_lines:
        assert line.startswith(SERVICE_USER_PREFIX), line
    assert not [l for l in out.splitlines() if l.startswith("+ runuser")]


def test_the_installer_cuts_stdin_of_every_command():
    assert '"$@" </dev/null' in function_body("run")
    assert "</dev/null" in function_body("capture_as_service_user")
    assert "setsid --wait runuser" in function_body("capture_as_service_user")


def wrapper(bash, tmp_path, *args):
    """Run the wrapper as 'root' with stubs that print what they would run."""
    folder = tmp_path / "stubs"
    folder.mkdir(exist_ok=True)
    for name, body in (("id", "echo 0"), ("runuser", 'echo "runuser $*"'), ("setsid", 'echo "setsid $*"')):
        stub = folder / name
        stub.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8", newline="\n")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    env = dict(os.environ, PATH=f"{folder.as_posix()}{os.pathsep}{os.environ['PATH']}")
    done = subprocess.run([bash, CLI_SH.as_posix(), *args], capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.mark.parametrize("command", [["set-port", "8081"], ["set-host", "0.0.0.0"], ["disable-https"], ["--version"]])
def test_wrapper_runs_non_interactive_commands_in_a_new_session(bash, tmp_path, command):
    out = wrapper(bash, tmp_path, *command)
    assert out.startswith("setsid --wait runuser -u reprintbuddy -- env HOME=/var/lib/reprintbuddy ")
    assert out.endswith(" ".join(command))


def test_wrapper_keeps_the_terminal_for_the_password_prompt(bash, tmp_path):
    """getpass needs the controlling terminal; a new session would show the password."""
    out = wrapper(bash, tmp_path, "set-password", "--username", "admin")
    assert out.startswith("runuser -u reprintbuddy -- env HOME=/var/lib/reprintbuddy ")
    assert "setsid" not in out


# --- an installer from `main` must not install units that belong to another version --------

INSTALL_DIR_FILES = ("reprintbuddy.sudoers", "reprintbuddy.service", "reprintbuddy-update.service", "reprintbuddy-cli.sh")


def installed_tree(dest: Path, *, change: str | None = None) -> None:
    """An existing installation whose install/ files are those of the repository (or one changed)."""
    tree = dest / "opt/reprintbuddy"
    (tree / ".git").mkdir(parents=True)
    (tree / "install").mkdir()
    for name in INSTALL_DIR_FILES:
        text = (INSTALL_SH.parent / name).read_text(encoding="utf-8")
        if name == change:
            text += "# changed in another version\n"
        (tree / "install" / name).write_text(text, encoding="utf-8", newline="\n")


def test_matching_installer_and_code_go_on(bash, tmp_path):
    os_release(tmp_path)
    installed_tree(tmp_path)
    result = run(bash, tmp_path, "--repo", REPO)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("changed", INSTALL_DIR_FILES)
def test_an_installer_that_does_not_match_the_installed_code_stops(bash, tmp_path, changed):
    os_release(tmp_path)
    installed_tree(tmp_path, change=changed)
    result = run(bash, tmp_path, "--repo", REPO)
    assert result.returncode == 1
    message = result.stderr + result.stdout
    assert "The installer does not match the installed version" in message
    assert changed in message
    assert "Use the install.sh of the tag" in message
    out = lines(result)
    assert "visudo" not in out and "/etc/sudoers.d/" not in out, "nothing may be installed before the check"


def test_the_check_reads_only(bash, tmp_path):
    os_release(tmp_path)
    installed_tree(tmp_path, change="reprintbuddy.service")
    before = {p: p.read_bytes() for p in (tmp_path / "opt").rglob("*") if p.is_file()}
    run(bash, tmp_path, "--repo", REPO)
    assert {p: p.read_bytes() for p in (tmp_path / "opt").rglob("*") if p.is_file()} == before


def test_the_check_is_skipped_for_a_dry_run_that_clones_nothing(bash, tmp_path):
    os_release(tmp_path)  # nothing installed: there is no tree to compare with
    assert run(bash, tmp_path, "--repo", REPO).returncode == 0
