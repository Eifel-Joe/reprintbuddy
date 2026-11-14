"""The uninstall instructions (README and the page under docs/, EN and DE) name everything install.sh puts on the system.

Spec 11.3a (decided 15.11.2026): there is no uninstall command in the installer; the way out is a list of steps,
and the list must not go stale. The places are read from install/install.sh, not written down here: when the
installer creates something new, a test below fails until the instructions know about it.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = (ROOT / "install" / "install.sh").read_text(encoding="utf-8")

# (file, heading of the section in a README; None = the whole page)
INSTRUCTIONS = [
    ("README.md", "## Uninstall"),
    ("README.de.md", "## Deinstallation"),
    ("docs/operate/uninstall.md", None),
    ("docs/operate/uninstall.de.md", None),
]
IDS = [name for name, _ in INSTRUCTIONS]


def text_of(name: str, heading: str | None) -> str:
    text = (ROOT / name).read_text(encoding="utf-8").replace("\r\n", "\n")
    if heading is None:
        return text
    assert text.count("\n" + heading + "\n") == 1, f"{name}: one section {heading!r} expected"
    return text.split("\n" + heading + "\n", 1)[1].split("\n## ", 1)[0]


def variable(name: str) -> str:
    match = re.search(rf'^{name}="([^"]*)"$', INSTALL_SH, re.MULTILINE)
    assert match, name
    return match.group(1)


APP_USER = variable("APP_USER")
INSTALL_DIR = variable("INSTALL_DIR")
DATA_DIR = variable("DATA_DIR")
SERVICE = variable("SERVICE")


def function_body(name: str) -> str:
    start = INSTALL_SH.index(f"\n{name}() {{")
    return INSTALL_SH[start : INSTALL_SH.index("\n}\n", start)]


def system_files() -> list[str]:
    """Where install_system_files() puts the sudoers rule, the units and the command: the last word of each `install`."""
    body = function_body("install_system_files").replace("\\\n", " ")
    targets = re.findall(r'^\s*run install -m \S+ -o root -g root "\$\{STAGE_DIR\}/\S+"\s+(/\S+)\s*$', body, re.MULTILINE)
    assert len(targets) >= 4, targets
    return targets


def packages() -> list[str]:
    match = re.search(r"apt-get install -y --no-install-recommends ([^\n]+)", function_body("install_packages"))
    assert match
    return match.group(1).split()


def bash_lines(text: str) -> list[str]:
    """Every line inside a ```bash block."""
    lines = []
    for block in re.findall(r"```bash\n(.*?)```", text, re.DOTALL):
        lines.extend(line.strip() for line in block.splitlines() if line.strip())
    return lines


def test_the_places_are_read_from_the_installer():
    assert (APP_USER, INSTALL_DIR, DATA_DIR) == ("reprintbuddy", "/opt/reprintbuddy", "/var/lib/reprintbuddy")
    assert SERVICE == "reprintbuddy.service"
    assert "/etc/sudoers.d/reprintbuddy" in system_files()
    assert "/usr/local/bin/reprintbuddy" in system_files()
    assert "/etc/systemd/system/reprintbuddy-update.service" in system_files()
    assert "useradd --system --user-group" in INSTALL_SH  # a group of the same name comes with the user
    assert packages()[:2] == ["python3", "python3-venv"]


# Paths the installer only reads, or that are part of a place the instructions name as a whole.
READ_ONLY = {"/etc/os-release", "/usr/sbin/nologin", "/usr/bin/systemctl", "/dev/null"}


def test_the_installer_creates_nothing_the_instructions_do_not_know():
    """Every absolute path in install.sh is a documented place, a folder below one, or on the list of paths it only reads."""
    code = "\n".join(line for line in INSTALL_SH.splitlines() if not line.lstrip().startswith("#"))
    code = code.replace("${INSTALL_DIR}", INSTALL_DIR).replace("${DATA_DIR}", DATA_DIR).replace("${DESTDIR}", "")
    paths = set(re.findall(r"(?<![\w.:/${}-])(/(?:etc|usr|var|opt|srv|lib|home|root|run|bin|sbin|dev)/[\w./-]*[\w-])", code))
    known = {INSTALL_DIR, DATA_DIR, *system_files()}
    unknown = {
        path
        for path in paths
        if path not in READ_ONLY and not any(path == place or path.startswith(place + "/") for place in known)
    }
    assert not unknown, f"install.sh now touches {sorted(unknown)}: add it to the uninstall instructions, then to this test"
    for creator in ("ln -s", "groupadd", "adduser", "crontab", "update-alternatives", "systemctl enable --now"):
        assert creator not in code, f"install.sh now uses {creator!r}: the uninstall instructions must undo it"
    assert code.count("systemctl enable") == 1  # only the service itself is switched on


@pytest.mark.parametrize(("name", "heading"), INSTRUCTIONS, ids=IDS)
def test_the_instructions_name_every_place_the_installer_creates(name, heading):
    text = text_of(name, heading)
    for needle in (APP_USER, INSTALL_DIR, DATA_DIR, *system_files()):
        assert needle in text, (name, needle)
    lines = bash_lines(text)
    # the service is switched off (it was enabled), the user and the group are removed
    assert f"systemctl disable --now {SERVICE}" in lines, name
    assert f"userdel {APP_USER}" in lines, name
    assert any(line.startswith(f"getent group {APP_USER} && groupdel {APP_USER}") for line in lines), name
    assert "systemctl daemon-reload" in lines, name
    for target in system_files():
        assert f"rm {target}" in lines, (name, target)
    assert f"rm -rf {INSTALL_DIR}" in lines and f"rm -rf {DATA_DIR}" in lines, name


@pytest.mark.parametrize(("name", "heading"), INSTRUCTIONS, ids=IDS)
def test_the_instructions_say_that_the_system_packages_stay(name, heading):
    text = text_of(name, heading)
    for package in packages():
        assert f"`{package}`" in text, (name, package)


@pytest.mark.parametrize(("name", "heading"), INSTRUCTIONS, ids=IDS)
def test_every_command_stands_in_a_block_of_its_own(name, heading):
    """One command per block, so that each can be copied with one click."""
    blocks = re.findall(r"```bash\n(.*?)```", text_of(name, heading), re.DOTALL)
    assert len(blocks) >= 10, name
    for block in blocks:
        assert len([line for line in block.splitlines() if line.strip()]) == 1, (name, block)


@pytest.mark.parametrize(("name", "heading"), INSTRUCTIONS, ids=IDS)
def test_the_order_of_the_steps(name, heading):
    """The backup comes first, the units go before the reload, the user goes last."""
    text = text_of(name, heading)
    backup = "Full backup" if name.endswith("uninstall.md") or name == "README.md" else "Vollständige Sicherung"
    lines = bash_lines(text)
    assert text.index(backup) < text.index(lines[0]), name
    stop = lines.index(f"systemctl disable --now {SERVICE}")
    removals = [lines.index(f"rm {path}") for path in system_files()]
    reload = lines.index("systemctl daemon-reload")
    folders = lines.index(f"rm -rf {INSTALL_DIR}"), lines.index(f"rm -rf {DATA_DIR}")
    user = lines.index(f"userdel {APP_USER}")
    assert stop < min(removals) and max(removals) < reload < min(folders), name
    assert max(folders) < user, name


@pytest.mark.parametrize(("english", "german"), [
    ("README.md", "README.de.md"),
    ("docs/operate/uninstall.md", "docs/operate/uninstall.de.md"),
])
def test_both_languages_give_the_same_commands(english, german):
    headings = dict(INSTRUCTIONS)
    assert bash_lines(text_of(english, headings[english])) == bash_lines(text_of(german, headings[german]))


def test_readme_and_page_give_the_same_commands():
    headings = dict(INSTRUCTIONS)
    page = bash_lines(text_of("docs/operate/uninstall.md", None))
    assert bash_lines(text_of("README.md", headings["README.md"])) == page


@pytest.mark.parametrize(("name", "heading"), INSTRUCTIONS[:2], ids=IDS[:2])
def test_the_readme_points_at_the_page(name, heading):
    assert "operate/uninstall/" in text_of(name, heading), name


@pytest.mark.parametrize("name", ["docs/start/install.md", "docs/start/install.de.md"])
def test_the_installation_page_links_to_the_uninstall_page(name):
    assert "(../operate/uninstall.md)" in (ROOT / name).read_text(encoding="utf-8"), name


@pytest.mark.parametrize("name", ["README.md", "README.de.md"])
def test_the_readme_installation_section_points_to_the_uninstall_section(name):
    heading = dict(INSTRUCTIONS)[name]
    text = (ROOT / name).read_text(encoding="utf-8").replace("\r\n", "\n")
    section = text.split("\n## Installation\n", 1)[1].split("\n## ", 1)[0]
    assert "(#" + heading.removeprefix("## ").lower() + ")" in section, name


def test_the_navigation_and_its_german_name_know_the_page():
    config = (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    assert "- Uninstall: operate/uninstall.md" in config
    assert "Uninstall: Deinstallation" in config
