"""The end-to-end run: install, update, restore, stop the way back from outside, go back (tests/system/README.md).

Runs as root inside a Debian container with systemd (`run.sh` starts it), on the container's own Python, standard
library only. The cases are those of the hand-made test plans of the pre-releases:

 1. install the last stable version S with its own installer (`install-<S>.sh` from `packages.py`);
 2. set up the account and Bambuddy (stand-in), add part A, download a backup;
 3. include pre-releases, check: the state under test P is offered; update to P;
 4. run the installer of P again (the repair run of an existing installation);
 5. restore the backup through the page, then through the command line (on P, part B added before each goes);
 6. go back to S and stop it from outside while pip installs ("install"): P runs again, rolled back;
 7. go back to S and stop it from outside while the service stops ("stop"): P runs again, nothing changed;
 8. go back to S to the end: S runs, part A is there.

After each: version (`release.json`, the program, the page), data, the card of the update and the log. At the end
no request may have reached the stand-ins that they do not expect. Any failure ends the run with exit code 1.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import websteps
from webclient import Browser

INSTALL_DIR = Path("/opt/reprintbuddy")
DATA_DIR = Path("/var/lib/reprintbuddy")
STATUS = DATA_DIR / "update-status.json"
VENV_PYTHON = INSTALL_DIR / ".venv" / "bin" / "python"
URL = "http://127.0.0.1:8080"  # the installer's default address
REPO = "e2e-owner/reprintbuddy"  # not the real repository: nothing of this run goes to GitHub
FINAL_STATES = frozenset({"succeeded", "failed", "rolled_back", "rollback_failed"})
# What a stop from outside may leave (updater._run_return, p610): during pip ("install") the way back is undone;
# while the service stops ("stop") nothing has changed yet. The signal of the second case can arrive a moment
# after the step was written, when the helper is already past "swap": then it is undone like the first.
INTERRUPTED = {
    "install": {("rolled_back", "interrupted_rolled_back")},
    "stop": {("failed", "interrupted_untouched"), ("rolled_back", "interrupted_rolled_back")},
}
ROLLBACK_LIMIT = 90.0  # seconds: systemd's SIGKILL follows a stop after 90 s; the rollback must be done before


class RunFailed(Exception):
    """The run found something else than what a working version shows."""


# --- helpers that need no container (tests/system/test_scenario.py) -------------------------------------------


@dataclass(frozen=True)
class Authority:
    ca: Path  # the certificate of the authority (PEM)
    server_bundle: Path  # key and certificate of github.com and api.github.com (PEM), for fakegithub.py


def make_authority(folder: Path) -> Authority:
    """A certificate authority for this run only, and a certificate of it for both GitHub names.

    Made with openssl, with the extensions Python's strict check (VERIFY_X509_STRICT, default since 3.13) asks for."""
    folder.mkdir(parents=True, exist_ok=True)

    def openssl(*args: str) -> None:
        done = subprocess.run(["openssl", *args], cwd=folder, capture_output=True, text=True, check=False)
        if done.returncode != 0:
            raise RunFailed(f"openssl {args[0]}: {done.stderr.strip()[:500]}")

    (folder / "server.ext").write_text(
        "basicConstraints = critical, CA:FALSE\n"
        "keyUsage = critical, digitalSignature, keyEncipherment\n"
        "extendedKeyUsage = serverAuth\n"
        "subjectAltName = DNS:github.com, DNS:api.github.com\n"
        "subjectKeyIdentifier = hash\n"
        "authorityKeyIdentifier = keyid, issuer\n",
        encoding="ascii",
    )
    openssl(
        "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "2",
        "-subj", "/CN=Reprintbuddy end-to-end test authority",
        "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
    )
    openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "server.key", "-out", "server.csr", "-subj", "/CN=github.com")
    openssl(
        "x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
        "-out", "server.pem", "-days", "2", "-extfile", "server.ext",
    )
    bundle = folder / "server-bundle.pem"
    bundle.write_text(
        (folder / "server.key").read_text(encoding="ascii") + (folder / "server.pem").read_text(encoding="ascii"),
        encoding="ascii",
    )
    return Authority(folder / "ca.pem", bundle)


def append_once(bundle: Path, pem: str) -> bool:
    """Append `pem` to a bundle of certificates unless it is there; True when it was appended."""
    text = bundle.read_text(encoding="utf-8")  # older bundles of certifi had comment lines in UTF-8
    if pem.strip() in text:
        return False
    with bundle.open("a", encoding="utf-8") as file:
        file.write(("" if text.endswith("\n") else "\n") + pem.strip() + "\n")
    return True


def read_status(path: Path) -> dict[str, object]:
    for _ in range(50):  # written by a rename; a read in between finds the old or the new file
        try:
            found = json.loads(path.read_text(encoding="utf-8"))
            return found if isinstance(found, dict) else {}
        except FileNotFoundError:
            return {}
        except ValueError:
            time.sleep(0.01)
    raise RunFailed(f"{path} cannot be read")


def wait_for_step(
    path: Path, steps: Sequence[str], *, timeout: float, tick: Callable[[], None] | None = None, interval: float = 0.02,
) -> str:
    """Wait until the update helper writes one of `steps` into its status file; return the step."""
    deadline = time.monotonic() + timeout
    while True:
        status = read_status(path)
        if status.get("state") == "running" and status.get("step") in steps:
            return str(status["step"])
        if status.get("state") in FINAL_STATES:
            raise RunFailed(f"the run ended ({status.get('state')}, {status.get('reason')}) before step {'/'.join(steps)}")
        if time.monotonic() > deadline:
            raise RunFailed(f"step {'/'.join(steps)} did not come within {timeout:.0f} s (last: {status.get('step')})")
        if tick is not None:
            tick()
        time.sleep(interval)


def check_interrupted(status: dict[str, object], at: str) -> None:
    pair = (status.get("state"), status.get("reason"))
    if pair not in INTERRUPTED[at]:
        raise RunFailed(f"a stop from outside at {at!r} left {pair}, expected one of {sorted(INTERRUPTED[at])}")


class Texts:
    """The English texts of the installed version: the run checks the page against them, not against copies."""

    def __init__(self, install_dir: Path, tag: str) -> None:
        path = install_dir / "releases" / tag / "src" / "reprintbuddy" / "locales" / "en.json"
        self.texts: dict[str, str] = json.loads(path.read_text(encoding="utf-8"))

    def __getitem__(self, key: str) -> str:
        if key not in self.texts:
            raise RunFailed(f"no text {key} in the installed version")
        return self.texts[key]

    def fixed(self, key: str) -> str:
        """The text up to its first placeholder: what the page shows whatever is filled in."""
        return self[key].split("{")[0].strip()


def answer_on_terminal(argv: Sequence[str], prompt: str, answer: str, *, timeout: float) -> tuple[int, str]:
    """Run `argv` on a pseudo terminal, type `answer` after `prompt`; (exit code, what it wrote, without the answer)."""
    import pty  # Linux only

    pid, fd = pty.fork()
    if pid == 0:  # the child: its controlling terminal is the pseudo terminal, as for getpass
        try:
            os.execvp(argv[0], list(argv))
        finally:
            os._exit(127)
    output = b""
    typed = False
    deadline = time.monotonic() + timeout
    import select

    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.5)
        if ready:
            try:
                chunk = os.read(fd, 4096)
            except OSError:  # the child closed the terminal
                break
            if not chunk:
                break
            output += chunk
        if not typed and prompt.encode() in output:
            os.write(fd, answer.encode() + b"\n")
            typed = True
    else:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
        raise RunFailed(f"{argv[0]} did not end within {timeout:.0f} s: {output.decode(errors='replace')[-500:]}")
    _, code = os.waitpid(pid, 0)
    os.close(fd)
    return os.waitstatus_to_exitcode(code), output.decode(errors="replace")


# --- the run in the container ------------------------------------------------------------------------------------


def say(text: str) -> None:
    print(text, flush=True)


def sh(*args: str, check: bool = True, timeout: float = 1800.0) -> subprocess.CompletedProcess[str]:
    say("$ " + " ".join(args))
    done = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL)
    if done.stdout.strip():
        say(done.stdout.rstrip())
    if done.stderr.strip():
        say(done.stderr.rstrip())
    if check and done.returncode != 0:
        raise RunFailed(f"{' '.join(args)} ended with {done.returncode}")
    return done


def as_service_user(*args: str) -> tuple[str, ...]:
    """A command of the installed environment as the service user, not as root: no file of root's lands in it."""
    return ("runuser", "-u", "reprintbuddy", "--", *args)


def healthy(timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(URL + "/healthz", timeout=3):
                return
        except OSError:
            time.sleep(0.5)
    raise RunFailed(f"Reprintbuddy did not answer within {timeout:.0f} s")


def main_pid() -> str:
    return sh("systemctl", "show", "--property=MainPID", "--value", "reprintbuddy.service").stdout.strip()


def unit_active(unit: str) -> bool:
    state = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, check=False).stdout.strip()
    return state in ("active", "activating", "deactivating", "reloading")


class Run:
    def __init__(self, build: Path, logs: Path) -> None:
        self.build = build
        self.logs = logs
        self.work = Path("/root/e2e-work")  # the key of the authority stays out of the uploaded logs
        self.plan = json.loads((build / "plan.json").read_text(encoding="utf-8"))
        self.stable: str = self.plan["stable"]
        self.pre: str = self.plan["prerelease"]
        # made for this run, never stored anywhere else
        self.user = "e2e-admin"
        self.password = secrets.token_urlsafe(18)
        self.backup_password = secrets.token_urlsafe(18)
        self.key = secrets.token_urlsafe(24)
        self.servers: list[subprocess.Popen[bytes]] = []
        self.ca_pem = ""
        self.web = Browser(URL)
        self.backup = b""
        self.number = 0

    @contextmanager
    def step(self, title: str) -> Iterator[None]:
        self.number += 1
        say(f"\n::group::{self.number}. {title}")
        started = time.monotonic()
        try:
            yield
        finally:
            say(f"({time.monotonic() - started:.1f} s)\n::endgroup::")
        say(f"PASSED {self.number}. {title}")

    # -- the stand-ins ----------------------------------------------------------------------------------------

    def start_stand_ins(self) -> None:
        for name in ("github.com", "api.github.com"):
            address = socket.gethostbyname(name)
            if address != "127.0.0.1":
                raise RunFailed(f"{name} resolves to {address}, not to the stand-in (hosts entry missing)")
        authority = make_authority(self.work / "authority")
        self.ca_pem = authority.ca.read_text(encoding="ascii")
        target = Path("/usr/local/share/ca-certificates/reprintbuddy-e2e.crt")
        target.write_text(self.ca_pem, encoding="ascii")
        sh("update-ca-certificates")
        here = Path(__file__).resolve().parent
        (self.work / "bambuddy.key").write_text(self.key, encoding="ascii")
        self.bambuddy_port = self._free_port()
        for argv in (
            [sys.executable, str(here / "fakegithub.py"), "--root", str(self.build), "--repo", REPO,
             "--cert", str(authority.server_bundle), "--log", str(self.logs / "fake-github.log")],
            [sys.executable, str(here / "fakebambuddy.py"), "--key-file", str(self.work / "bambuddy.key"),
             "--log", str(self.logs / "fake-bambuddy.log"), "--port", str(self.bambuddy_port)],
        ):
            self.servers.append(subprocess.Popen(argv, stdout=sys.stdout, stderr=sys.stderr))
        for port in (443, self.bambuddy_port):
            deadline = time.monotonic() + 30
            while True:
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=1).close()
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise RunFailed(f"the stand-in on port {port} did not start") from None
                    time.sleep(0.2)

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def trust_in_the_program(self) -> None:
        """The pages ask GitHub with httpx, which trusts only certifi's list (`trust_env=False`): the authority of
        the run goes there too, in the installed environment only. Looked at before every check: pip could have
        put in a new certifi."""
        found = sh(*as_service_user(str(VENV_PYTHON), "-c", "import certifi; print(certifi.where())"), check=False)
        if found.returncode != 0:  # a later httpx without certifi reads the system's list, which has the authority
            say("no certifi in the installed environment: the system's list of authorities is used")
            return
        where = found.stdout.strip()
        if append_once(Path(where), self.ca_pem):
            say(f"the authority of the run added to {where}")

    def stop_stand_ins(self) -> None:
        for server in self.servers:
            server.terminate()

    def check_stand_ins(self, *, failing: bool = False) -> None:
        """No request the stand-ins did not expect. After a failure of the run they are only shown."""
        github = (self.logs / "fake-github.log").read_text(encoding="utf-8") if (self.logs / "fake-github.log").exists() else ""
        bambuddy = (self.logs / "fake-bambuddy.log").read_text(encoding="utf-8") if (self.logs / "fake-bambuddy.log").exists() else ""
        unknown = [line for line in bambuddy.splitlines() if line.startswith("UNKNOWN")]
        if unknown:
            say("readings the Bambuddy stand-in does not know (404, not a failure):\n" + "\n".join(sorted(set(unknown))))
        unexpected = [line for line in (github + bambuddy).splitlines() if line.startswith("UNEXPECTED")]
        if unexpected:
            text = "requests the stand-ins did not expect:\n" + "\n".join(unexpected)
            if failing:
                say(text)
                return
            raise RunFailed(text)

    # -- what is checked after each step ------------------------------------------------------------------------

    def expect_version(self, tag: str) -> None:
        marker = json.loads((INSTALL_DIR / "release.json").read_text(encoding="utf-8"))
        if marker.get("tag") != tag or marker.get("repo") != REPO:
            raise RunFailed(f"release.json says {marker}, expected {tag} of {REPO}")
        said = sh(*as_service_user(str(INSTALL_DIR / ".venv" / "bin" / "reprintbuddy"), "--version")).stdout.strip()
        if said != f"reprintbuddy {tag[1:]}":
            raise RunFailed(f"the program says {said!r}, expected {tag}")
        healthy()
        page = websteps.visible_text(websteps.maintenance(self.web))
        if tag not in page and tag[1:] not in page:
            raise RunFailed(f"the page does not show the version {tag}")
        say(f"version {tag}: release.json, program and page agree")

    def expect_parts(self, present: Sequence[str], absent: Sequence[str] = ()) -> None:
        for name in present:
            if not websteps.has_part(self.web, name):
                raise RunFailed(f"part {name!r} is missing")
        for name in absent:
            if websteps.has_part(self.web, name):
                raise RunFailed(f"part {name!r} is still there")
        say(f"parts: {list(present)} there, {list(absent)} not")

    def expect_card(self, *texts: str) -> None:
        page = websteps.visible_text(websteps.maintenance(self.web))
        for text in texts:
            if text not in page:
                raise RunFailed(f"the card of the update does not say {text!r}")
        say(f"the card says: {list(texts)}")

    def expect_log(self, *texts: str) -> None:
        log = websteps.log_text(self.web)
        for text in texts:
            if text not in log:
                raise RunFailed(f"the log does not say {text!r}")
        say(f"the log says: {list(texts)}")

    def log_count(self, text: str) -> int:
        return websteps.log_text(self.web).count(text)

    def wait_for_log(self, text: str, *, more_than: int, timeout: float = 150.0) -> None:
        """Until the log says `text` more often than before. The result of an update reaches the log when the
        program next looks at the state file of the update: at its start and then once a minute
        (activity/writer.py UPDATE_EVERY); the new process starts before the helper writes the result."""
        deadline = time.monotonic() + timeout
        while True:
            seen = self.log_count(text)
            if seen > more_than:
                say(f"the log says {text!r} ({seen}x, before {more_than}x)")
                return
            if time.monotonic() > deadline:
                raise RunFailed(f"the log does not say {text!r} once more within {timeout:.0f} s")
            time.sleep(5)

    def wait_for_end(self, timeout: float = 1200.0) -> dict[str, object]:
        """Until the helper has written its result and its unit is over; then until Reprintbuddy answers."""
        deadline = time.monotonic() + timeout
        while True:
            status = read_status(STATUS)
            if status.get("state") in FINAL_STATES and not unit_active("reprintbuddy-update.service"):
                break
            if time.monotonic() > deadline:
                raise RunFailed(f"the update did not end within {timeout:.0f} s: {status}")
            time.sleep(1)
        healthy()
        say(f"status: state={status.get('state')} reason={status.get('reason')} step={status.get('step')} kind={status.get('kind')}")
        return status

    def log_in_again(self) -> None:
        self.web = Browser(URL)
        websteps.log_in(self.web, self.user, self.password)

    # -- the cases ------------------------------------------------------------------------------------------------

    def install_stable(self) -> None:
        with self.step(f"install {self.stable} with its installer"):
            sh("bash", str(self.build / f"install-{self.stable}.sh"), "--language", "en", "--repo", f"https://github.com/{REPO}")
            healthy()
            websteps.set_up(self.web, self.user, self.password, f"http://127.0.0.1:{self.bambuddy_port}", self.key)
            self.expect_version(self.stable)
            websteps.add_part(self.web, "E2E part A")
            self.expect_parts(["E2E part A"])
            self.backup = websteps.download_backup(self.web, login_password=self.password, backup_password=self.backup_password)
            say(f"backup of {self.stable}: {len(self.backup)} bytes")

    def update_to_prerelease(self) -> None:
        with self.step(f"update {self.stable} -> {self.pre} from the page"):
            self.trust_in_the_program()
            websteps.include_prereleases(self.web)
            page = websteps.visible_text(websteps.maintenance(self.web))
            if self.pre not in page and self.pre[1:] not in page:
                raise RunFailed(f"{self.pre} is not offered after the check")
            texts = Texts(INSTALL_DIR, self.stable)
            rework_warning = texts["ops.update.rework_warning"] if "ops.update.rework_warning" in texts.texts else None
            if rework_warning and (rework_warning in page) != bool(self.plan["rework"]):
                raise RunFailed(f"the rework warning is {'missing' if self.plan['rework'] else 'shown'}")
            results_before = self.log_count(texts["activity.kind.update_result"])
            websteps.start_update(self.web)
            status = self.wait_for_end()
            if (status.get("state"), status.get("to_tag")) != ("succeeded", self.pre):
                raise RunFailed(f"the update ended {status.get('state')} ({status.get('reason')}: {status.get('message')})")
            self.log_in_again()
            self.expect_card(Texts(INSTALL_DIR, self.pre)["ops.update.state.succeeded"])
            self.expect_version(self.pre)
            self.expect_parts(["E2E part A"])
            self.wait_for_log(Texts(INSTALL_DIR, self.pre)["activity.kind.update_result"], more_than=results_before)

    def reinstall_prerelease(self) -> None:
        with self.step(f"run the installer of {self.pre} again (repair run)"):
            done = sh("bash", str(self.build / f"install-{self.pre}.sh"), "--repo", f"https://github.com/{REPO}")
            if "Existing installation found" not in done.stdout:
                raise RunFailed("the installer did not keep the existing installation")
            self.expect_version(self.pre)
            self.expect_parts(["E2E part A"])

    def restore_on_the_page(self) -> None:
        with self.step(f"restore the backup of {self.stable} through the page, on {self.pre}"):
            websteps.add_part(self.web, "E2E part B")
            self.expect_parts(["E2E part A", "E2E part B"])
            before = main_pid()
            websteps.restore(self.web, self.backup, backup_password=self.backup_password, login_password=self.password)
            deadline = time.monotonic() + 120
            while main_pid() in (before, "0"):  # the page restarts the service through sudo
                if time.monotonic() > deadline:
                    raise RunFailed("the service did not restart after the restore")
                time.sleep(0.5)
            healthy()
            self.log_in_again()
            self.expect_version(self.pre)
            self.expect_parts(["E2E part A"], ["E2E part B"])
            self.expect_log(Texts(INSTALL_DIR, self.pre)["activity.kind.restore_done"])

    def restore_on_the_command_line(self) -> None:
        with self.step(f"restore the backup through the command line, on {self.pre}"):
            websteps.add_part(self.web, "E2E part B")
            self.expect_parts(["E2E part A", "E2E part B"])
            copy = DATA_DIR / "e2e-backup.zip"
            copy.write_bytes(self.backup)
            sh("chown", "reprintbuddy:reprintbuddy", str(copy))
            sh("systemctl", "stop", "reprintbuddy.service")
            code, output = answer_on_terminal(
                ["/usr/local/bin/reprintbuddy", "restore-backup", str(copy)], "Password of the backup:", self.backup_password,
                timeout=300,
            )
            say(output.rstrip())
            copy.unlink()
            if code != 0:
                raise RunFailed(f"reprintbuddy restore-backup ended with {code}")
            sh("systemctl", "start", "reprintbuddy.service")
            healthy()
            self.log_in_again()
            self.expect_version(self.pre)
            self.expect_parts(["E2E part A"], ["E2E part B"])

    def _start_way_back(self) -> int:
        self.trust_in_the_program()
        websteps.dismiss_card(self.web)
        websteps.check_for_updates(self.web)  # the card offers the way back from the result of a check
        page = websteps.check_way_back(self.web)
        case, _ = websteps.confirm_way_back(self.web, page, login_password=self.password)
        say(f"way back to {self.stable}: case {case}")
        return case

    def way_back_stopped(self, at: str) -> None:
        with self.step(f"go back to {self.stable}, stopped from outside at {at!r}"):
            interrupted = Texts(INSTALL_DIR, self.pre)["activity.update.interrupted"]
            interrupted_before = self.log_count(interrupted)  # the first stop's entry must not count for the second
            self._start_way_back()
            hit = wait_for_step(STATUS, ("install",) if at == "install" else ("stop", "database"), timeout=600)
            say(f"step {hit!r} reached: systemctl stop reprintbuddy-update")
            stopped = time.monotonic()
            sh("systemctl", "stop", "reprintbuddy-update.service", timeout=300)
            status = self.wait_for_end(timeout=300)
            took = time.monotonic() - stopped
            say(f"from the stop to an answering {self.pre}: {took:.1f} s")
            if took > ROLLBACK_LIMIT:
                raise RunFailed(f"the rollback took {took:.1f} s, more than the {ROLLBACK_LIMIT:.0f} s before SIGKILL")
            check_interrupted(status, at)
            if status.get("kind") != "return":
                raise RunFailed(f"the status is of a {status.get('kind')}, not of the way back")
            self.log_in_again()
            self.expect_version(self.pre)
            self.expect_parts(["E2E part A"])
            texts = Texts(INSTALL_DIR, self.pre)
            self.expect_card(texts[f"ops.update.reason.{status['reason']}"])
            self.wait_for_log(interrupted, more_than=interrupted_before)

    def way_back_to_the_end(self) -> None:
        with self.step(f"go back to {self.stable} to the end"):
            case = self._start_way_back()
            status = self.wait_for_end()
            if (status.get("state"), status.get("kind"), status.get("to_tag")) != ("succeeded", "return", self.stable):
                raise RunFailed(f"the way back ended {status.get('state')} ({status.get('reason')}: {status.get('message')})")
            self.log_in_again()
            self.expect_card(Texts(INSTALL_DIR, self.stable).fixed("ops.return.state.succeeded"))
            self.expect_version(self.stable)
            self.expect_parts(["E2E part A"])  # case 3 restores the backup from before the update: it holds A too
            say(f"way back case {case} done")

    def run(self) -> None:
        with self.step("start the stand-ins for GitHub and Bambuddy"):
            self.start_stand_ins()
        try:
            self.install_stable()
            self.update_to_prerelease()
            self.reinstall_prerelease()
            self.restore_on_the_page()
            self.restore_on_the_command_line()
            self.way_back_stopped("install")
            self.way_back_stopped("stop")
            self.way_back_to_the_end()
            with self.step("no request the stand-ins did not expect"):
                self.check_stand_ins()
        except BaseException:
            self.check_stand_ins(failing=True)
            raise
        finally:
            self.stop_stand_ins()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--build", type=Path, required=True, help="the folder packages.py wrote")
    parser.add_argument("--logs", type=Path, required=True, help="where logs go (uploaded when the run fails)")
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        print("run this as root inside the container (tests/system/run.sh)", file=sys.stderr)
        return 2
    args.logs.mkdir(parents=True, exist_ok=True)
    run = Run(args.build, args.logs)
    try:
        run.run()
    except (RunFailed, websteps.StepFailed) as error:
        say(f"::error::end-to-end run failed in step {run.number}: {error}")
        return 1
    except Exception as error:  # a form not found, a connection lost: named with its step, then the traceback
        say(f"::error::end-to-end run failed in step {run.number}: {type(error).__name__}: {error}")
        raise
    say("\nend-to-end run passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
