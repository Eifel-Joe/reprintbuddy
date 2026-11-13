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
        "http://<server-ip>:8080/setup",
    ):
        assert needle in text, (name, needle)


@pytest.mark.parametrize("name", READMES)
def test_readme_explains_the_emergency_restore_on_the_command_line(name):
    """p100: when the service does not start, `reprintbuddy restore-backup` is the way back."""
    text = read(name)
    assert "reprintbuddy restore-backup /var/lib/reprintbuddy/" in text
    assert "systemctl stop reprintbuddy" in text and "systemctl start reprintbuddy" in text
    assert "reprintbuddy restore-backup <" in text  # in the list of commands


@pytest.mark.parametrize("name", READMES)
def test_install_points_at_the_published_repository(name):
    """Published: README and installer name the real repository, no placeholder."""
    assert "<owner>" not in read(name)
    assert 'REPO_URL_DEFAULT="https://github.com/Eifel-Joe/reprintbuddy"' in (ROOT / "install" / "install.sh").read_text(encoding="utf-8")


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
        ("README.md", "Fresh Debian systems often come without curl; the first part installs it."),
        ("README.de.md", "Frische Debian-Systeme bringen oft kein curl mit; der erste Teil installiert es."),
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
        "Print Without Review",
        "Read Printers",
        "Read Inventory",
        "Read Filaments",
        "Read Settings",
        "Read Api Keys",
        "Create Api Keys",
    ):
        assert label in text, (name, label)
    for word in ("expiry date", "no restriction", "Ablaufdatum", "keine Einschränkung"):
        assert word not in text, (name, word)  # the Bambuddy dialog has no such fields
    assert "Start Unreviewed Queue" not in text, name  # Bambuddy shows "Print Without Review"


@pytest.mark.parametrize("name", READMES)
def test_readme_spoolman_is_optional(name):
    """Spec 9.1, 11.8: Bambuddy runs with its own spool management or with Spoolman."""
    text = read(name)
    requirements = text[text.index("## Requirements" if name == "README.md" else "## Voraussetzungen") :]
    first = requirements.split(chr(10) + "- ", 2)[1]  # the Bambuddy line
    assert "optional" in first and "Spoolman" in first, name
    assert "Spoolman mode" not in text and "Spoolman-Modus**" not in text, name


REPO ="https://github.com/Eifel-Joe/reprintbuddy"
HEADLINE = {
    "README.md": (
        "Your shelf stays stocked. Bambuddy does the printing.",
        "Self-hosted stock keeping for 3D-printed items, with reprints through Bambuddy's queue.",
    ),
    "README.de.md": (
        "Dein Regal bleibt voll. Bambuddy druckt.",
        "Selbst gehostete Lagerführung für 3D-gedruckte Teile, Nachdrucke über die Warteschlange von Bambuddy.",
    ),
}
BADGES = [
    (f"{REPO}/releases", "https://img.shields.io/github/v/release/Eifel-Joe/reprintbuddy", "Release"),
    (
        f"{REPO}/actions/workflows/ci.yml",
        "https://img.shields.io/github/actions/workflow/status/Eifel-Joe/reprintbuddy/ci.yml?branch=main&logo=github&label=CI",
        "CI",
    ),
    (f"{REPO}/blob/main/LICENSE", "https://img.shields.io/github/license/Eifel-Joe/reprintbuddy", "License: AGPL-3.0"),
    (f"{REPO}/stargazers", "https://img.shields.io/github/stars/Eifel-Joe/reprintbuddy?style=flat", "Stars"),
    (f"{REPO}/issues", "https://img.shields.io/github/issues/Eifel-Joe/reprintbuddy", "Issues"),
]


def head_of(name):
    """Everything above the language switch."""
    text = read(name)
    marker = "[Deutsch: README.de.md]" if name == "README.md" else "[English: README.md]"
    return text[: text.index(marker)]


@pytest.mark.parametrize("name", READMES)
def test_readme_head_has_the_centred_logo_and_name(name):
    head = head_of(name)
    assert head.startswith('<p align="center">\n  <img src="src/reprintbuddy/web/static/img/logo.svg"')
    assert 'alt="Reprintbuddy" width="128" height="128">' in head
    assert '<h1 align="center">Reprintbuddy</h1>' in head
    assert (ROOT / "src/reprintbuddy/web/static/img/logo.svg").is_file()  # the path in the head exists


@pytest.mark.parametrize("name", READMES)
def test_readme_head_has_headline_and_subtitle(name):
    headline, subtitle = HEADLINE[name]
    head = head_of(name)
    assert f"<strong>{headline}</strong><br>\n  {subtitle}" in head
    assert head.index("</h1>") < head.index(headline) < head.index("img.shields.io")


@pytest.mark.parametrize("name", READMES)
def test_readme_head_has_exactly_the_five_badges(name):
    head = head_of(name)
    for link, image, alt in BADGES:
        assert f'<a href="{link}"><img src="{image}" alt="{alt}"></a>' in head, (name, alt)
    assert head.count("img.shields.io") == 5
    lowered = head.lower()
    for forbidden in ("discord", "ko-fi", "sponsor", "codeql", "security-audit", "security audit"):
        assert forbidden not in lowered, (name, forbidden)


def test_both_readme_heads_are_built_the_same_way():
    """Same markup in both languages; only the headline and subtitle differ."""

    def skeleton(name):
        head = head_of(name)
        for text in HEADLINE[name]:
            head = head.replace(text, "TEXT")
        return head

    assert skeleton("README.md") == skeleton("README.de.md")


@pytest.mark.parametrize(
    "name,status",
    [
        ("README.md", "> **Status:** under development, not ready for real use yet."),
        ("README.de.md", "> **Stand:** in Entwicklung, noch nicht einsatzbereit."),
    ],
)
def test_readme_status_line_is_true(name, status):
    text = read(name)
    assert status in text
    assert "no release yet" not in text and "noch keine Version veröffentlicht" not in text


@pytest.mark.parametrize(
    ("name", "group_later", "key_later"),
    [
        ("README.md", "add a missing right to the group at any time", "cannot be changed afterwards"),
        ("README.de.md", "jederzeit ergänzen", "nicht nachträglich ändern"),
    ],
)
def test_readme_says_which_rights_can_be_added_later_and_which_need_a_new_key(name, group_later, key_later):
    """p109: a missing right of the group is added there; a missing right at the key needs a new key."""
    text = " ".join(read(name).split())  # the sentences are wrapped
    assert group_later in text
    assert key_later in text


@pytest.mark.parametrize("name", [*READMES, "docs/start/install.md", "docs/start/install.de.md"])
def test_install_texts_do_not_say_the_script_installs_the_newest_release(name):
    """v0.3.14 p168: the script installs the release of its own tag (RELEASE_TAG in install.sh). The texts
    said "the newest release" and that the tag in the address only named the script."""
    text = read(name)
    for old in (
        "puts the newest release in place",
        "puts the **newest release** in place",
        "legt das neueste Release auf",
        "legt das **neueste Release** auf",
        "on the newest release tag",
        "auf dem neuesten Release-Tag",
        "is the version of the installer",
        "ist die Version des Installationsskripts",
    ):
        assert old not in text, (name, old)


PLANNED = [
    ("README.md", "## Planned", ["label printers", "Avery", "Shopify", "email", "webhook", "shelf", "several users", "pre-releases"]),
    ("README.de.md", "## Geplant", ["Etikettendrucker", "Avery", "Shopify", "E-Mail", "Webhook", "Regal", "mehrere Benutzer", "Vorabversionen"]),
    ("docs/index.md", "## Planned", ["label printers", "Avery", "Shopify", "email", "webhook", "shelf", "several users", "pre-releases"]),
    ("docs/index.de.md", "## Geplant", ["Etikettendrucker", "Avery", "Shopify", "E-Mail", "Webhook", "Regal", "mehrere Benutzer", "Vorabversionen"]),
]


@pytest.mark.parametrize("name, heading, words", PLANNED)
def test_what_is_planned_is_listed_without_dates(name, heading, words):  # v0.3.14: the "Planned" section
    """A short list of directions in the README and on the start page: no dates, no version numbers."""
    text = read(name).replace("\r\n", "\n")
    assert text.count(heading + "\n") == 1, name
    section = text.split(heading + "\n", 1)[1].split("\n## ", 1)[0]
    for word in words:
        assert word in section, (name, word)
    assert not any(char.isdigit() for char in section), "no dates or version numbers in the planned list"
