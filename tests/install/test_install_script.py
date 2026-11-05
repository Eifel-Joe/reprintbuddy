"""The install script is only exercised with `bash -n`, `shellcheck` and `--dry-run`.

`REPRINTBUDDY_DESTDIR` (allowed only together with `--dry-run`) makes the script look
for existing files under a test folder instead of the real root.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = ROOT / "install" / "install.sh"
CLI_SH = ROOT / "install" / "reprintbuddy-cli.sh"
REPO = "https://example.invalid/reprintbuddy.git"
RUNUSER = "+ setsid --wait runuser -u reprintbuddy -- env HOME=/var/lib/reprintbuddy"


@pytest.fixture
def bash():
    found = shutil.which("bash")
    if not found:
        pytest.skip("bash is not installed")
    return found


def os_release(dest: Path, id_: str = "debian", version: str = "13") -> None:
    (dest / "etc").mkdir(parents=True, exist_ok=True)
    (dest / "etc" / "os-release").write_text(f'ID={id_}\nVERSION_ID="{version}"\n', encoding="utf-8")


def run(bash, dest, *args, dry=True, env_extra=None, destdir=True):
    env = dict(os.environ)
    env.pop("REPRINTBUDDY_REPO", None)
    env.pop("REPRINTBUDDY_DESTDIR", None)
    if destdir:
        env["REPRINTBUDDY_DESTDIR"] = dest.as_posix()
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
        "apt-get install -y --no-install-recommends python3 python3-venv git curl sudo ca-certificates",
        "useradd --system",
        "chmod 700 /var/lib/reprintbuddy",
        "git clone https://example.invalid/reprintbuddy.git /opt/reprintbuddy",
        "checkout --quiet --detach vX.Y.Z",
        "python3 -m venv /opt/reprintbuddy/.venv",
        "rm -rf /opt/reprintbuddy/build",
        "-m pip install --no-cache-dir --upgrade /opt/reprintbuddy",
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
        "Finish the setup now: http://<container-ip>:8080/setup",
    ]
    position = 0
    for part in expected:
        found = out.find(part, position)
        assert found >= 0, f"{part!r} missing or out of order in:\n{out}"
        position = found + len(part)
    assert "anyone in your network" in out
    for line in out.splitlines():
        if any(word in line for word in ("git clone", "-m venv", "-m pip", "reprintbuddy init", "checkout --quiet")):
            assert line.startswith(RUNUSER), line
        assert not line.startswith(("+ git", "+ python3 ", "+ /opt/reprintbuddy/.venv")), line


def test_dry_run_changes_nothing(bash, tmp_path):
    os_release(tmp_path)
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    assert run(bash, tmp_path, "--repo", REPO).returncode == 0
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*")) == before


def test_dry_run_debian12(bash, tmp_path):
    os_release(tmp_path, version="12")
    assert run(bash, tmp_path, "--repo", REPO).returncode == 0


def test_refuses_non_debian(bash, tmp_path):
    os_release(tmp_path, id_="ubuntu", version="24.04")
    result = run(bash, tmp_path, "--repo", REPO)
    assert result.returncode == 1
    assert "Reprintbuddy needs Debian 12 or 13." in result.stderr + result.stdout


def test_refuses_old_debian(bash, tmp_path):
    os_release(tmp_path, version="11")
    assert run(bash, tmp_path, "--repo", REPO).returncode == 1


def test_refuses_missing_os_release(bash, tmp_path):
    result = run(bash, tmp_path, "--repo", REPO)
    assert result.returncode == 1
    assert "needs Debian 12 or 13" in result.stderr + result.stdout


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
    """Published: without --repo the script clones the public repository."""
    os_release(tmp_path)
    result = run(bash, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "git clone https://github.com/Eifel-Joe/reprintbuddy.git /opt/reprintbuddy" in lines(result)


def test_repo_from_environment(bash, tmp_path):
    os_release(tmp_path)
    result = run(bash, tmp_path, env_extra={"REPRINTBUDDY_REPO": REPO})
    assert result.returncode == 0
    assert f"git clone {REPO} /opt/reprintbuddy" in lines(result)


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


def test_rerun_keeps_database(bash, tmp_path):
    os_release(tmp_path)
    for folder in ("opt/reprintbuddy/.git", "opt/reprintbuddy/.venv", "var/lib/reprintbuddy"):
        (tmp_path / folder).mkdir(parents=True)
    (tmp_path / "var/lib/reprintbuddy/reprintbuddy.db").write_bytes(b"x")
    result = run(bash, tmp_path, "--repo", REPO)
    assert result.returncode == 0, result.stderr
    out = lines(result)
    assert "Existing installation found, keeping version." in out
    assert "Existing database kept." in out
    assert "systemctl restart reprintbuddy.service" in out
    for forbidden in ("git clone", "checkout", "init --db", "-m venv"):
        assert forbidden not in out, forbidden


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
    assert 'REPO_URL_DEFAULT="https://github.com/Eifel-Joe/reprintbuddy.git"' in text


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


def test_rerun_after_an_aborted_first_install_switches_to_the_release(bash, tmp_path):
    """First run died after the clone (no release yet): no database, default branch checked out."""
    os_release(tmp_path)
    (tmp_path / "opt/reprintbuddy/.git").mkdir(parents=True)
    out = lines(run(bash, tmp_path, "--repo", REPO))
    assert "Unfinished installation found" in out
    assert "git -C /opt/reprintbuddy fetch --prune --prune-tags --force --tags origin" in out
    assert "checkout --quiet --detach vX.Y.Z" in out
    assert "git clone" not in out
    assert "Existing installation found, keeping version." not in out
    for line in out.splitlines():
        if "fetch --tags" in line or "checkout --quiet" in line:
            assert line.startswith(RUNUSER), line


def test_script_and_python_use_the_same_release_tag_rule():
    from reprintbuddy.ops.versions import RELEASE_TAG

    number = "(0|[1-9][0-9]*)"
    assert rf"'^v{number}\.{number}\.{number}$'" in INSTALL_SH.read_text(encoding="utf-8")
    assert RELEASE_TAG.pattern == rf"^v{number}\.{number}\.{number}$"
