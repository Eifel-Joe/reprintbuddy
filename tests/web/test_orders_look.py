"""The order pages in the new look: status badges, short summary, search, filters, tiles or list,
live regions, and forms that keep a typed note when the order changes elsewhere
(Spec 10.1, 10.5; plan Task 14)."""

import re
from datetime import date

import pytest
from conftest import csrf_of, form_token_of, sm_of
from factories import make_article

from reprintbuddy import settings as st
from reprintbuddy.domain import catalog, orders, stock
from reprintbuddy.domain.catalog import BaseInput, SetInput
from reprintbuddy.domain.orders import ArticleItem
from reprintbuddy.web import regions
from reprintbuddy.web.i18n import translate


def en(key, **params):
    return translate("en", key, **params)


def article(client, name="Template A", stock_=0):
    with sm_of(client).begin() as s:
        return make_article(s, name=name, stock=stock_).id


def place(client, article_id, qty=1, reference="Order 1001"):
    with sm_of(client).begin() as s:
        return orders.create_order(
            s, reference=reference, order_date=date(2026, 1, 15),
            items=[ArticleItem(article_id, qty)], actor="user",
        ).id


def cancel(client, order_id):
    with sm_of(client).begin() as s:
        orders.cancel_order(s, order_id, "user")


def ship(client, order_id):
    with sm_of(client).begin() as s:
        orders.ship_order(s, order_id, "user")


def open_pair(client):
    """Order 1001 waits for stock, Order 1002 is ready to ship."""
    a = article(client, stock_=1)
    waiting = place(client, a, 2, "Order 1001")
    ready = place(client, a, 1, "Order 1002")
    return waiting, ready


def closed_pair(client):
    """Order 2001 shipped, Order 2002 cancelled."""
    a = article(client, name="Template B", stock_=5)
    shipped = place(client, a, 1, "Order 2001")
    cancelled = place(client, a, 1, "Order 2002")
    ship(client, shipped)
    cancel(client, cancelled)
    return shipped, cancelled


def cards_of(page):
    """The cards of the summary region (a whole page or the refetched piece) as (tone, value, label)."""
    block = page if page.startswith("<div") else regions.extract(page, ["summary"])["summary"]
    return [
        (tone, int(value), label)
        for tone, value, label in re.findall(
            r'<span class="kpi-icon tone-(\w+)">.*?</span><div><span class="kpi-value">(\d+)</span>'
            r'<span class="kpi-label">(.*?)</span>', block, re.S)
    ]


def text_of(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html)).strip()


def region(page, name):
    return regions.extract(page, [name])[name]


def regions_of(client, path, *names):
    """Fetch regions as live.js does; the answer is the regions one after the other (never nested)."""
    response = client.get(path, headers={regions.HEADER: " ".join(names)})
    assert response.status_code == 200 and response.headers["x-live-regions"] == " ".join(names)
    marker = '<div class="live-region" data-live-region="'
    pieces = [marker + piece for piece in response.text.split(marker)[1:]]
    return {re.match(r'[^>]*data-live-region="([^"]+)"', piece).group(1): piece for piece in pieces}


def region_tag(page, name):
    return re.search(rf'<div class="live-region" data-live-region="{name}"[^>]*>', page).group(0)


def forms_in(html):
    return re.findall(r"<form\b[^>]*>.*?</form>", html, re.S)


def with_tokens(client, order_id):
    return {"csrf_token": csrf_of(client, f"/orders/{order_id}"),
            "form_token": form_token_of(client, f"/orders/{order_id}", f"/orders/{order_id}/cancel")}


# ---- list ------------------------------------------------------------------------------------


def test_order_badges_follow_the_rules(ready_client):  # Spec 10.1
    open_pair(ready_client)
    listed = region(ready_client.get("/orders").text, "list")
    assert '<span class="badge tone-warn">open</span>' in listed
    assert '<span class="badge tone-ok">ready to ship</span>' in listed
    closed_pair(ready_client)
    closed = region(ready_client.get("/orders?view=closed").text, "list")
    assert '<span class="badge tone-neutral">shipped</span>' in closed
    assert '<span class="badge tone-neutral">cancelled</span>' in closed


def test_orders_cards_open_and_closed(ready_client):
    open_pair(ready_client)
    closed_pair(ready_client)
    assert cards_of(ready_client.get("/orders").text) == [
        ("warn", 2, "Open orders"), ("ok", 1, "ready to ship"),
    ]
    assert cards_of(ready_client.get("/orders?view=closed").text) == [
        ("accent", 2, "Closed orders"), ("neutral", 1, "shipped"), ("neutral", 1, "cancelled"),
    ]
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    assert cards_of(ready_client.get("/orders").text) == [
        ("warn", 2, "Offene Bestellungen"), ("ok", 1, "versandbereit"),
    ]
    assert cards_of(ready_client.get("/orders?view=closed").text)[0] == ("accent", 2, "Abgeschlossene Bestellungen")


def test_orders_cards_skip_states_without_entries(ready_client):
    place(ready_client, article(ready_client), 1)
    assert cards_of(ready_client.get("/orders").text) == [("warn", 1, "Open orders")]
    assert cards_of(ready_client.get("/orders?view=closed").text) == [("accent", 0, "Closed orders")]
    assert '<p class="summary">' not in ready_client.get("/orders").text


def test_order_filter_shows_only_that_state_and_the_counts_stay(ready_client):
    open_pair(ready_client)
    page = ready_client.get("/orders?status=ready").text
    listed = region(page, "list")
    assert "Order 1002" in listed and "Order 1001" not in listed
    assert '<a class="filter active" href="/orders?status=ready" aria-current="true">' in page
    assert 'All <span class="count">2</span>' in page
    assert cards_of(page) == [("warn", 2, "Open orders"), ("ok", 1, "ready to ship")]  # not narrowed


def test_closed_view_filters_shipped_and_cancelled(ready_client):
    closed_pair(ready_client)
    page = ready_client.get("/orders?view=closed&status=cancelled").text
    listed = region(page, "list")
    assert "Order 2002" in listed and "Order 2001" not in listed
    filters = region(page, "filters")
    assert "shipped" in filters and "cancelled" in filters and "ready to ship" not in filters
    other_view = ready_client.get("/orders?status=shipped")  # not a state of the open view: ignored
    assert other_view.status_code == 200 and en("orders.empty_list") in region(other_view.text, "list")


def test_closed_view_sees_every_closed_order(ready_client):
    """Search and summary cover all closed orders, not only the newest hundred."""
    a = article(ready_client, stock_=0)
    with sm_of(ready_client).begin() as s:
        for n in range(1, 102):  # Order 0001 is closed first, so it is the oldest
            order = orders.create_order(
                s, reference=f"Order {n:04d}", order_date=date(2026, 1, 15),
                items=[ArticleItem(a, 1)], actor="user",
            )
            orders.cancel_order(s, order.id, "user")
    page = ready_client.get("/orders?view=closed").text
    assert cards_of(page) == [("accent", 101, "Closed orders"), ("neutral", 101, "cancelled")]
    found = region(ready_client.get("/orders?view=closed&q=0001").text, "list")
    assert "Order 0001" in found and "Order 0101" not in found


def test_orders_search_by_reference(ready_client):
    open_pair(ready_client)
    page = ready_client.get("/orders?q=1002").text
    listed = region(page, "list")
    assert "Order 1002" in listed and "Order 1001" not in listed
    assert 'value="1002"' in page
    assert f'placeholder="{en("orders.field.reference")}"' in page
    nothing = ready_client.get("/orders?q=%25")
    assert nothing.status_code == 200 and en("list.no_match") in region(nothing.text, "list")


def test_search_keeps_the_closed_view(ready_client):
    page = ready_client.get("/orders?view=closed").text
    search = re.search(r'<form class="search".*?</form>', page, re.S).group(0)
    assert '<input type="hidden" name="view" value="closed">' in search


def test_orders_both_views_and_tiles(ready_client):
    open_pair(ready_client)
    listed = region(ready_client.get("/orders").text, "list")
    assert 'class="collection layout-auto"' in listed
    assert "<table>" in listed and 'class="tiles"' in listed
    tiles = listed[listed.index('class="tiles"'):]
    assert '<a class="tile" href="/orders/' in tiles
    assert "Order 1001" in tiles and "Template A" in tiles and "tone-warn" in tiles


def test_orders_view_choice_is_its_own(ready_client):
    response = ready_client.get("/orders?view=closed&layout=tiles")
    assert response.status_code == 303 and response.headers["location"] == "/orders?view=closed"
    assert "rb_layout_orders=tiles" in response.headers["set-cookie"]
    open_pair(ready_client)
    assert 'class="collection layout-tiles"' in ready_client.get("/orders").text
    ready_client.cookies.delete("rb_layout_orders")
    ready_client.cookies.set("rb_layout_articles", "tiles")
    assert 'class="collection layout-auto"' in ready_client.get("/orders").text


def test_orders_toolbar_without_javascript(ready_client):
    page = ready_client.get("/orders").text
    assert '<a href="/orders?layout=list"' in page and '<a href="/orders?layout=tiles"' in page
    assert '<form class="search" method="get"' in page
    assert '<a class="btn" href="/orders/new">' in page
    assert '<a class="btn secondary" href="/orders?view=closed">' in page
    closed = ready_client.get("/orders?view=closed").text
    assert '<a class="btn secondary" href="/orders">' in closed


def test_list_regions_and_topics(ready_client):
    page = ready_client.get("/orders").text
    for name in ("summary", "filters", "list"):
        assert 'data-live-topics="order article"' in region_tag(page, name), name


def test_order_placed_elsewhere_reaches_the_list_region(ready_client):  # Design 8
    ready_client.get("/orders")
    place(ready_client, article(ready_client), 1, "Order 3001")
    found = regions_of(ready_client, "/orders", "list", "summary")
    assert "Order 3001" in found["list"] and cards_of(found["summary"]) == [("warn", 1, "Open orders")]


def test_stock_elsewhere_makes_the_order_ready_in_the_list(ready_client):  # topic "article"
    a = article(ready_client)
    place(ready_client, a, 1)
    with sm_of(ready_client).begin() as s:
        stock.stock_in(s, a, 1, "user", consume_parts=False)
    found = regions_of(ready_client, "/orders", "list")
    assert '<span class="badge tone-ok">ready to ship</span>' in found["list"]


# ---- detail ----------------------------------------------------------------------------------


def test_order_detail_head_shows_date_and_badge(ready_client):
    o = place(ready_client, article(ready_client))
    page = ready_client.get(f"/orders/{o}").text
    head = re.search(r'<header class="page-head">.*?</header>', page, re.S).group(0)
    assert "Order 1001" in head and "2026" in text_of(head)
    assert '<span class="badge tone-warn">open</span>' in head
    assert head in region(page, "main")  # the badge follows the order


def test_order_detail_splits_data_and_forms(ready_client):
    o = place(ready_client, article(ready_client, stock_=5))
    page = ready_client.get(f"/orders/{o}").text
    assert f'data-live-topics="order:{o} article"' in region_tag(page, "main")
    assert f'data-live-topics="order:{o} article"' in region_tag(page, "actions")
    main, actions = region(page, "main"), region(page, "actions")
    assert "<form" not in main and "Template A" in main
    noted = [f for f in forms_in(page) if 'name="note"' in f]
    assert len(noted) == 2
    for form in noted:
        assert form in actions
        assert f'data-live-watch="order:{o}"' in form
        assert '<p class="live-stale" hidden role="status">' in form


def test_order_detail_regions_are_balanced(ready_client):  # like test_regions for the static pages
    page = ready_client.get(f"/orders/{place(ready_client, article(ready_client))}").text
    marks = re.findall(r"<!--(/?)live:([^>]*?)-->", page)
    assert marks == [("", "main"), ("/", "main"), ("", "actions"), ("/", "actions")]


def test_stock_elsewhere_offers_shipping_on_the_detail_page(ready_client):  # topic "article"
    a = article(ready_client)
    o = place(ready_client, a, 1)
    assert f'action="/orders/{o}/ship"' not in ready_client.get(f"/orders/{o}").text
    with sm_of(ready_client).begin() as s:
        stock.stock_in(s, a, 1, "user", consume_parts=False)
    found = regions_of(ready_client, f"/orders/{o}", "main", "actions")
    assert '<span class="badge tone-ok">ready to ship</span>' in found["main"]
    assert f'action="/orders/{o}/ship"' in found["actions"]


def test_order_detail_error_stays_outside_the_regions_and_on_top(ready_client):
    o = place(ready_client, article(ready_client, stock_=1), 2)
    page = ready_client.post(f"/orders/{o}/ship", data=with_tokens(ready_client, o)).text
    alert = f'<p class="alert" role="alert">{en("orders.not_ready")}</p>'
    assert alert in page
    assert alert not in region(page, "main") and alert not in region(page, "actions")
    # above the order: on a phone a long order would push it out of sight
    assert page.index(alert) < page.index("<!--live:main-->")


def test_failed_ship_page_has_no_region_source(ready_client):  # Review Focus 2
    o = place(ready_client, article(ready_client, stock_=1), 2)
    response = ready_client.post(f"/orders/{o}/ship", data=with_tokens(ready_client, o))
    assert response.status_code == 200 and en("orders.not_ready") in response.text
    assert 'data-live-src=""' in response.text


def test_cancelled_elsewhere_leaves_no_forms_in_actions(ready_client):  # Design 8
    o = place(ready_client, article(ready_client, stock_=5))
    assert "<form" in region(ready_client.get(f"/orders/{o}").text, "actions")
    cancel(ready_client, o)
    found = regions_of(ready_client, f"/orders/{o}", "actions", "main")
    assert "<form" not in found["actions"]
    assert '<span class="badge tone-neutral">cancelled</span>' in found["main"]


def test_cancel_confirmation_watches_the_order(ready_client):
    o = place(ready_client, article(ready_client, stock_=5))
    page = ready_client.post(f"/orders/{o}/cancel", data=with_tokens(ready_client, o)).text
    assert 'data-live-src=""' in page and "data-live-region" not in page
    form = forms_in(page)[-1]
    assert f'data-live-watch="order:{o}"' in form and 'name="confirm"' in form
    assert '<p class="live-stale" hidden role="status">' in form
    assert '<header class="page-head">' in page


# ---- new order -------------------------------------------------------------------------------


def test_new_order_form_is_a_region_without_watch(ready_client):
    page = ready_client.get("/orders/new").text
    assert 'data-live-topics="article set"' in region_tag(page, "form")
    assert 'action="/orders/new"' in region(page, "form")
    assert "data-live-watch" not in page


def test_new_order_form_offers_an_article_created_elsewhere(ready_client):
    ready_client.get("/orders/new")
    article(ready_client, name="Template Z")
    with sm_of(ready_client).begin() as s:
        a = make_article(s, name="Template Y").id
        b = make_article(s, name="Template X").id
        catalog.create_set(s, SetInput("Starter Set", None, [BaseInput(1, a), BaseInput(1, b)], []))
    found = regions_of(ready_client, "/orders/new", "form")
    assert "Template Z" in found["form"] and "Starter Set" in found["form"]


@pytest.mark.parametrize("path", ["/orders", "/orders?view=closed", "/orders/new"])
def test_order_pages_have_the_head(ready_client, path):
    page = ready_client.get(path).text
    head = re.search(r'<header class="page-head">.*?</header>', page, re.S).group(0)
    assert "#receipt" in head


# ---- head, subtitles, row actions --------------------------------------------------------------


def head_of(page):
    return re.search(r'<header class="page-head">.*?</header>', page, re.S).group(0)


def test_orders_head_has_subtitle_and_new_button(ready_client):
    page = ready_client.get("/orders").text
    head = head_of(page)
    assert f'<p class="page-sub">{en("orders.subtitle")}</p>' in head
    assert en("orders.subtitle") == "Orders reserve stock until they are shipped or cancelled"
    assert 'href="/orders/new"' in head[head.index('<div class="page-actions">') :]
    assert 'href="/orders/new"' not in page[page.index('<div class="toolbar">') : page.index("<!--live:list-->")]
    assert 'href="/orders?view=closed"' in page[page.index('<div class="toolbar">') :]
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    assert "Bestellungen reservieren Bestand, bis sie versandt oder storniert sind" in ready_client.get("/orders").text


def row_cell(page):
    block = region(page, "list")
    return block[block.index('<td class="actions-col">') :].split("</td>")[0]


def row_token(page, order_id):
    form = re.search(rf'<form class="icon-form"[^>]*action="/orders/{order_id}/cancel">(.*?)</form>', page, re.S).group(1)
    return re.search(r'name="form_token" value="([^"]+)"', form).group(1)


def test_open_row_has_view_and_cancel(ready_client):
    open_pair(ready_client)
    page = ready_client.get("/orders").text
    cell = row_cell(page)
    assert 'href="/orders/' in cell and f'title="{en("orders.view")}"' in cell
    assert '<form class="icon-form" method="post" action="/orders/' in cell and cell.count("/cancel") == 1
    assert 'name="csrf_token"' in cell and 'name="form_token"' in cell and 'name="confirm"' not in cell
    assert f'title="{en("orders.cancel")}"' in cell and "/ship" not in region(page, "list")
    assert f'<span class="sr-only">{en("list.col.actions")}</span>' in region(page, "list")
    assert region(page, "list").count("/cancel") == 2  # one per row; tiles have no actions
    assert en("orders.view") == "View"


def test_closed_row_has_view_only(ready_client):
    shipped, _cancelled = closed_pair(ready_client)
    page = ready_client.get("/orders?view=closed").text
    assert f'href="/orders/{shipped}"' in region(page, "list") and f'title="{en("orders.view")}"' in row_cell(page)
    assert "/cancel" not in region(page, "list")


def test_cancel_from_the_row_asks_first_then_cancels(ready_client):
    o = place(ready_client, article(ready_client, stock_=5))
    page = ready_client.get("/orders").text
    first = ready_client.post(
        f"/orders/{o}/cancel", data={"csrf_token": csrf_of(ready_client, "/orders"), "form_token": row_token(page, o)}
    )
    assert first.status_code == 200 and en("orders.cancel_yes") in first.text  # the confirmation page
    assert en("orders.cancel_confirm", reference="Order 1001") in first.text
    with sm_of(ready_client).begin() as s:
        assert orders.get_order(s, o).status == "open"
    passed = re.search(r'name="form_token" value="([^"]+)"', forms_in(first.text)[-1]).group(1)
    assert passed == row_token(page, o)
    second = ready_client.post(
        f"/orders/{o}/cancel",
        data={"csrf_token": csrf_of(ready_client, "/orders"), "form_token": passed, "confirm": "yes"},
    )
    assert second.status_code == 303
    with sm_of(ready_client).begin() as s:
        assert orders.get_order(s, o).status == "cancelled"


def test_list_regions_carry_cards_and_fresh_form_tokens(ready_client):
    o = place(ready_client, article(ready_client, stock_=5))
    response = ready_client.get("/orders", headers={regions.HEADER: "summary list"})
    assert 'data-live-region="summary"' in response.text and 'class="kpis"' in response.text
    token = row_token(response.text, o)
    asked = ready_client.post(
        f"/orders/{o}/cancel", data={"csrf_token": csrf_of(ready_client, "/orders"), "form_token": token}
    )
    assert asked.status_code == 200 and en("orders.cancel_yes") in asked.text  # the refetched token is valid


def test_detail_new_and_cancel_heads_have_subtitles(ready_client):
    o = place(ready_client, article(ready_client, stock_=5))
    detail = head_of(ready_client.get(f"/orders/{o}").text)
    sub = re.search(r'<p class="page-sub">(.*?)</p>', detail, re.S).group(1)
    assert "2026" in sub and '<span class="badge tone-' in sub
    assert '<div class="page-actions">' not in detail
    new = head_of(ready_client.get("/orders/new").text)
    assert f'<p class="page-sub">{en("orders.new_subtitle")}</p>' in new
    assert en("orders.new_subtitle") == "Record an order; its stock is reserved"
    asked = ready_client.post(f"/orders/{o}/cancel", data=with_tokens(ready_client, o)).text
    assert f'<p class="page-sub">{en("orders.cancel_subtitle")}</p>' in head_of(asked)
    assert en("orders.cancel_subtitle") == "Confirm the cancellation; the reservation is released"


def test_no_order_template_uses_the_old_summary():
    from pathlib import Path
    root = Path(regions.__file__).parent / "templates"
    for name in ("orders_list.html", "order_detail.html", "order_new.html", "order_cancel.html"):
        assert "ui.summary" not in (root / name).read_text(encoding="utf-8"), name
