"""The README sections for installation, update, restart, HTTPS and export (EN and DE)."""

from pathlib import Path

import pytest

import reprintbuddy

from reprintbuddy.ops.service import (
    RESTART_CMD,
    RESTART_WAIT_CMD,
    START_CMD,
    START_UPDATE_CMD,
    STOP_CMD,
)

ROOT = Path(__file__).resolve().parents[1]
READMES = ("README.md", "README.de.md")
PREFIX = "apt-get update && apt-get install -y curl && "
URL_TEMPLATE = "https://raw.githubusercontent.com/Eifel-Joe/reprintbuddy/{ref}/install/install.sh"


def install_command(ref: str) -> str:
    return PREFIX + 'bash -c "$(curl -fsSL ' + URL_TEMPLATE.format(ref=ref) + ')"'


# the main command installs exactly the version this code carries (installer and code belong together)
INSTALL_COMMAND = install_command("v" + reprintbuddy.__version__)


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", READMES)
def test_install_command(name):
    assert INSTALL_COMMAND in read(name)
    assert (ROOT / "install" / "install.sh").is_file()


@pytest.mark.parametrize("name", READMES)
def test_install_command_with_language(name):
    assert INSTALL_COMMAND + " _ --language de" in read(name)


@pytest.mark.parametrize(
    "name,headings",
    [
        ("README.md", ("Installation", "Update", "Restart", "HTTPS", "Backup and export", "Command line")),
        ("README.de.md", ("Installation", "Update", "Neustart", "HTTPS", "Sicherung und Export", "Kommandozeile")),
    ],
)
def test_ops_sections(name, headings):
    titles = [line.lstrip("#").strip() for line in read(name).splitlines() if line.startswith("#")]
    for heading in headings:
        assert any(heading in title for title in titles), (name, heading, titles)


@pytest.mark.parametrize("name", READMES)
def test_readme_sudoers_matches(name):
    text = read(name)
    for command in (RESTART_CMD, RESTART_WAIT_CMD, START_UPDATE_CMD, STOP_CMD, START_CMD):
        assert " ".join(command[2:]) in text, (name, command)


@pytest.mark.parametrize("name", READMES)
def test_readme_outbound_hosts(name):
    text = read(name)
    for host in ("pypi.org", "files.pythonhosted.org", "raw.githubusercontent.com", "github.com"):
        assert host in text, (name, host)


@pytest.mark.parametrize("name", READMES)
def test_readme_names_the_paths_and_commands(name):
    text = read(name)
    for needle in (
        "/opt/reprintbuddy",
        "/var/lib/reprintbuddy",
        "/var/lib/reprintbuddy/backups/",
        "reprintbuddy-update.service",
        "journalctl -u reprintbuddy-update",
        "systemctl restart reprintbuddy",
        "reprintbuddy disable-https",
        "reprintbuddy set-port",
        "reprintbuddy --version",
        "http://<container-ip>:8080/setup",
    ):
        assert needle in text, (name, needle)


@pytest.mark.parametrize("name", READMES)
def test_install_points_at_the_published_repository(name):
    """Published: README and installer name the real repository, no placeholder."""
    assert "<owner>" not in read(name)
    assert 'REPO_URL_DEFAULT="https://github.com/Eifel-Joe/reprintbuddy.git"' in (ROOT / "install" / "install.sh").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", READMES)
def test_install_command_bound_to_a_tag(name):
    """Installer and code must be one version: the URL with the tag installs exactly that pair."""
    assert install_command("<tag>") in read(name)


@pytest.mark.parametrize("name,lang", [("README.md", "en"), ("README.de.md", "de")])
def test_readme_explains_the_way_out_of_a_failed_update(name, lang):
    """The page says 'interrupted' or 'could not be restored'; the README says what to do then."""
    from reprintbuddy.web import i18n

    texts = i18n.load_locales()[lang]
    text = read(name)
    assert texts["ops.update.state.interrupted"] in text
    assert texts["ops.update.state.rollback_failed"] in text
    assert "/var/lib/reprintbuddy/backups/" in text


@pytest.mark.parametrize("name", READMES)
def test_readme_tag_example_names_the_current_version(name):
    import reprintbuddy

    assert f"`v{reprintbuddy.__version__}`" in read(name)


@pytest.mark.parametrize("name", READMES)
def test_readme_first_run_has_four_steps_and_the_permission_list(name):
    from reprintbuddy.bambuddy.connection import REQUIRED_PERMISSIONS

    text = read(name)
    for code in REQUIRED_PERMISSIONS:
        assert f"`{code}`" in text, (name, code)
    assert "Operators" in text and "Administrators" in text, name  # why no system group fits
    assert "`reprintbuddy`" in text, name
    # the old way (typing the user name, ticking a box) is gone
    assert "confirm that the user has" not in text and "bestätigen, dass der Benutzer" not in text, name
    first_run = text[text.index("## First run" if name == "README.md" else "## Erster Aufruf") :]
    first_run = first_run[: first_run.index("\n## ", 5)]
    for number in ("1.", "2.", "3.", "4."):
        assert f"\n{number} " in first_run, (name, number)


@pytest.mark.parametrize(
    "name,sentence",
    [
        ("README.md", "Fresh Debian containers often come without curl; the first part installs it."),
        ("README.de.md", "Frische Debian-Container bringen oft kein curl mit; der erste Teil installiert es."),
    ],
)
def test_readme_says_why_the_install_command_installs_curl(name, sentence):
    assert sentence in read(name)


@pytest.mark.parametrize("name", READMES)
def test_every_install_command_in_the_readme_installs_curl_first(name):
    """A bare `bash -c "$(curl ...` line would fail on a fresh container."""
    for line in read(name).splitlines():
        if "curl -fsSL" in line and line.lstrip().startswith("bash -c"):
            raise AssertionError(f"{name}: install command without the curl installation: {line}")


@pytest.mark.parametrize("name", READMES)
def test_readme_names_every_key_setting(name):
    text = read(name)
    for label in (
        "Read Status",
        "Manage Queue",
        "Control Printer",
        "Manage Library",
        "Manage Inventory",
        "Manage Maintenance",
        "Manage Archives",
        "Manage Projects",
        "Allow Cloud Access",
        "Update Electricity Price",
        "Send Notifications",
    ):
        assert label in text, (name, label)


@pytest.mark.parametrize("name", READMES)
def test_readme_uses_bambuddys_labels_and_has_no_printer_or_expiry_setting(name):
    text = read(name)
    for label in (
        "Read All Library",
        "Read All Queue",
        "Create Queue",
        "Start Unreviewed Queue",
        "Read Printers",
        "Read Inventory",
        "Read Settings",
        "Read Api Keys",
        "Create Api Keys",
    ):
        assert label in text, (name, label)
    for word in ("expiry date", "no restriction", "Ablaufdatum", "keine Einschränkung"):
        assert word not in text, (name, word)  # the Bambuddy dialog has no such fields
