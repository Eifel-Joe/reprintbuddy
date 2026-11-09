"""The building blocks of the new look (macros in _ui.html, symbols, rules of the style sheet)."""

import re

import pytest
from markupsafe import Markup

from reprintbuddy.web.kpis import Kpi

CSS = "/static/css/reprintbuddy.css"
SPRITE = "/static/icons/icons.svg"


def render(client, source, **context):
    env = client.app.state.templates.env
    head = '{% import "_ui.html" as ui with context %}'
    values = {"lang": "en", "csrf_token": "CSRF", **context}
    return env.from_string(head + source).render(**values)


def test_page_head_subtitle_is_escaped(ready_client):
    html = render(ready_client, '{% call ui.page_head("package", "Title", "<b>x</b>") %}{% endcall %}')
    assert '<p class="page-sub">&lt;b&gt;x&lt;/b&gt;</p>' in html
    assert "<b>" not in html


def test_page_head_subtitle_markup_passes(ready_client):
    html = render(
        ready_client,
        '{% set sub %}<b>x</b>{% endset %}{% call ui.page_head("package", "Title", sub) %}{% endcall %}',
    )
    assert '<p class="page-sub"><b>x</b></p>' in html


def test_page_head_without_caller_content_has_no_actions(ready_client):
    html = render(ready_client, '{% call ui.page_head("package", "Title") %}  {% endcall %}')
    assert "page-actions" not in html
    assert "page-sub" not in html
    assert "<h1>Title</h1>" in html


def test_page_head_actions_on_the_right(ready_client):
    html = render(
        ready_client,
        '{% call ui.page_head("package", "Title", "Sub") %}<a class="btn" href="/x">Go</a>{% endcall %}',
    )
    assert '<div class="page-actions"><a class="btn" href="/x">Go</a></div>' in html


def test_kpi_row(ready_client):
    cards = [Kpi("package", "accent", 4, "Articles"), Kpi("x", "bad", 1, "<i>")]
    html = render(ready_client, "{{ ui.kpi_row(cards) }}", cards=cards)
    assert html.count('class="kpi"') == 2
    assert 'class="kpis"' in html
    assert '<span class="kpi-icon tone-accent">' in html
    assert '<span class="kpi-icon tone-bad">' in html
    assert '<span class="kpi-value">4</span>' in html
    assert '<span class="kpi-label">&lt;i&gt;</span>' in html


def test_kpi_with_href_is_a_link(ready_client):
    """The overview's cards lead to their page (Spec 10.2); a card without `href` stays a div."""
    cards = [Kpi("printer", "accent", 3, "Planned", href="/queue"), Kpi("x", "bad", 1, "Plain")]
    html = render(ready_client, "{{ ui.kpi_row(cards) }}", cards=cards)
    assert '<a class="kpi kpi-link" href="/queue">' in html
    assert html.count("<a ") == 1
    assert html.count('<div class="kpi">') == 1
    assert '<span class="kpi-value">3</span>' in html


def test_icon_button_is_a_link_with_title_and_label(ready_client):
    html = render(ready_client, '{{ ui.icon_button("pencil", "Edit <it>", "/a?x=1&y=2") }}')
    assert html.startswith('<a class="icon-btn"')
    assert 'title="Edit &lt;it&gt;"' in html
    assert 'aria-label="Edit &lt;it&gt;"' in html
    assert 'href="/a?x=1&amp;y=2"' in html
    assert "#pencil" in html


def test_icon_post_is_a_form_with_tokens(ready_client):
    html = render(ready_client, '{{ ui.icon_post("archive", "Archive", "/articles/1/archive") }}')
    assert '<form class="icon-form" method="post" action="/articles/1/archive">' in html
    assert 'name="csrf_token" value="CSRF"' in html
    assert 'name="form_token"' in html
    assert re.search(r'<button type="submit" class="icon-btn" title="Archive" aria-label="Archive">', html)


def test_row_actions(ready_client):
    html = render(ready_client, "{% call ui.row_actions() %}X{% endcall %}")
    assert '<div class="row-actions">X</div>' in html


def test_switch_checked(ready_client):
    html = render(ready_client, '{{ ui.switch("tls", true, "TLS", "tls-id") }}')
    assert 'type="checkbox"' in html
    assert 'class="switch"' in html
    assert 'role="switch"' in html
    assert 'name="tls"' in html
    assert 'id="tls-id"' in html
    assert 'aria-label="TLS"' in html
    assert " checked" in html


def test_switch_unchecked(ready_client):
    html = render(ready_client, '{{ ui.switch("tls", false, "TLS") }}')
    assert " checked" not in html
    assert "id=" not in html


def test_tabs_mark_exactly_one(ready_client):
    items = [
        ("general", "settings", "General", "?tab=general"),
        ("account", "log-out", "Account", "?tab=account"),
    ]
    html = render(ready_client, '{{ ui.tabs(items, "account") }}', items=items)
    assert '<nav class="tabs"' in html
    assert html.count('aria-current="page"') == 1
    assert re.search(r'<a class="tab active" href="\?tab=account" aria-current="page">', html)
    assert re.search(r'<a class="tab" href="\?tab=general">', html)


def test_card_head(ready_client):
    html = render(ready_client, '{% call ui.card_head("settings", "Server") %}<button>Save</button>{% endcall %}')
    assert '<div class="card-head"><h2>' in html
    assert "Server</h2>" in html
    assert "<div><button>Save</button></div>" in html


def test_setting_row(ready_client):
    html = render(
        ready_client,
        '{% call ui.setting_row("Port", "The <port>", "port") %}<input id="port">{% endcall %}',
    )
    assert '<div class="setting-row">' in html
    assert '<label for="port">Port</label>' in html
    assert "<span>The &lt;port&gt;</span>" in html
    assert '<div class="setting-control"><input id="port"></div>' in html


@pytest.mark.parametrize(
    "name",
    ["pencil", "archive-restore", "eye", "triangle-alert", "grip-vertical", "pause",
     "chevrons-left", "chevrons-right", "menu"],
)
def test_symbols_in_the_sprite(ready_client, name):
    assert f'<symbol id="{name}"' in ready_client.get(SPRITE).text


def test_mobile_menu_uses_the_menu_symbol(ready_client):
    html = ready_client.get("/").text
    mobile = re.search(r'<details class="mobile-menu">.*?</details>', html, re.S).group(0)
    summary = re.search(r"<summary>.*?</summary>", mobile, re.S).group(0)
    assert "#menu" in summary
    assert 'class="brand"' in summary
    assert "\\2630" not in ready_client.get(CSS).text


def test_css_kpis_two_columns_on_phone(ready_client):
    css = ready_client.get(CSS).text
    media = re.search(r"@media \(max-width: 899px\) \{[^@]*?\.kpis \{[^}]*repeat\(2", css)
    assert media, "no two-column .kpis rule inside the phone query"


def test_css_tap_targets(ready_client):
    css = ready_client.get(CSS).text
    assert re.search(r"\n\.btn \{[^}]*min-height: 2\.75rem", css)
    icon_btn = re.search(r"\n\.icon-btn \{[^}]*\}", css).group(0)
    assert "min-width: 2.75rem" in icon_btn and "min-height: 2.75rem" in icon_btn
    for selector in (".tab", ".filter"):
        assert re.search(r"\n" + re.escape(selector) + r" \{[^}]*min-height: 2\.75rem", css), selector


def test_kpi_row_in_live_region(ready_client):
    cards = [Kpi("package", "accent", 1, "A")]
    html = render(
        ready_client,
        '{% call ui.region("summary", "article") %}{{ ui.kpi_row(cards) }}{% endcall %}',
        cards=cards,
    )
    assert "<!--live:summary-->" in html and 'class="kpis"' in html


def test_markup_label_not_double_escaped(ready_client):
    cards = [Kpi("package", "accent", 1, Markup("<b>A</b>"))]
    assert "<b>A</b>" in render(ready_client, "{{ ui.kpi_row(cards) }}", cards=cards)
