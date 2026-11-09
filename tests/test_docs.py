"""The documentation pages (MkDocs, folder `docs/`, Spec 11.8) stay complete and in step with the code.

`mkdocs build --strict` runs in the CI (job `docs`); these checks need no MkDocs and run with the
normal suite: every page exists in both languages, the pictures are there and small, the
installation command on the pages is the one of this version.
"""

import importlib.util
import re
from pathlib import Path

import pytest
import yaml

import reprintbuddy

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
IMAGE_LIMIT = 150 * 1024  # bytes per picture: the pages and the repository stay small
INSTALL_URL = "https://raw.githubusercontent.com/Eifel-Joe/reprintbuddy/v{version}/install/install.sh"
PREFIX = "apt-get update && apt-get install -y curl && "


def config() -> dict:
    # The file holds no Python tags, so the safe loader reads it.
    return yaml.safe_load((ROOT / "mkdocs.yml").read_text(encoding="utf-8"))


def nav_files(entries) -> list[str]:
    found: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            found.append(entry)
        elif isinstance(entry, dict):
            for value in entry.values():
                found.extend([value] if isinstance(value, str) else nav_files(value))
    return found


def english_pages() -> list[Path]:
    return sorted(p for p in DOCS.rglob("*.md") if not p.name.endswith(".de.md"))


def german(page: Path) -> Path:
    return page.with_name(page.name[: -len(".md")] + ".de.md")


def load_hooks():
    spec = importlib.util.spec_from_file_location("docs_hooks", ROOT / "tools" / "docs_hooks.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_site_is_configured_for_both_languages():
    cfg = config()
    assert cfg["site_url"] == "https://eifel-joe.github.io/reprintbuddy/"
    plugin = next(p["i18n"] for p in cfg["plugins"] if isinstance(p, dict) and "i18n" in p)
    assert plugin["docs_structure"] == "suffix"
    assert [lang["locale"] for lang in plugin["languages"]] == ["en", "de"]
    assert cfg["theme"]["font"] is False  # no web fonts: a visit loads nothing from another host


def test_every_navigation_entry_is_a_page():
    files = nav_files(config()["nav"])
    assert files, "empty navigation"
    for name in files:
        assert (DOCS / name).is_file(), name


def test_every_page_is_in_the_navigation():
    in_nav = set(nav_files(config()["nav"]))
    on_disk = {p.relative_to(DOCS).as_posix() for p in english_pages()}
    assert on_disk == in_nav


def test_every_page_has_both_languages():
    for page in english_pages():
        assert german(page).is_file(), f"{page.relative_to(DOCS)} has no German twin"
    for page in DOCS.rglob("*.de.md"):
        english = page.with_name(page.name[: -len(".de.md")] + ".md")
        assert english.is_file(), f"{page.relative_to(DOCS)} has no English twin"


def test_every_page_has_a_title():
    for page in DOCS.rglob("*.md"):
        first = page.read_text(encoding="utf-8").lstrip().splitlines()[0]
        assert first.startswith("# "), page.relative_to(DOCS)


def test_pictures_exist_are_small_and_come_in_both_languages():
    images = DOCS / "assets" / "images"
    names = {lang: {p.name for p in (images / lang).glob("*.png")} for lang in ("en", "de")}
    assert names["en"] == names["de"], names
    for path in images.rglob("*.png"):
        assert path.stat().st_size <= IMAGE_LIMIT, (path.name, path.stat().st_size)
    for page in DOCS.rglob("*.md"):
        text = page.read_text(encoding="utf-8")
        lang = "de" if page.name.endswith(".de.md") else "en"
        for target in re.findall(r"!\[[^\]]*\]\(([^)\s]+)", text):
            if target.startswith("http"):
                continue
            resolved = (page.parent / target).resolve()
            assert resolved.is_file(), (page.relative_to(DOCS), target)
            if "assets/images/" in target:
                assert f"/images/{lang}/" in target.replace("\\", "/"), (page.relative_to(DOCS), target)


def test_hook_puts_the_version_into_the_pages():
    hooks = load_hooks()
    assert hooks.read_version() == reprintbuddy.__version__
    assert hooks.on_page_markdown("tag {{ version }}", None, None, None) == f"tag {reprintbuddy.__version__}"
    assert "{{ version }}" not in hooks.on_page_markdown("{{ version }} {{ version }}", None, None, None)


@pytest.mark.parametrize("suffix", [".md", ".de.md"])
def test_install_page_has_the_command_of_this_version(suffix):
    hooks = load_hooks()
    text = hooks.on_page_markdown((DOCS / "start" / f"install{suffix}").read_text(encoding="utf-8"), None, None, None)
    command = PREFIX + 'bash -c "$(curl -fsSL ' + INSTALL_URL.format(version=reprintbuddy.__version__) + ')"'
    assert command in text
    assert command + " _ --language de" in text


@pytest.mark.parametrize("name", ["README.md", "README.de.md"])
def test_readme_points_at_the_documentation_near_the_top(name):
    lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
    top = "\n".join(lines[:30])
    assert config()["site_url"] in top, name


@pytest.mark.parametrize("name", ["README.md", "README.de.md"])
def test_readme_pictures_exist(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    for target in re.findall(r"(?:!\[[^\]]*\]\(|<img src=\")([^)\s\"]+)", text):
        if target.startswith("http"):
            continue
        assert (ROOT / target).is_file(), (name, target)


def test_docs_extra_names_the_tools():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r"^docs = \[", text, re.M)
    for package in ("mkdocs-material", "mkdocs-static-i18n"):
        assert package in text


def test_pages_workflow_builds_strict_and_the_ci_checks_it():
    pages = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "mkdocs build --strict" in pages and "deploy-pages" in pages
    assert "mkdocs build --strict" in ci
    assert "site/" in (ROOT / ".gitignore").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["CONTRIBUTING.md", "CONTRIBUTING.de.md"])
def test_contributing_asks_for_the_documentation_in_the_same_pull_request(name):
    """Spec 11.8: README and the pages under docs/ come in the same PR when installation, setup or use changes."""
    text = (ROOT / name).read_text(encoding="utf-8")
    assert "`docs/`" in text and "README" in text
    assert "mkdocs build --strict" in text
    assert 'pip install -e ".[docs]"' in text
    assert "tools/docs_screenshots.py" in text


def test_pull_request_template_asks_about_the_documentation():
    text = (ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
    boxes = [line for line in text.splitlines() if line.startswith("- [ ]")]
    asking = [line for line in boxes if "`docs/`" in line]
    assert len(asking) == 1, boxes
    assert "README" in asking[0] and " / " in asking[0]  # both languages


@pytest.mark.parametrize(
    "name",
    ["start/requirements.md", "start/requirements.de.md", "start/install.md", "start/install.de.md",
     "operate/update-backup.md", "operate/update-backup.de.md"],
)
def test_pages_name_the_host_of_the_release_notes(name):
    """The update check asks GitHub's API for the release notes (Spec 11.3a): every list of hosts names it."""
    from reprintbuddy.ops import releasenotes

    host = releasenotes.API.removeprefix("https://")
    assert host in (DOCS / name).read_text(encoding="utf-8"), name


@pytest.mark.parametrize("name", ["README.md", "README.de.md"])
def test_readme_names_the_host_of_the_release_notes(name):
    from reprintbuddy.ops import releasenotes

    assert releasenotes.API.removeprefix("https://") in (ROOT / name).read_text(encoding="utf-8")
