"""The time zone step of the installer (Spec 11.3a "Time zone", p718).

In a terminal the installer shows the time zone of the server and the time it gives, and asks whether that is
right (default yes); on no it opens Debian's full selection (`dpkg-reconfigure tzdata`). Nothing is typed in
freely. Without a terminal it asks nothing; `--timezone ZONE` sets a zone from the list for scripted
installations and stops on an unknown name before anything is set up.

Two kinds of tests: the whole script in a dry run (as the other installer tests), and the function itself, with
the script loaded without its last line (`main "$@"`), `DRY_RUN=0` and stubs on PATH that write down how they
were called. The function asks whether stdin is a terminal through `stdin_is_terminal`, which a test replaces.
"""

import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from test_install_script import INSTALL_SH, REPO, bash, fake_python, lines, os_release, run  # noqa: F401

ZONES = ["Etc/UTC", "Europe/Berlin", "Europe/Vienna", "America/New_York", "Etc/GMT+5", "UTC"]
QUESTION = re.compile(r"Time zone of this server: (\S+), time now \d\d:\d\d\. Is that right\? \[Y/n\]")


def stubs(folder: Path, *, current: str = "Etc/UTC", list_fails: bool = False, set_fails: bool = False,
          reconfigure_fails: bool = False) -> tuple[dict, Path]:
    """`timedatectl` and `dpkg-reconfigure` that write every call to calls.log. After `set-timezone` or a
    selection, `show` answers the new zone (Europe/Vienna for the selection)."""
    folder.mkdir(parents=True, exist_ok=True)
    log = folder / "calls.log"
    state = folder / "zone"
    state.write_text(current + "\n", encoding="utf-8", newline="\n")
    listing = "\\n".join(ZONES)
    scripts = {
        "timedatectl": (
            "#!/bin/sh\n"
            f'echo "timedatectl $*" >> "{log.as_posix()}"\n'
            'case "$1" in\n'
            f'  show) cat "{state.as_posix()}" ;;\n'
            + (f"  list-timezones) exit 1 ;;\n" if list_fails else f"  list-timezones) printf '{listing}\\n' ;;\n")
            + (f"  set-timezone) exit 1 ;;\n" if set_fails else f'  set-timezone) echo "$2" > "{state.as_posix()}" ;;\n')
            + "esac\n"
        ),
        "dpkg-reconfigure": (
            "#!/bin/sh\n"
            f'echo "dpkg-reconfigure $* DEBIAN_FRONTEND=$DEBIAN_FRONTEND" >> "{log.as_posix()}"\n'
            + ("exit 10\n" if reconfigure_fails else f'echo Europe/Vienna > "{state.as_posix()}"\n')
        ),
    }
    for name, body in scripts.items():
        path = folder / name
        path.write_text(body, encoding="utf-8", newline="\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return {"PATH": f"{folder.as_posix()}{os.pathsep}{os.environ['PATH']}"}, log


def calls(log: Path) -> list[str]:
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def changes(log: Path) -> list[str]:
    return [c for c in calls(log) if c.startswith(("timedatectl set-timezone", "dpkg-reconfigure"))]


# --- the whole script in a dry run --------------------------------------------------------


def test_without_a_terminal_nothing_is_asked_and_nothing_is_called(bash, tmp_path):
    os_release(tmp_path)
    env, log = stubs(tmp_path / "stubs")
    result = run(bash, tmp_path, "--repo", REPO, env_extra=env)
    assert result.returncode == 0, result.stderr
    assert "Is that right?" not in result.stdout + result.stderr
    assert "time zone" not in lines(result).lower()
    assert calls(log) == []


def test_a_valid_zone_given_as_option_is_set(bash, tmp_path):
    os_release(tmp_path)
    env, log = stubs(tmp_path / "stubs")
    result = run(bash, tmp_path, "--repo", REPO, "--timezone", "Europe/Berlin", env_extra=env)
    assert result.returncode == 0, result.stderr
    out = lines(result)
    assert "+ timedatectl set-timezone Europe/Berlin" in out
    # checked against the list, but a dry run sets nothing
    assert "timedatectl list-timezones" in calls(log)
    assert changes(log) == []
    # after the packages (tzdata comes with them), before the user and the code
    assert out.index("apt-get install") < out.index("timedatectl set-timezone") < out.index("useradd")


@pytest.mark.parametrize(
    "zone", ["Europe/Atlantis", "europe/berlin", "localtime", "posixrules", "", "Europe/Berlin ", "../UTC"]
)
def test_an_unknown_zone_stops_before_anything_is_set_up(bash, tmp_path, zone):
    os_release(tmp_path)
    env, log = stubs(tmp_path / "stubs")
    result = run(bash, tmp_path, "--repo", REPO, "--timezone", zone, env_extra=env)
    assert result.returncode == 2
    assert "Unknown time zone" in result.stderr
    assert "timedatectl list-timezones" in result.stderr  # where the names come from
    out = lines(result)
    assert "set-timezone" not in out and "useradd" not in out and "/opt/reprintbuddy" not in out
    assert changes(log) == []


def test_without_a_list_of_zones_the_option_stops_with_a_message(bash, tmp_path):
    os_release(tmp_path)
    env, log = stubs(tmp_path / "stubs", list_fails=True)
    result = run(bash, tmp_path, "--repo", REPO, "--timezone", "Europe/Berlin", env_extra=env)
    assert result.returncode == 1
    assert "The time zone cannot be checked here" in result.stderr
    assert "useradd" not in lines(result)
    assert changes(log) == []


def test_the_option_needs_a_value(bash, tmp_path):
    os_release(tmp_path)
    result = run(bash, tmp_path, "--repo", REPO, "--timezone")
    assert result.returncode == 2
    assert "Missing value for --timezone." in result.stderr


def test_help_names_the_option(bash, tmp_path):
    result = run(bash, tmp_path, "--help", dry=False, destdir=False)
    assert "--timezone ZONE" in lines(result)


def test_tzdata_is_one_of_the_packages(bash, tmp_path):
    """The list of zones and the selection come from tzdata; minimal Debian systems can be without it."""
    os_release(tmp_path)
    out = lines(run(bash, tmp_path, "--repo", REPO))
    install = next(line for line in out.splitlines() if "apt-get install" in line)
    assert " tzdata" in install


# --- the function itself, with stubs ------------------------------------------------------


def call(bash, tmp_path, *, terminal: bool, answer: str = "", choice: str | None = None, dry: bool = False,
         **stub_options):
    """Load the script without its last line, then run choose_time_zone with the given answer on stdin;
    `choice` is what --timezone would set."""
    body = INSTALL_SH.read_text(encoding="utf-8").rstrip("\n")
    assert body.endswith('main "$@"')
    library = tmp_path / "install-lib.sh"
    library.write_text(body[: -len('main "$@"')], encoding="utf-8", newline="\n")
    env, log = stubs(tmp_path / "stubs", **stub_options)
    env = dict(os.environ, **env, DEBIAN_FRONTEND="noninteractive")  # main exports this before the step runs
    env.pop("TZ", None)
    program = (
        f'source "{library.as_posix()}"\n'
        f"DRY_RUN={1 if dry else 0}\n"
        + (f"TIME_ZONE_CHOICE={shlex_quote(choice)}\nTIME_ZONE_GIVEN=1\n" if choice is not None else "")
        + f"stdin_is_terminal() {{ return {0 if terminal else 1}; }}\n"
        "choose_time_zone\n"
        'echo "done"\n'
    )
    # bytes, not text: on Windows text mode would send "\r\n" for each newline, which no terminal does
    done = subprocess.run([bash, "-c", program], input=answer.encode(), capture_output=True, env=env, timeout=60)
    result = subprocess.CompletedProcess(
        done.args, done.returncode, done.stdout.decode("utf-8").replace("\r\n", "\n"), done.stderr.decode("utf-8")
    )
    return result, log


def shlex_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


@pytest.mark.parametrize("answer", ["\n", "y\n", "Y\n", "yes\n", ""])
def test_in_a_terminal_yes_or_enter_changes_nothing(bash, tmp_path, answer):
    result, log = call(bash, tmp_path, terminal=True, answer=answer, current="Etc/UTC")
    assert result.returncode == 0, result.stderr
    assert QUESTION.search(result.stdout).group(1) == "Etc/UTC"
    assert result.stdout.rstrip().endswith("done")
    assert changes(log) == []


@pytest.mark.parametrize("answer", ["n\n", "N\n", "no\n", "maybe\nn\n"])
def test_in_a_terminal_no_opens_debians_selection(bash, tmp_path, answer):
    result, log = call(bash, tmp_path, terminal=True, answer=answer)
    assert result.returncode == 0, result.stderr
    # the selection asks, so it must not get the noninteractive frontend the rest of the script uses
    assert changes(log) == ["dpkg-reconfigure tzdata DEBIAN_FRONTEND=dialog"]
    assert "set-timezone" not in "\n".join(calls(log))  # nothing typed in freely
    assert re.search(r"Time zone of this server is now Europe/Vienna, time now \d\d:\d\d\.", result.stdout)


def test_an_unclear_answer_asks_again(bash, tmp_path):
    result, _ = call(bash, tmp_path, terminal=True, answer="maybe\n\n")
    assert len(QUESTION.findall(result.stdout)) == 1  # the zone line once
    assert result.stdout.count("[Y/n]") == 2


def test_a_cancelled_selection_does_not_stop_the_installation(bash, tmp_path):
    result, log = call(bash, tmp_path, terminal=True, answer="n\n", reconfigure_fails=True)
    assert result.returncode == 0, result.stderr
    assert "The time zone selection did not finish." in result.stdout
    assert re.search(r"Time zone of this server is now Etc/UTC, time now \d\d:\d\d\.", result.stdout)
    assert result.stdout.rstrip().endswith("done")


def test_without_a_terminal_the_function_asks_nothing(bash, tmp_path):
    result, log = call(bash, tmp_path, terminal=False, answer="n\n")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "done"
    assert calls(log) == []


@pytest.mark.parametrize("terminal", [False, True])
def test_the_option_sets_the_zone_and_asks_nothing(bash, tmp_path, terminal):
    result, log = call(bash, tmp_path, terminal=terminal, answer="n\n", choice="America/New_York")
    assert result.returncode == 0, result.stderr
    assert "[Y/n]" not in result.stdout
    assert changes(log) == ["timedatectl set-timezone America/New_York"]
    assert re.search(r"Time zone of this server is now America/New_York, time now \d\d:\d\d\.", result.stdout)


@pytest.mark.parametrize("zone", ["Mars/Olympus", "Etc/UTC\nEurope/Berlin", "Europe/*", ""])
def test_an_unknown_zone_sets_nothing(bash, tmp_path, zone):
    """Two names on two lines are two lines of the list, not one name. Checked here and not on the command line:
    on Windows an argument with a newline does not reach bash in one piece."""
    result, log = call(bash, tmp_path, terminal=True, choice=zone)
    assert result.returncode == 2
    assert f"Unknown time zone: {zone}." in result.stderr
    assert changes(log) == []
    assert "done" not in result.stdout


def test_a_failing_set_timezone_stops_with_a_message(bash, tmp_path):
    result, log = call(bash, tmp_path, terminal=False, choice="Europe/Berlin", set_fails=True)
    assert result.returncode == 1
    assert "The time zone could not be set" in result.stderr
    assert "done" not in result.stdout


def test_a_dry_run_in_a_terminal_shows_the_selection_but_does_not_open_it(bash, tmp_path):
    result, log = call(bash, tmp_path, terminal=True, answer="n\n", dry=True)
    assert result.returncode == 0, result.stderr
    assert "+ DEBIAN_FRONTEND=dialog dpkg-reconfigure tzdata" in result.stdout
    assert changes(log) == []


# --- how the script is written --------------------------------------------------------------


def script() -> str:
    return INSTALL_SH.read_text(encoding="utf-8")


def function_body(name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{\n(.*?)\n\}}\n", script(), re.S | re.M)
    assert match, name
    return match.group(1)


def test_the_terminal_question_is_the_one_of_the_language():
    assert function_body("stdin_is_terminal").strip() == "[ -t 0 ]"


def test_the_selection_keeps_the_terminal():
    """`run` cuts stdin; the selection needs the terminal and must therefore not go through it."""
    body = function_body("choose_time_zone") + function_body("select_time_zone")
    assert "run dpkg-reconfigure" not in body
    assert "DEBIAN_FRONTEND=dialog dpkg-reconfigure tzdata" in body


def test_the_step_comes_after_the_packages_and_before_anything_else_is_set_up():
    body = function_body("main")
    assert body.index("install_packages") < body.index("choose_time_zone") < body.index("create_user")
    # the service is restarted at the end of every run, so it reads a changed zone
    assert "systemctl restart" in function_body("start_service")


# --- in a real terminal (pty; Linux only, so in the CI) ------------------------------------


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs a pseudo terminal")
@pytest.mark.parametrize(("answer", "selected"), [("\n", False), ("n\n", True)])
def test_in_a_real_terminal_the_whole_script_asks(bash, tmp_path, answer, selected):
    import pty

    os_release(tmp_path)
    env, log = stubs(tmp_path / "stubs")
    master, slave = pty.openpty()
    try:
        os.write(master, ("\n" + answer).encode())  # the language question first (default en), then the zone
        command = [bash, INSTALL_SH.as_posix(), "--dry-run", "--repo", REPO]
        full_env = dict(os.environ, REPRINTBUDDY_DESTDIR=tmp_path.as_posix(), **env)
        full_env.pop("REPRINTBUDDY_REPO", None)
        folder = fake_python(tmp_path.parent / f"{tmp_path.name}-bin", "3.13")
        full_env["PATH"] = f"{folder.as_posix()}{os.pathsep}{full_env['PATH']}"
        result = subprocess.run(command, stdin=slave, capture_output=True, text=True, env=full_env, timeout=60)
    finally:
        os.close(slave)
        os.close(master)
    assert result.returncode == 0, result.stderr
    assert QUESTION.search(result.stdout).group(1) == "Etc/UTC"
    assert ("+ DEBIAN_FRONTEND=dialog dpkg-reconfigure tzdata" in result.stdout) is selected
    assert changes(log) == []  # a dry run
