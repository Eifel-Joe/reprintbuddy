"""Last corrections of the new look: input width, tidy stylesheet, unknown state, the collapse
link on a page that answered a POST, redirect helper, collapsed version, tap target of the layout switch."""

import re
from datetime import date

from conftest import csrf_of, form_token_of, sm_of
from factories import make_article

from reprintbuddy.domain import orders
from reprintbuddy.domain.orders import ArticleItem
from reprintbuddy.web import kpis
from reprintbuddy.web.deps import safe_next

CSS = "/static/css/reprintbuddy.css"


def css_text(client):
    return client.get(CSS).text


def test_form_inputs_are_capped_at_40rem(ready_client):
    css = css_text(ready_client)
    rule = re.search(r"\n(\.card\.section[^{}]*)\{([^}]*max-width:\s*40rem[^}]*)\}", css)
    assert rule, "no 40rem cap on inputs in full-width form cards"
    selector = rule.group(1)
    assert 'input:not([type="checkbox"]):not([type="file"])' in selector
    assert ".card.section select" in selector
    assert ".card.section textarea" in selector


def test_wide_settings_fields_and_part_rows_keep_their_width(ready_client):
    css = css_text(ready_client)
    assert re.search(r"\.setting-row\.stack [^{}]*\{[^}]*max-width:\s*none", css)
    assert re.search(r"\.part-row[^{}]*\{[^}]*max-width:\s*none", css)


def test_no_plan_references_and_no_slate_in_stylesheet(ready_client):
    css = css_text(ready_client)
    assert "(Task 6)" not in css
    assert "(Task 7" not in css
    assert not re.search(r"Task \d", css)
    assert "slate" not in css.splitlines()[0]


def test_no_duplicate_rules(ready_client):
    css = css_text(ready_client)
    assert len(re.findall(r"^\.sr-only \{", css, re.M)) == 1
    assert len(re.findall(r"^\.btn \{ min-height: 2\.75rem; \}$", css, re.M)) == 1


def test_state_without_an_icon_gets_a_fallback_icon():
    """A state with a tone (`deliverable`) that the card icons do not list must not fail the page."""
    cards = kpis.state_cards(lambda key: f"<{key}>", "set", "package", 3, "All", [("deliverable", 2)])
    assert [(c.icon, c.value, c.label) for c in cards[1:]] == [("circle-alert", 2, "<status.set.deliverable>")]


def place_order(client):
    with sm_of(client).begin() as s:
        article = make_article(s, name="Template A", stock=5)
        return orders.create_order(
            s, reference="Order 4001", order_date=date(2026, 1, 15),
            items=[ArticleItem(article.id, 1)], actor="user",
        ).id


def test_collapse_link_on_post_answered_page_does_not_point_to_the_post_route(ready_client):
    o = place_order(ready_client)
    data = {"csrf_token": csrf_of(ready_client, f"/orders/{o}"),
            "form_token": form_token_of(ready_client, f"/orders/{o}", f"/orders/{o}/cancel")}
    page = ready_client.post(f"/orders/{o}/cancel", data=data)
    assert page.status_code == 200
    link = re.search(r'id="sidebar-toggle" href="([^"]*)"', page.text).group(1)
    assert "cancel" not in link
    assert link.endswith("next=/")


def test_collapse_link_on_get_page_keeps_path_and_query(ready_client):
    html = ready_client.get("/parts?q=x").text
    assert "/ui/sidebar?collapsed=1&amp;next=/parts%3Fq%3Dx" in html


def test_sidebar_redirect_uses_the_shared_helper():
    import inspect

    from reprintbuddy.web.routes import ui

    assert not hasattr(ui, "_local_path")
    assert "safe_next" in inspect.getsource(ui)
    assert safe_next("/parts?q=x") == "/parts?q=x"
    assert safe_next("//evil.example") == "/"


def test_collapsed_version_wraps_and_centres(ready_client):
    css = css_text(ready_client)
    rule = re.search(r"\.sidebar-collapsed \.sidebar \.nav-version \{([^}]*)\}", css).group(1)
    assert "overflow-wrap: anywhere" in rule
    assert "text-align: center" in rule


def test_layout_switch_has_a_44px_hit_area(ready_client):
    css = css_text(ready_client)
    rules = "".join(re.findall(r"\n\.layout-toggle a \{([^}]*)\}", css))
    assert "min-height: 2.75rem" in rules
    assert "min-width: 2.75rem" in rules
