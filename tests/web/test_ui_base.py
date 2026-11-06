"""Icons, menu, page head and status badges (Spec 10.1)."""

import re
from pathlib import Path

from reprintbuddy import __version__
from reprintbuddy.domain import status
from reprintbuddy.web import i18n

WEB = Path(i18n.__file__).parent
SPRITE = WEB / "static" / "icons" / "icons.svg"

MENU_ICONS = [
    "layout-dashboard",
    "package",
    "boxes",
    "receipt",
    "arrow-left-right",
    "clipboard-check",
    "wrench",
    "list-ordered",
    "disc-3",
    "puzzle",
    "shopping-bag",
    "settings",
]


def _sidebar(html):
    return re.search(r'<nav class="sidebar-nav".*?</nav>', html, re.S).group(0)


def test_every_menu_entry_has_an_icon_and_a_label(ready_client):
    links = re.findall(r"<a [^>]*>.*?</a>", _sidebar(ready_client.get("/").text), re.S)
    assert len(links) == 12
    for link, name in zip(links, MENU_ICONS, strict=True):
        assert '<svg class="icon' in link
        assert f'<use href="/static/icons/icons.svg?v={__version__}#{name}"' in link
        assert '<span class="nav-label">' in link


def test_every_icon_used_is_in_the_sprite():
    sprite = SPRITE.read_text(encoding="utf-8")
    base = (WEB / "templates" / "base.html").read_text(encoding="utf-8")
    used = set(MENU_ICONS) | {"external-link", "log-out"}
    for path in (WEB / "templates").glob("*.html"):
        used |= set(re.findall(r"""ui\.icon\(\s*["']([a-z0-9-]+)["']""", path.read_text(encoding="utf-8")))
    for name in MENU_ICONS:
        assert f"'{name}'" in base or f'"{name}"' in base, name
    missing = [name for name in sorted(used) if f'<symbol id="{name}"' not in sprite]
    assert missing == []


def test_sprite_contains_all_planned_symbols():
    sprite = SPRITE.read_text(encoding="utf-8")
    planned = MENU_ICONS + [
        "external-link", "log-out", "search", "plus", "layout-grid", "list",
        "history", "archive", "refresh-cw", "circle-alert", "check", "x",
    ]  # fmt: skip
    assert [n for n in planned if f'<symbol id="{n}" viewBox="0 0 24 24"' not in sprite] == []


def test_sprite_is_served_and_licensed(ready_client):
    response = ready_client.get(f"/static/icons/icons.svg?v={__version__}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    licence = (WEB / "static" / "icons" / "LICENSE-lucide.txt").read_text(encoding="utf-8")
    assert licence.startswith("ISC License")


def test_translate_count():
    assert i18n.translate_count("en", "summary.articles", 1) == "1 article"
    assert i18n.translate_count("en", "summary.articles", 4) == "4 articles"
    assert i18n.translate_count("de", "summary.articles", 1) == "1 Artikel"
    assert i18n.translate_count("de", "summary.parts", 2) == "2 Zukaufteile"


def test_every_status_has_a_text():
    locales = i18n.load_locales()
    for lang in ("en", "de"):
        keys = [f"status.{kind}.{state}" for kind, states in status.STATES.items() for state in states]
        assert [k for k in keys + ["status.paused"] if k not in locales[lang]] == []


def test_status_colours_in_the_style_sheet(ready_client):
    css = ready_client.get("/static/css/reprintbuddy.css").text
    assert "--warn: #f59e0b" in css
    assert ".badge.flag" in css


def test_macros_render_icon_badge_and_summary(ready_client):
    env = ready_client.app.state.templates.env
    source = (
        '{% import "_ui.html" as ui with context %}'
        '{{ ui.icon("plus", "x") }}|{{ ui.badge("article", "overbooked") }}|{{ ui.badge("article", "ok") }}'
        '|{{ ui.badge("order", "paused") }}|{{ ui.badge("article", "archived") }}'
        '|{{ ui.summary("article", tc("summary.articles", 3), [("below_min", 2), ("ok", 1)]) }}'
        '|{% call ui.page_head("package", "Title") %}<p>sub</p>{% endcall %}'
    )
    html = env.from_string(source).render(lang="en", app_version=__version__)
    assert f'<svg class="icon x" aria-hidden="true" focusable="false"><use href="/static/icons/icons.svg?v={__version__}#plus"></use></svg>' in html
    assert '<span class="badge tone-bad flag">overbooked</span>' in html
    assert '<span class="badge tone-ok">enough</span>' in html
    assert '<span class="badge tone-neutral">paused</span>' in html
    assert '<span class="badge tone-neutral">Archived</span>' in html
    assert '<p class="summary">3 articles' in html
    assert '<span class="dot tone-bad"></span>2 below minimum' in html
    assert '<header class="page-head"><span class="page-icon">' in html
    assert "<h1>Title</h1><p>sub</p>" in html
