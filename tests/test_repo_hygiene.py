"""Keeps the public repository free of private network addresses and its documentation complete.

Project-specific checks do not belong in a public repository; this file only
holds checks that make sense for any project.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

# Addresses from the private ranges of RFC 1918. Examples in documents and tests
# use placeholders such as <bambuddy-host> or the documentation range 192.0.2.x.
PRIVATE_ADDRESS = re.compile(
    r"(?<![\d.])(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3})(?![\d.]*\d)"
)


def git(*args: str) -> str:
    if shutil.which("git") is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True, encoding="utf-8"
    ).stdout


def repo_files() -> list[Path]:
    """Tracked files plus new ones that are not ignored (so it works before the commit)."""
    names = git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split(chr(0))
    return [ROOT / n for n in names if n and (ROOT / n).is_file()]


@pytest.mark.parametrize(
    "address",
    ["10" + ".0.0.1", "10" + ".20.30.40", "172" + ".16.0.5", "172" + ".31.255.1", "192" + ".168.1.20"],
)
def test_private_addresses_are_recognised(address):
    assert PRIVATE_ADDRESS.search(f"http://{address}:8000/path")


@pytest.mark.parametrize(
    "text",
    [
        "http://192.0.2.10:8000",  # documentation range
        "127.0.0.1 and 0.0.0.0",
        "172" + ".15.0.1 and 172" + ".32.0.1",  # outside the private /12
        "version 1.10.0.1234",
        "<bambuddy-host>:8000",
    ],
)
def test_other_text_is_not_flagged(text):
    assert PRIVATE_ADDRESS.search(text) is None


def test_no_private_addresses_in_files():
    found = {}
    for path in repo_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary file
        if PRIVATE_ADDRESS.search(text):
            found[str(path.relative_to(ROOT))] = "private network address"
    assert found == {}


def test_no_private_addresses_in_commit_messages():
    assert PRIVATE_ADDRESS.search(git("log", "--all", "--format=%B")) is None


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("english,german", [("README.md", "README.de.md"), ("CONTRIBUTING.md", "CONTRIBUTING.de.md")])
def test_docs_bilingual(english, german):
    assert (ROOT / english).is_file() and (ROOT / german).is_file()
    assert german in _read(english), f"{english} must link {german}"
    assert english in _read(german), f"{german} must link {english}"


def test_security_policy_is_bilingual():
    text = _read("SECURITY.md")
    assert "Security" in text and "Sicherheit" in text
    assert "vulnerability" in text and "Sicherheitslücke" in text


def test_readme_covers_the_required_topics():
    text = _read("README.md")
    for topic in (
        "Bambuddy",
        "Print without review",
        "Read Status",
        "Manage Queue",
        "Spoolman",
        "install.sh",
        "set-password",
        "never controls",
        "configuration file",
    ):
        assert topic in text, topic
    german = _read("README.de.md")
    for topic in ("Bambuddy", "Spoolman", "set-password", "Konfigurationsdatei", "ohne Prüfung drucken"):
        assert topic in german, topic


def test_contributing_covers_the_required_topics():
    text = _read("CONTRIBUTING.md").lower()
    for topic in ("Bambuddy version", "Spoolman", "Docker", "printer", "one topic", "tests"):
        assert topic.lower() in text, topic


def _yaml(path: str):
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def _ids(form):
    return {item["id"]: item for item in form["body"] if item.get("id")}


def test_issue_templates():
    bug = _yaml(".github/ISSUE_TEMPLATE/bug_report.yml")
    ids = _ids(bug)
    for key in ("reprintbuddy_version", "bambuddy_version", "spoolman", "steps", "expected", "actual", "logs"):
        assert key in ids, key
    for key in ("reprintbuddy_version", "bambuddy_version", "spoolman", "steps", "expected", "actual"):
        assert ids[key]["validations"]["required"] is True, key
    assert "API keys and passwords" in ids["logs"]["attributes"]["description"]
    assert "Schlüssel und Passwörter" in ids["logs"]["attributes"]["description"]

    feature = _yaml(".github/ISSUE_TEMPLATE/feature_request.yml")
    assert _ids(feature)  # loads and has fields

    config = _yaml(".github/ISSUE_TEMPLATE/config.yml")
    assert config["blank_issues_enabled"] is False


def test_issue_template_labels_are_bilingual():
    bug = _yaml(".github/ISSUE_TEMPLATE/bug_report.yml")
    for item in bug["body"]:
        label = item.get("attributes", {}).get("label")
        if label:
            assert " / " in label, f"label not bilingual: {label}"


def test_pull_request_template_checklist():
    text = _read(".github/PULL_REQUEST_TEMPLATE.md")
    assert text.count("- [ ]") >= 6
    for topic in ("issue", "one topic", "tests", "secrets", "both languages"):
        assert topic.lower() in text.lower(), topic


def test_license_present():
    text = _read("LICENSE")
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in text
    assert "Version 3" in text
    pyproject = _read("pyproject.toml")
    assert 'license = "AGPL-3.0-or-later"' in pyproject
    assert 'license-files = ["LICENSE"]' in pyproject


@pytest.mark.parametrize(
    "readme,notice",
    [("README.md", "Copyright (C) 2026 Eifel-Joe"), ("README.de.md", "Copyright (C) 2026 Eifel-Joe")],
)
def test_readme_names_license_and_copyright(readme, notice):
    text = _read(readme)
    assert "AGPL-3.0-or-later" in text
    assert notice in text
    assert "to be decided" not in text.lower() and "wird noch festgelegt" not in text.lower()


@pytest.mark.parametrize(
    "name,heading",
    [("CONTRIBUTING.md", "## License of contributions"), ("CONTRIBUTING.de.md", "## Lizenz von Beiträgen")],
)
def test_contribution_clause(name, heading):
    text = _read(name)
    assert heading in text
    section = text.split(heading, 1)[1]
    assert "AGPL-3.0-or-later" in section
    assert "Eifel-Joe" in section


def test_pull_request_template_asks_for_license_agreement():
    text = _read(".github/PULL_REQUEST_TEMPLATE.md")
    assert "- [ ] I agree to the license terms for contributions in CONTRIBUTING.md" in text


@pytest.mark.parametrize(
    "readme,sentence",
    [
        ("README.md", "Reprintbuddy is an independent project and is not affiliated with Bambuddy or Bambu Lab."),
        ("README.de.md", "Reprintbuddy ist ein eigenständiges Projekt und nicht mit Bambuddy oder Bambu Lab verbunden."),
    ],
)
def test_readme_states_independence(readme, sentence):
    assert sentence in _read(readme)
