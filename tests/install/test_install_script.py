"""The install script is only exercised with `bash -n`, `shellcheck` and `--dry-run`.

`REPRINTBUDDY_DESTDIR` (allowed only together with `--dry-run`) makes the script look
for existing files under a test folder instead of the real root.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import reprintbuddy

ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = ROOT / "install" / "install.sh"
CLI_SH = ROOT / "install" / "reprintbuddy-cli.sh"
REPO = "https://github.com/owner/reprintbuddy.git"  # v0.4.9: a GitHub address (the package comes from its releases)
REPO_BASE = "https://github.com/owner/reprintbuddy"
RUNUSER = "+ setsid --wait runuser -u reprintbuddy -- env HOME=/var/lib/reprintbuddy"
PINNED = "v" + reprintbuddy.__version__  # the release the script belongs to and installs (v0.3.14 p168)
PACKAGE = f"reprintbuddy-{PINNED}.tar.gz"
WORK = "/opt/reprintbuddy/releases/.download"
SOURCE = f"/opt/reprintbuddy/releases/{PINNED}"


@pytest.fixture
def bash():
    found = shutil.which("bash")
    if not found:
        pytest.skip("bash is not installed")
    return found


def os_release(dest: Path, id_: str = "debian", version: str = "13") -> None:
    (dest / "etc").mkdir(parents=True, exist_ok=True)
    (dest / "etc" / "os-release").write_text(f'ID={id_}\nVERSION_ID="{version}"\n', encoding="utf-8")


def fake_python(folder: Path, version: str) -> Path:
    """A `python3` that only knows the two questions the script asks: is it 3.13 or newer (the exit
    status), and which version is it (the printed text)."""
    folder.mkdir(parents=True, exist_ok=True)
    major, minor = (int(part) for part in version.split("."))
    script = folder / "python3"
    script.write_text(
        "#!/bin/sh" + "\n"
        + 'case "$2" in' + "\n"
        + f'  *print*) echo "{version}" ;;' + "\n"
        + f"  *) [ {major * 100 + minor} -ge 313 ] ;;" + "\n"
        + "esac" + "\n",
        encoding="utf-8",
        newline="\n",
    )
    script.chmod(0o755)
    return folder


def run(bash, dest, *args, dry=True, env_extra=None, destdir=True, python="3.13"):
    """The script in a dry run; `python3` on the path is a fake of that version (the machine's own
    one would decide the outcome otherwise)."""
    env = dict(os.environ)
    env.pop("REPRINTBUDDY_REPO", None)
    env.pop("REPRINTBUDDY_DESTDIR", None)
    if destdir:
        env["REPRINTBUDDY_DESTDIR"] = dest.as_posix()
    if python:
        folder = fake_python(dest.parent / f"{dest.name}-bin", python)
        env["PATH"] = f"{folder.as_posix()}{os.pathsep}{env['PATH']}"
    env.update(env_extra or {})
    command = [bash, INSTALL_SH.as_posix(), *(["--dry-run"] if dry else []), *args]
    return subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", env=env, stdin=subprocess.DEVNULL, timeout=60
    )


def lines(result):
    return result.stdout.replace("\r\n", "\n")


def test_bash_syntax(bash):
    for script in (INSTALL_SH, CLI_SH):
        done = subprocess.run([bash, "-n", script.as_posix()], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr


def test_shellcheck(bash):
    tool = shutil.which("shellcheck")
    if not tool:
        pytest.skip("shellcheck is not installed")
    done = subprocess.run(
        [tool, "-s", "bash", INSTALL_SH.as_posix(), CLI_SH.as_posix()], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_script_ends_with_main_call():
    last = [line for line in INSTALL_SH.read_text(encoding="utf-8").splitlines() if line.strip()][-1]
    assert last == 'main "$@"'


def test_dry_run_fresh(bash, tmp_path):
    os_release(tmp_path)
    result = run(bash, tmp_path, "--language", "de", "--repo", REPO)
    assert result.returncode == 0, result.stderr
    out = lines(result)
    expected = [
        "+ apt-get update",
        "apt-get install -y --no-install-recommends python3 python3-venv curl sudo ca-certificates",
        "useradd --system",
        "chmod 700 /var/lib/reprintbuddy",
        # v0.4.9 p500: the package of the release of this script, checked against its checksum
        f"mkdir -p {WORK}",
        f"curl -fsSL --proto =https --proto-redir =https --max-filesize 50000000 -o {WORK}/{PACKAGE} "
        f"{REPO_BASE}/releases/download/{PINNED}/{PACKAGE}",
        f"-o {WORK}/{PACKAGE}.sha256 {REPO_BASE}/releases/download/{PINNED}/{PACKAGE}.sha256",
        f"sha256sum -c --strict --quiet {PACKAGE}.sha256",
        f"tar -xzf {WORK}/{PACKAGE} -C {WORK}",
        f"mv {WORK}/reprintbuddy-{PINNED} {SOURCE}",
        "release.json",
        f"rm -rf {WORK}",
        "python3 -m venv /opt/reprintbuddy/.venv",
        f"rm -rf {SOURCE}/build",
        f"-m pip install --no-cache-dir --upgrade {SOURCE}",
        "reprintbuddy init --db /var/lib/reprintbuddy/reprintbuddy.db --language de",
        "visudo -cf ",
        "reprintbuddy.sudoers",
        "/etc/sudoers.d/reprintbuddy",
        "/etc/systemd/system/reprintbuddy.service",
        "/etc/systemd/system/reprintbuddy-update.service",
        "/usr/local/bin/reprintbuddy",
        "systemctl daemon-reload",
        "systemctl enable reprintbuddy.service",
        "systemctl restart reprintbuddy.service",
        "Finish the setup now: http://<server-ip>:8080/setup",
    ]
    position = 0
    for part in expected:
        found = out.find(part, position)
        assert found >= 0, f"{part!r} missing or out of order in:\n{out}"
        position = found + len(part)
    assert "anyone in your network" in out
    assert not [line for line in out.splitlines() if " git " in line or "git clone" in line]  # v0.4.9: no git at all
    for line in out.splitlines():
        if any(word in line for word in ("curl -fsSL", "sha256sum", "tar -x", "-m venv", "-m pip", "reprintbuddy init", "/releases/")):
            assert line.startswith(RUNUSER), line
        assert not line.startswith(("+ git", "+ python3 ", "+ /opt/reprintbuddy/.venv", "+ curl", "+ tar")), line


def test_dry_run_changes_nothing(bash, tmp_path):
    os_release(tmp_path)
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    assert run(bash, tmp_path, "--repo", REPO).returncode == 0
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == before


def test_debian12_is_refused_and_says_why(bash, tmp_path):
    """v0.3.9 p101: Debian 13 is the floor, since Debian 12 only has Python 3.11 (main), no backports; v0.3.12 p163: the
    message names Python 3.13, the one Debian 13 brings along."""
    os_release(tmp_path, version="12")
    result = run(bash, tmp_path, "--repo", REPO)
    assert result.returncode == 1
    message = result.stderr + result.stdout
    assert "Debian 13" in message and "Python 3.13" in message and "Debian 12" in message


def test_a_newer_debian_is_accepted(bash, tmp_path):
    os_release(tmp_path, version="14")
    assert run(bash, tmp_path, "--repo", REPO).returncode == 0


def rolling_release(dest: Path, codename: str) -> None:
    """The os-release of Debian testing or unstable: no VERSION_ID, only a codename."""
    (dest / "etc").mkdir(parents=True, exist_ok=True)
    (dest / "etc" / "os-release").write_text(
        f'PRETTY_NAME="Debian GNU/Linux {codename}/sid"\nNAME="Debian GNU/Linux"\n'
        f"VERSION_CODENAME={codename}\nID=debian\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("codename", ["forky", "sid"])
def test_debian_testing_or_unstable_is_accepted_with_a_new_enough_python(bash, tmp_path, codename):
    """v0.3.11 p130: no VERSION_ID is no reason to refuse; the Python check decides."""
    rolling_release(tmp_path, codename)
    result = run(bash, tmp_path, "--repo", REPO, python="3.13")
    assert result.returncode == 0, result.stderr


def test_debian_testing_with_a_too_old_python_is_refused_by_the_python_check(bash, tmp_path):
    rolling_release(tmp_path, "sid")
    result = run(bash, tmp_path, "--repo", REPO, python="3.11")
    assert result.returncode == 1
    message = result.stderr + result.stdout
    # The Python check's own words (not the Debian check's, which names Python 3.11 too):
    assert "python3 here is Python 3.11" in message


def test_a_debian_version_that_is_no_number_is_refused(bash, tmp_path):
    os_release(tmp_path, version="trixie")
    assert run(bash, tmp_path, "--repo", REPO).returncode == 1


def test_a_release_without_version_that_is_not_debian_is_refused(bash, tmp_path):
    (tmp_path / "etc").mkdir(parents=True, exist_ok=True)
    (tmp_path / "etc" / "os-release").write_text("ID=arch\n", encoding="utf-8")
    result = run(bash, tmp_path, "--repo", REPO)
    assert result.returncode == 1
    assert "needs Debian 13 or newer" in result.stderr + result.stdout


@pytest.mark.parametrize("python", ["3.11", "3.12"])
def test_python_older_than_3_13_stops_the_script_with_the_version_named(bash, tmp_path, python):
    """Whatever the system says it is: the Python that would build the venv decides. v0.3.12 p163: 3.12 is
    refused too (no Debian release the script accepts has it)."""
    os_release(tmp_path)
    result = run(bash, tmp_path, "--repo", REPO, python=python)
    assert result.returncode == 1
    message = result.stderr + result.stdout
    assert "Python 3.13 or newer" in message and f"python3 here is Python {python}" in message


def test_the_python_check_comes_before_anything_is_created(bash, tmp_path):
    os_release(tmp_path)
    out = lines(run(bash, tmp_path, "--repo", REPO, python="3.11"))
    assert "useradd" not in out and "git clone" not in out and "-m venv" not in out


def test_refuses_non_debian(bash, tmp_path):
    os_release(tmp_path, id_="ubuntu", version="24.04")
    result = run(bash, tmp_path, "--repo", REPO)
    assert result.returncode == 1
    assert "Reprintbuddy needs Debian 13 or newer" in result.stderr + result.stdout


def test_refuses_old_debian(bash, tmp_path):
    os_release(tmp_path, version="11")
    assert run(bash, tmp_path, "--repo", REPO).returncode == 1


def test_refuses_missing_os_release(bash, tmp_path):
    result = run(bash, tmp_path, "--repo", REPO)
    assert result.returncode == 1
    assert "needs Debian 13 or newer" in result.stderr + result.stdout


def test_default_language_en(bash, tmp_path):
    os_release(tmp_path)
    result = run(bash, tmp_path, "--repo", REPO)
    assert result.returncode == 0
    init_line = [line for line in lines(result).splitlines() if "reprintbuddy init" in line][0]
    assert "--language en" in init_line


def test_invalid_language(bash, tmp_path):
    os_release(tmp_path)
    result = run(bash, tmp_path, "--language", "fr", "--repo", REPO)
    assert result.returncode == 2
    assert "Unknown language: fr (use en or de)." in result.stderr + result.stdout


def test_placeholder_repo_refused(bash, tmp_path):
    os_release(tmp_path)
    result = run(bash, tmp_path, "--repo", "https://github.com/<owner>/reprintbuddy.git")
    assert result.returncode == 1
    assert "Repository URL not set" in result.stderr + result.stdout


def test_default_repo_is_the_published_one(bash, tmp_path):
    """Published: without --repo the script loads the package from the public repository's releases."""
    os_release(tmp_path)
    result = run(bash, tmp_path)
    assert result.returncode == 0, result.stderr
    assert f"https://github.com/Eifel-Joe/reprintbuddy/releases/download/{PINNED}/{PACKAGE}" in lines(result)


@pytest.mark.parametrize("given", [REPO, REPO_BASE, REPO_BASE + "/"])
def test_repo_from_environment(bash, tmp_path, given):
    os_release(tmp_path)
    result = run(bash, tmp_path, env_extra={"REPRINTBUDDY_REPO": given})
    assert result.returncode == 0
    assert f"{REPO_BASE}/releases/download/{PINNED}/{PACKAGE}" in lines(result)


@pytest.mark.parametrize(
    "given",
    ["git@github.com:owner/reprintbuddy.git", "http://github.com/owner/reprintbuddy", "https://gitlab.com/owner/reprintbuddy",
     "https://github.com/owner", "https://github.com/owner/repo/extra", "https://github.com/../x"],
)
def test_a_repository_that_is_no_github_address_is_refused(bash, tmp_path, given):
    """v0.4.9: the package comes from the GitHub releases of the repository."""
    os_release(tmp_path)
    result = run(bash, tmp_path, "--repo", given)
    assert result.returncode == 1
    assert "Repository URL must be https://github.com/<owner>/<name>" in result.stderr + result.stdout
    assert "apt-get" not in lines(result)


def test_destdir_requires_dry_run(bash, tmp_path):
    os_release(tmp_path)
    result = run(bash, tmp_path, "--repo", REPO, dry=False)
    assert result.returncode == 2
    assert "REPRINTBUDDY_DESTDIR is only allowed with --dry-run." in result.stderr + result.stdout


def test_unknown_option(bash, tmp_path):
    os_release(tmp_path)
    assert run(bash, tmp_path, "--bogus").returncode == 2


def test_help(bash, tmp_path):
    result = run(bash, tmp_path, "--help", dry=False, destdir=False)
    assert result.returncode == 0
    assert "--language" in lines(result) and "--dry-run" in lines(result)


def fake_venv(dest: Path, version: str, *, python: bool = True, pip: bool = True) -> None:
    """The installed environment: `pyvenv.cfg` of that version (the Python it was built with), `bin/python` and
    `bin/pip` as asked. The script reads the version from the file and never runs the environment's python as root."""
    venv = dest / "opt/reprintbuddy/.venv"
    venv.mkdir(parents=True, exist_ok=True)
    (venv / "pyvenv.cfg").write_text(f"home = /usr/bin\nversion = {version}.2\n", encoding="utf-8")
    (venv / "bin").mkdir(exist_ok=True)
    for name, wanted in (("python", python), ("pip", pip)):
        if wanted:
            (venv / "bin" / name).write_text("#!/bin/sh\n", encoding="utf-8")
            (venv / "bin" / name).chmod(0o755)


def installed(dest: Path, venv: str = "3.13", **venv_options) -> None:
    """A finished installation: code, environment, a database with content."""
    os_release(dest)
    for folder in ("opt/reprintbuddy/.git", "var/lib/reprintbuddy"):
        (dest / folder).mkdir(parents=True, exist_ok=True)
    fake_venv(dest, venv, **venv_options)
    (dest / "var/lib/reprintbuddy/reprintbuddy.db").write_bytes(b"x")


def test_rerun_keeps_database(bash, tmp_path):
    installed(tmp_path)
    result = run(bash, tmp_path, "--repo", REPO)
    assert result.returncode == 0, result.stderr
    out = lines(result)
    assert "Existing installation found, keeping version." in out
    assert "Existing database kept." in out
    assert "systemctl restart reprintbuddy.service" in out
    for forbidden in ("git clone", "checkout", "init --db", "-m venv", "rm -rf /opt/reprintbuddy/.venv", "curl -fsSL"):
        assert forbidden not in out, forbidden
    assert "-m pip install --no-cache-dir --upgrade /opt/reprintbuddy\n" in out  # a git installation: its own tree


def installed_package(dest: Path, tag: str = "v0.4.9", venv: str = "3.13") -> None:
    """A finished installation from a package (v0.4.9 on): release.json, the unpacked version, a database."""
    os_release(dest)
    tree = dest / "opt/reprintbuddy"
    (tree / "releases" / tag).mkdir(parents=True, exist_ok=True)
    (tree / "release.json").write_text(
        '{"tag": "%s", "repo": "owner/reprintbuddy", "sha256": "%s"}\n' % (tag, "0" * 64), encoding="utf-8"
    )
    (dest / "var/lib/reprintbuddy").mkdir(parents=True, exist_ok=True)
    fake_venv(dest, venv)
    (dest / "var/lib/reprintbuddy/reprintbuddy.db").write_bytes(b"x")


def test_rerun_of_a_package_installation_keeps_its_version(bash, tmp_path):
    installed_package(tmp_path)
    result = run(bash, tmp_path, "--repo", REPO)
    assert result.returncode == 0, result.stderr
    out = lines(result)
    assert f"Existing installation found, keeping version v0.4.9 (this script is {PINNED};" in out
    assert "Existing database kept." in out
    assert "-m pip install --no-cache-dir --upgrade /opt/reprintbuddy/releases/v0.4.9" in out
    for forbidden in ("curl -fsSL", "init --db", "-m venv", " git "):
        assert forbidden not in out, forbidden


@pytest.mark.parametrize("marker", ['{"tag": "../../etc"}', "not json", '{"tag": "v1.0.0; rm -rf /"}'])
def test_rerun_with_a_marker_that_names_no_version_is_refused(bash, tmp_path, marker):
    installed_package(tmp_path)
    (tmp_path / "opt/reprintbuddy/release.json").write_text(marker, encoding="utf-8")
    result = run(bash, tmp_path, "--repo", REPO)
    assert result.returncode == 1
    assert "does not name a version" in result.stderr + result.stdout
    assert "pip install" not in lines(result)

REBUILT = {
    # (system python, python the environment was built with, options): why the environment is of no use
    "built-with-3.11": ("3.13", "3.11", {}),  # a Debian release upgrade: its packages sit under lib/python3.11
    "built-with-3.12": ("3.13", "3.12", {}),  # an environment of the old floor (v0.3.9 to v0.3.11)
    "built-with-an-older-python-than-the-system": ("3.14", "3.13", {}),  # the next release upgrade
    "no-python-in-it": ("3.13", "3.13", {"python": False}),
    "no-pip-in-it": ("3.13", "3.13", {"pip": False}),  # `python3 -m venv` died at ensurepip once
}


@pytest.mark.parametrize(("system", "venv", "options"), REBUILT.values(), ids=REBUILT.keys())
def test_rerun_rebuilds_an_environment_that_does_not_fit_the_system_python(bash, tmp_path, system, venv, options):
    """v0.3.9 p101: after a release upgrade of Debian the packages of the environment sit under the old
    lib/python3.x and its python is another one (the service does not start). The installer builds it
    again and keeps everything else."""
    installed(tmp_path, venv, **options)
    result = run(bash, tmp_path, "--repo", REPO, python=system)
    assert result.returncode == 0, result.stderr
    out = lines(result)
    steps = [
        "systemctl stop reprintbuddy.service",
        "rm -rf /opt/reprintbuddy/.venv /opt/reprintbuddy/.venv.rollback",
        "python3 -m venv /opt/reprintbuddy/.venv",
        "-m pip install --no-cache-dir --upgrade /opt/reprintbuddy",
        "systemctl restart reprintbuddy.service",
    ]
    position = 0
    for step in steps:
        found = out.find(step, position)
        assert found >= 0, f"{step!r} missing or out of order in: " + out
        position = found + len(step)
    assert "does not fit the system" in out and "rebuilding" in out
    # what is kept: the version, the database, the data folder
    assert "Existing installation found, keeping version." in out
    assert "Existing database kept." in out
    for forbidden in ("git clone", "checkout", "init --db"):
        assert forbidden not in out, forbidden
    for line in out.splitlines():
        if line.startswith("+ rm "):
            assert "/var/lib/reprintbuddy" not in line and "reprintbuddy.db" not in line, line
            assert line.startswith("+ rm -rf /opt/reprintbuddy/"), line


@pytest.mark.parametrize("version", ["3.13", "3.14"])
def test_an_environment_built_with_the_system_python_is_not_rebuilt(bash, tmp_path, version):
    installed(tmp_path, version)
    out = lines(run(bash, tmp_path, "--repo", REPO, python=version))
    assert "-m venv" not in out and "rm -rf" not in out.replace("rm -rf /opt/reprintbuddy/build", "")


def silent_python(folder: Path) -> Path:
    """A `python3` that is 3.13 or newer as far as the floor check goes, but cannot say which version
    (the question for the version fails): the installer then knows nothing of the system's Python."""
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / "python3"
    body = '#!/bin/sh\ncase "$2" in\n  *print*) exit 1 ;;\n  *) exit 0 ;;\nesac\n'
    script.write_text(body, encoding="utf-8", newline="\n")
    script.chmod(0o755)
    return folder


@pytest.mark.parametrize(("venv", "rebuilt"), [("3.12", True), ("3.13", False)])
def test_the_environment_floor_is_3_13_even_when_the_system_python_is_unknown(bash, tmp_path, venv, rebuilt):
    """v0.3.12 p163: `venv_usable` has a floor of its own for when the system's version cannot be read; it is the
    same 3.13 as the one of `require_python`."""
    installed(tmp_path, venv)
    folder = silent_python(tmp_path.parent / f"{tmp_path.name}-silent")
    env = {"PATH": f"{folder.as_posix()}{os.pathsep}{os.environ['PATH']}"}
    out = lines(run(bash, tmp_path, "--repo", REPO, python=None, env_extra=env))
    assert ("-m venv" in out) is rebuilt, out


def test_a_folder_with_release_json_is_an_installation(bash, tmp_path):
    os_release(tmp_path)
    (tmp_path / "opt/reprintbuddy").mkdir(parents=True)
    (tmp_path / "opt/reprintbuddy/release.json").write_text('{"tag": "v0.4.9"}', encoding="utf-8")
    result = run(bash, tmp_path, "--repo", REPO)
    assert result.returncode == 0, result.stderr


def test_refuses_foreign_install_dir(bash, tmp_path):
    os_release(tmp_path)
    (tmp_path / "opt/reprintbuddy").mkdir(parents=True)
    (tmp_path / "opt/reprintbuddy/somefile").write_text("x")
    result = run(bash, tmp_path, "--repo", REPO)
    assert result.returncode == 1
    assert "/opt/reprintbuddy exists but is not a Reprintbuddy installation." in result.stderr + result.stdout


def test_not_root_refused(bash, tmp_path):
    check = subprocess.run([bash, "-c", "echo $EUID"], capture_output=True, text=True)
    if check.stdout.strip() == "0":
        pytest.skip("running as root")
    env = {k: v for k, v in os.environ.items() if k != "REPRINTBUDDY_DESTDIR"}
    result = subprocess.run(
        [bash, INSTALL_SH.as_posix(), "--repo", REPO],
        capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=60,
    )
    assert result.returncode == 1
    assert "Run this script as root." in result.stderr + result.stdout
    assert not [line for line in result.stdout.splitlines() if line.startswith("+")]


def test_messages_are_english_only_and_neutral():
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert 'REPO_URL_DEFAULT="https://github.com/Eifel-Joe/reprintbuddy"' in text


# --- root must not install anything from a place the service user can write to ---------


def embedded(name: str) -> str:
    """The text of a file embedded in install.sh as a quoted here-document."""
    import re

    text = INSTALL_SH.read_text(encoding="utf-8")
    match = re.search(rf"<<'RB_{name}_EOF'\n(.*?)\nRB_{name}_EOF\n", text, re.S)
    assert match, f"{name} is not embedded in install.sh"
    return match.group(1) + "\n"


@pytest.mark.parametrize(
    "name,filename",
    [
        ("SUDOERS", "reprintbuddy.sudoers"),
        ("SERVICE", "reprintbuddy.service"),
        ("UPDATE_SERVICE", "reprintbuddy-update.service"),
        ("CLI", "reprintbuddy-cli.sh"),
    ],
)
def test_embedded_system_files_equal_the_install_directory(name, filename):
    assert embedded(name) == (ROOT / "install" / filename).read_text(encoding="utf-8")


def test_system_files_are_not_installed_from_the_install_tree(bash, tmp_path):
    os_release(tmp_path)
    out = lines(run(bash, tmp_path, "--repo", REPO))
    assert "/opt/reprintbuddy/install/" not in out
    # The installed tree is only compared with (cmp), never a source for `install`.
    script = INSTALL_SH.read_text(encoding="utf-8")
    for line in script.splitlines():
        if line.lstrip().startswith("run install "):
            assert "INSTALL_DIR" not in line, line


def test_sudoers_is_checked_before_it_is_installed(bash, tmp_path):
    os_release(tmp_path)
    out = lines(run(bash, tmp_path, "--repo", REPO))
    assert out.index("visudo -cf ") < out.index("/etc/sudoers.d/reprintbuddy")


def test_rerun_after_an_aborted_first_install_puts_the_release_in_place(bash, tmp_path):
    """A first run (of an older script, by git) died after the clone: no database. Nothing is installed, so
    the git copy goes and the package of this script's release is put in place (v0.4.9)."""
    os_release(tmp_path)
    (tmp_path / "opt/reprintbuddy/.git").mkdir(parents=True)
    out = lines(run(bash, tmp_path, "--repo", REPO))
    assert "Unfinished installation found" in out
    # (a dry run prints the command quoted for the shell: `\!` is `!`)
    assert r"find /opt/reprintbuddy -mindepth 1 -maxdepth 1 \! -name .venv \! -name releases \! -name release.json -exec rm -rf" in out
    assert f"{REPO_BASE}/releases/download/{PINNED}/{PACKAGE}" in out
    assert "git clone" not in out and "Existing installation found" not in out
    for line in out.splitlines():
        if "-maxdepth 1" in line:
            assert line.startswith(RUNUSER), line


VERSION_SAMPLES = (
    "v0.4.9", "v1.0.0", "v10.20.30", "v1.0.1-beta.1", "v1.0.1-beta.12",
    "v01.0.0", "v1.0", "1.0.0", "v1.0.0-beta.0", "v1.0.0-beta.01", "v1.0.0-beta", "v1.0.0-rc.1", "--force", "v1.0.0 ",
)


def test_script_and_python_use_the_same_version_rule():
    """v0.4.9: the script's pattern (grep -E) accepts exactly the tags Python's `parse_version` does."""
    from reprintbuddy.ops.versions import parse_version

    found = re.findall(r"VERSION_PATTERN='([^']*)'", INSTALL_SH.read_text(encoding="utf-8"))
    assert len(found) == 1
    pattern = re.compile(found[0])
    for tag in VERSION_SAMPLES:
        assert bool(pattern.fullmatch(tag)) is (parse_version(tag) is not None), tag


# --- v0.3.14 p168: the script installs the release it belongs to (the tag in its address) --------------


def test_the_release_in_the_script_is_the_version_of_this_code():
    """The script is fetched from a tag and installs that tag. It cannot see its own address, so the tag is
    written into it and set together with the package version (this test fails until it is)."""
    found = re.findall(r'^RELEASE_TAG="([^"]*)"$', INSTALL_SH.read_text(encoding="utf-8"), re.MULTILINE)
    assert found == [PINNED]


def test_dry_run_loads_the_release_of_the_script_not_the_newest(bash, tmp_path):
    os_release(tmp_path)
    out = lines(run(bash, tmp_path, "--repo", REPO))
    assert f"/releases/download/{PINNED}/{PACKAGE}" in out
    assert "/releases/latest/" not in out and "vX.Y.Z" not in out




SHIM = """#!/bin/bash
# Stands in for `setsid --wait runuser -u reprintbuddy -- env HOME=... git -C /opt/reprintbuddy ...`:
# drops the five words in front and runs the rest on the test repository.
shift 5
args=()
for a in "$@"; do args+=("${a//\\/opt\\/reprintbuddy/$FAKE_INSTALL}"); done
exec "${args[@]}"
"""










# --- review of p168: the re-run paths of fetch_code on a real clone -------------------------------------


def _run_fetch_code(bash, tmp_path, origin, *, checked_out, database):
    """`fetch_code` of the script on a clone of `origin` that sits on `checked_out`, with or without the
    database. REPRINTBUDDY_DESTDIR points the file checks at the test folder; the same stand-in for
    `setsid ... runuser` as above runs git on the clone."""
    git = shutil.which("git")
    install = tmp_path / "root" / "opt" / "reprintbuddy"
    install.parent.mkdir(parents=True)
    subprocess.run([git, "clone", "-q", origin.as_posix(), install.as_posix()], check=True, capture_output=True)
    subprocess.run([git, "-C", install.as_posix(), "checkout", "-q", "--detach", checked_out], check=True, capture_output=True)
    if database:
        db = tmp_path / "root" / "var" / "lib" / "reprintbuddy" / "reprintbuddy.db"
        db.parent.mkdir(parents=True)
        db.write_bytes(b"")
    text = INSTALL_SH.read_text(encoding="utf-8").replace("\r\n", "\n").rstrip()
    assert text.endswith('main "$@"')
    functions = tmp_path / "functions.sh"
    functions.write_text(text.removesuffix('main "$@"'), encoding="utf-8", newline="\n")
    shims = tmp_path / "shims"
    shims.mkdir()
    (shims / "setsid").write_text(SHIM, encoding="utf-8", newline="\n")
    (shims / "setsid").chmod(0o755)
    env = dict(
        os.environ, FAKE_INSTALL=install.as_posix(), REPRINTBUDDY_DESTDIR=(tmp_path / "root").as_posix(),
        PATH=f"{shims.as_posix()}{os.pathsep}{os.environ['PATH']}",
    )
    command = f'source "{functions.as_posix()}"; fetch_code; git -C "{install.as_posix()}" describe --tags --exact-match'
    return subprocess.run(
        [bash, "-c", command], capture_output=True, text=True, encoding="utf-8", env=env,
        stdin=subprocess.DEVNULL, timeout=120,
    )


@pytest.fixture
def origin_repo(tmp_path):
    """The repository the installer clones from: an older release, the one of the script and a newer one;
    the default branch stands on the newest (as main does after a release)."""
    git = shutil.which("git")
    if not git:
        pytest.skip("git is not installed")
    repo = tmp_path / "origin"
    repo.mkdir()
    subprocess.run([git, "init", "-q", repo.as_posix()], check=True, capture_output=True)
    for tag in ("v0.0.1", PINNED, "v99.0.0"):
        (repo / "version.txt").write_text(tag, encoding="utf-8")
        for args in (["add", "version.txt"], ["commit", "-q", "-m", tag], ["tag", tag]):
            subprocess.run(
                [git, "-C", repo.as_posix(), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                check=True, capture_output=True,
            )
    return repo




def test_a_rerun_with_database_keeps_the_installed_version_and_says_which(bash, tmp_path, origin_repo):
    done = _run_fetch_code(bash, tmp_path, origin_repo, checked_out="v0.0.1", database=True)
    assert done.returncode == 0, done.stderr
    assert "Existing installation found, keeping version v0.0.1" in done.stdout
    assert PINNED in done.stdout  # it says which release this script is, and where versions change
    assert done.stdout.strip().splitlines()[-1] == "v0.0.1"


def test_a_rerun_with_database_on_the_release_of_the_script_says_nothing_more(bash, tmp_path, origin_repo):
    done = _run_fetch_code(bash, tmp_path, origin_repo, checked_out=PINNED, database=True)
    assert done.returncode == 0, done.stderr
    assert "Existing installation found, keeping version." in done.stdout


# --- v0.4.9 p500: the package of the release, downloaded and checked for real ---------------------------


CURL_STUB = """#!/bin/bash
# Stands in for curl: hands over the file of the test folder named like the last part of the address.
out=""
while [ "$#" -gt 1 ]; do
    if [ "$1" = "-o" ]; then out="$2"; shift 2; continue; fi
    shift
done
name="${1##*/}"
echo "$1" >> "$FIXTURES/asked.txt"
[ -f "$FIXTURES/$name" ] || exit 22
cp "$FIXTURES/$name" "$out"
"""


def msys(path: Path) -> str:
    """A path as bash on Windows (MSYS) and on Linux both read it without a drive colon (GNU tar
    would take `D:` for a remote host)."""
    text = path.as_posix()
    if len(text) > 1 and text[1] == ":":
        return "/" + text[0].lower() + text[2:]
    return text


def package_fixture(folder: Path, tag: str = PINNED, *, extra: str | None = None, checksum: str | None = None) -> Path:
    """The package of `tag` as the release workflow builds it, and its checksum file."""
    import hashlib
    import io
    import tarfile

    folder.mkdir(parents=True, exist_ok=True)
    name = f"reprintbuddy-{tag}.tar.gz"
    top = f"reprintbuddy-{tag}"
    with tarfile.open(folder / name, "w:gz") as archive:
        entries = [(f"{top}/pyproject.toml", b"[project]\n"), (f"{top}/install/install.sh", b"#!/bin/sh\n")]
        if extra:
            entries.append((extra, b"x"))
        for member, data in entries:
            info = tarfile.TarInfo(member)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    digest = checksum or hashlib.sha256((folder / name).read_bytes()).hexdigest()
    (folder / f"{name}.sha256").write_text(f"{digest}  {name}\n", encoding="utf-8", newline="\n")
    return folder


def _call(bash, tmp_path, command: str, fixtures: Path, *, git_clone_of: Path | None = None, database: bool = False):
    """Functions of the script (without `main`) on a test folder: the stand-in for `setsid ... runuser`
    runs the command on the test folder, `curl` hands over the fixture files, `python3` is this Python."""
    import sys

    root = tmp_path / "root"
    install = root / "opt" / "reprintbuddy"
    install.mkdir(parents=True)
    if git_clone_of is not None:
        subprocess.run([shutil.which("git"), "clone", "-q", git_clone_of.as_posix(), install.as_posix()], check=True, capture_output=True)
    if database:
        db = root / "var" / "lib" / "reprintbuddy" / "reprintbuddy.db"
        db.parent.mkdir(parents=True)
        db.write_bytes(b"")
    text = INSTALL_SH.read_text(encoding="utf-8").replace("\r\n", "\n").rstrip()
    functions = tmp_path / "functions.sh"
    functions.write_text(text.removesuffix('main "$@"'), encoding="utf-8", newline="\n")
    shims = tmp_path / "shims"
    shims.mkdir()
    for name, body in (
        ("setsid", SHIM),
        ("curl", CURL_STUB),
        ("python3", f'#!/bin/bash\nexec "{msys(Path(sys.executable))}" "$@"\n'),
    ):
        (shims / name).write_text(body, encoding="utf-8", newline="\n")
        (shims / name).chmod(0o755)
    env = dict(
        os.environ,
        FAKE_INSTALL=msys(install),
        REPRINTBUDDY_DESTDIR=msys(root),
        FIXTURES=msys(fixtures),
        PATH=f"{shims.as_posix()}{os.pathsep}{os.environ['PATH']}",
    )
    env.pop("REPRINTBUDDY_REPO", None)
    script = f'source "{msys(functions)}"; REPO_URL="{REPO}"; require_repo_url; {command}'
    done = subprocess.run(
        [bash, "-c", script], capture_output=True, text=True, encoding="utf-8", env=env,
        stdin=subprocess.DEVNULL, timeout=120,
    )
    return done, install


def test_the_package_is_downloaded_checked_and_put_in_place(bash, tmp_path):
    import hashlib
    import json

    fixtures = package_fixture(tmp_path / "fixtures")
    done, install = _call(bash, tmp_path, "fetch_code", fixtures)
    assert done.returncode == 0, done.stderr
    assert (install / "releases" / PINNED / "pyproject.toml").is_file()
    assert (install / "releases" / PINNED / "install" / "install.sh").is_file()
    assert not (install / "releases" / ".download").exists()
    marker = json.loads((install / "release.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256((fixtures / PACKAGE).read_bytes()).hexdigest()
    assert marker == {"tag": PINNED, "repo": "owner/reprintbuddy", "sha256": digest}
    asked = (fixtures / "asked.txt").read_text(encoding="utf-8").split()
    assert asked == [f"{REPO_BASE}/releases/download/{PINNED}/{PACKAGE}", f"{REPO_BASE}/releases/download/{PINNED}/{PACKAGE}.sha256"]


@pytest.mark.parametrize(
    "fixture, words",
    [
        ({"checksum": "0" * 64}, "checksum"),
        ({"extra": "stray.txt"}, "more than the folder"),
        ({"extra": f"reprintbuddy-{PINNED}/../../evil"}, "more than the folder"),
        ({"tag": "v0.0.1"}, "could not be downloaded"),  # no package of this script's release
    ],
)
def test_a_package_that_does_not_check_out_installs_nothing(bash, tmp_path, fixture, words):
    fixtures = package_fixture(tmp_path / "fixtures", **fixture)
    done, install = _call(bash, tmp_path, "fetch_code", fixtures)
    assert done.returncode == 1
    assert words in done.stderr, done.stderr
    assert not (install / "release.json").exists()
    assert not (install / "releases" / PINNED).exists()
    assert not (tmp_path / "root" / "evil").exists() and not (install / "evil").exists()


def test_an_unfinished_git_installation_gives_way_to_the_package(bash, tmp_path, origin_repo):
    """v0.4.9: a first run of an older script died after its clone (no database). The git copy goes, the
    package of this script's release comes; nothing of git is used."""
    import json

    fixtures = package_fixture(tmp_path / "fixtures")
    done, install = _call(bash, tmp_path, "fetch_code", fixtures, git_clone_of=origin_repo)
    assert done.returncode == 0, done.stderr
    assert "Unfinished installation found" in done.stdout
    assert sorted(path.name for path in install.iterdir()) == ["release.json", "releases"]
    assert json.loads((install / "release.json").read_text(encoding="utf-8"))["tag"] == PINNED



def test_review_a_checksum_line_that_names_another_file_installs_nothing(bash, tmp_path):
    """Final review, minor 5: `sha256sum -c` checks the file the line names; it must name the package."""
    import hashlib

    fixtures = package_fixture(tmp_path / "fixtures")
    (fixtures / "other.txt").write_text("x", encoding="utf-8")
    digest = hashlib.sha256(b"x").hexdigest()
    (fixtures / f"{PACKAGE}.sha256").write_text(f"{digest}  other.txt\n", encoding="utf-8", newline="\n")
    done, install = _call(bash, tmp_path, "fetch_code", fixtures)
    assert done.returncode == 1
    assert "does not name the package" in done.stderr, done.stderr
    assert not (install / "release.json").exists()


def test_review_root_reads_nothing_in_the_folders_of_the_service_user():
    """Final review, minors 3 and 4: in `releases/` (the service user's) root neither reads (checksum, list
    of the archive) nor removes anything; the service user does it (the script's own rule)."""
    import re as _re

    text = INSTALL_SH.read_text(encoding="utf-8")
    body = _re.search(r"^download_release\(\) \{\n(.*?)\n\}\n", text, _re.S | _re.M).group(1)
    for line in body.splitlines():
        if "tar -tzf" in line or "cut -d" in line:
            assert "capture_as_service_user" in line, line
        assert "${DESTDIR}${work}" not in line, line
    install_python = _re.search(r"^install_python\(\) \{\n(.*?)\n\}\n", text, _re.S | _re.M).group(1)
    assert 'as_service_user rm -rf "${SOURCE_DIR}/build"' in install_python


def test_review_the_top_folder_check_takes_the_dots_of_the_tag_literally(bash, tmp_path):
    """Final review, minor 9: `reprintbuddy-v0x4y9/` must not pass for `reprintbuddy-v0.4.9/`."""
    fixtures = package_fixture(tmp_path / "fixtures", extra=f"reprintbuddy-{PINNED.replace('.', 'x')}/x")
    done, install = _call(bash, tmp_path, "fetch_code", fixtures)
    assert done.returncode == 1
    assert "more than the folder" in done.stderr
