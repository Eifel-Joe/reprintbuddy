"""The links to the code and to the documentation at the foot of the menu (v0.3.14, p188)."""

import re
from pathlib import Path

import pytest

from reprintbuddy import links
from reprintbuddy import settings as st
from reprintbuddy.web import i18n

WEB = Path(i18n.__file__).parent
ROOT = Path(__file__).resolve().parents[2]
CSS = "/static/css/reprintbuddy.css"


def _menus(html):
    sidebar = re.search(r'<aside class="sidebar">.*?</aside>', html, re.S).group(0)
    mobile = re.search(r'<details class="mobile-menu">.*?</details>', html, re.S).group(0)
    return {"sidebar": sidebar, "phone menu": mobile}


def _about_links(menu):
    block = re.search(r'<div class="nav-about">(.*?)</div>', menu, re.S)
    assert block, "no row of links"
    return re.findall(r"<a [^>]*>.*?</a>", block.group(1), re.S)


def _attr(tag, name):
    found = re.search(rf'\s{name}="([^"]*)"', tag)
    return found.group(1) if found else None


def test_the_addresses_are_the_published_ones():
    assert links.REPO_URL == "https://github.com/Eifel-Joe/reprintbuddy"
    assert links.DOCS_URL == "https://eifel-joe.github.io/reprintbuddy/"
    assert f"site_url: {links.DOCS_URL}" in (ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    assert links.REPO_URL in (ROOT / "README.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("menu", ["sidebar", "phone menu"])
def test_both_menus_link_to_the_code_and_the_docs(ready_client, menu):
    found = _about_links(_menus(ready_client.get("/").text)[menu])
    assert [_attr(a, "href") for a in found] == [links.REPO_URL, links.DOCS_URL]
    for tag in found:
        assert _attr(tag, "target") == "_blank"
        assert "noopener" in _attr(tag, "rel").split()
        assert _attr(tag, "title") and _attr(tag, "title") == _attr(tag, "aria-label")
        assert '<svg class="icon' in tag


def test_the_symbols_come_from_the_sprite(ready_client):
    found = _about_links(_menus(ready_client.get("/").text)["sidebar"])
    assert "#github" in found[0] and "#book-open" in found[1]
    sprite = (WEB / "static" / "icons" / "icons.svg").read_text(encoding="utf-8")
    for name in ("github", "book-open"):
        assert f'<symbol id="{name}" viewBox="0 0 24 24"' in sprite


def test_names_in_both_languages(ready_client):
    english = [_attr(a, "title") for a in _about_links(_menus(ready_client.get("/").text)["sidebar"])]
    assert english == ["Source code on GitHub", "Documentation"]
    with ready_client.app.state.sessionmaker() as s:
        st.set_setting(s, "language", "de")
    german = [_attr(a, "title") for a in _about_links(_menus(ready_client.get("/").text)["sidebar"])]
    assert german == ["Quellcode auf GitHub", "Dokumentation"]


def test_the_row_sits_between_the_account_links_and_the_version(ready_client):
    sidebar = _menus(ready_client.get("/").text)["sidebar"]
    assert sidebar.index('action="/logout"') < sidebar.index('class="nav-about"') < sidebar.index('class="nav-version-row"')


def test_the_links_are_on_every_page_with_the_menu(ready_client):
    for path in ("/articles", "/settings", "/queue"):
        assert links.REPO_URL in ready_client.get(path).text, path


def test_the_addresses_stay_out_of_the_templates():
    """The addresses live in one place (`links.py`); a template only receives them."""
    for path in (WEB / "templates").glob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert "github.com" not in text and "github.io" not in text, path.name


def test_collapsed_menu_stacks_the_links_and_each_target_is_44_px(ready_client):
    css = ready_client.get(CSS).text
    assert re.search(r"\.sidebar-collapsed \.sidebar \.nav-about \{[^}]*flex-direction: column", css)
    block = re.search(r"\.nav-about a \{([^}]*)\}", css)
    assert block and "min-width: 2.75rem" in block.group(1) and "min-height: 2.75rem" in block.group(1)


@pytest.mark.parametrize(("page", "file"), [("en", "index.md"), ("de", "index.de.md")])
def test_the_docs_describe_the_foot_of_the_menu(page, file):
    """Spec 11.8: what the menu shows is in the documentation, in both languages, with the names of the interface."""
    import json

    texts = json.loads((WEB.parent / "locales" / f"{page}.json").read_text(encoding="utf-8"))
    text = (ROOT / "docs" / file).read_text(encoding="utf-8")
    assert f"**{texts['nav.open_bambuddy']}**" in text and f"**{texts['nav.logout']}**" in text
    assert "GitHub" in text
