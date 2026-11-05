import re
from datetime import date

from sqlalchemy import select

from conftest import csrf_of, form_token_of, sm_of
from factories import make_article
from reprintbuddy import settings as st
from reprintbuddy.domain import catalog, orders, stock
from reprintbuddy.domain.catalog import PositionInput, SetInput
from reprintbuddy.domain.orders import ArticleItem
from reprintbuddy.models import Article, JournalEntry, Order
from reprintbuddy.web.formtoken import issue_form_token
from reprintbuddy.web.i18n import translate


def en(key, **params):
    return translate("en", key, **params)


def order_fields(client, **over):
    data = {
        "csrf_token": csrf_of(client, "/orders/new"),
        "form_token": form_token_of(client, "/orders/new", "/orders/new"),
        "reference": "Order 1001", "order_date": "2026-01-15", "note": "", "rows": "5",
    }
    data.update(over)
    return data


def one_article(client, **fields):
    with sm_of(client).begin() as s:
        return make_article(s, name="Template A", **fields).id


def order_in_db(client):
    with sm_of(client).begin() as s:
        return list(s.scalars(select(Order)))


def reserved(client, article_id):
    with sm_of(client).begin() as s:
        return s.get(Article, article_id).reserved


def place(client, article_id, qty=2, reference="Order 1001"):
    with sm_of(client).begin() as s:
        return orders.create_order(
            s, reference=reference, order_date=date(2026, 1, 15),
            items=[ArticleItem(article_id, qty)], actor="user",
        ).id


def test_orders_empty(ready_client):
    page = ready_client.get("/orders")
    assert page.status_code == 200 and en("orders.empty_list") in page.text


def test_create_order_articles(ready_client):  # Spec 6.2
    a = one_article(ready_client)
    response = ready_client.post("/orders/new", data=order_fields(ready_client, item_0=f"article:{a}", qty_0="2"))
    created = order_in_db(ready_client)
    assert response.status_code == 303 and response.headers["location"] == f"/orders/{created[0].id}"
    assert reserved(ready_client, a) == 2
    detail = ready_client.get(f"/orders/{created[0].id}").text
    assert "Order 1001" in detail and "Template A" in detail and "2" in detail
    assert en("orders.created", reference="Order 1001") in detail


def test_create_order_set_choice_two_steps(ready_client):  # Spec 5.4
    with sm_of(ready_client).begin() as s:
        a = make_article(s, name="Template A").id
        b = make_article(s, name="Template B").id
        c = make_article(s, name="Template C").id
        set_id = catalog.create_set(
            s, SetInput("Starter Set", None, [PositionInput(1, [a]), PositionInput(1, [b, c])])
        ).id
        position = catalog.list_sets(s)[0].positions[1].id
    data = order_fields(ready_client, item_0=f"set:{set_id}", qty_0="1")
    first = ready_client.post("/orders/new", data=data)
    assert first.status_code == 200
    assert f'name="choice_0_{position}"' in first.text and en("orders.choose") in first.text
    assert reserved(ready_client, c) == 0 and order_in_db(ready_client) == []
    assert 'value="Order 1001"' in first.text  # input stays
    data[f"choice_0_{position}"] = str(c)
    second = ready_client.post("/orders/new", data=data)  # the same one-time token still works
    assert second.status_code == 303
    assert reserved(ready_client, c) == 1 and reserved(ready_client, a) == 1 and reserved(ready_client, b) == 0


def test_choice_not_allowed_rejected(ready_client):
    with sm_of(ready_client).begin() as s:
        a = make_article(s, name="Template A").id
        b = make_article(s, name="Template B").id
        c = make_article(s, name="Template C").id
        set_id = catalog.create_set(s, SetInput("Starter Set", None, [PositionInput(1, [b, c])])).id
        position = catalog.list_sets(s)[0].positions[0].id
    data = order_fields(ready_client, item_0=f"set:{set_id}", qty_0="1", **{f"choice_0_{position}": str(a)})
    response = ready_client.post("/orders/new", data=data)
    assert response.status_code == 200 and order_in_db(ready_client) == []


def test_more_rows_keeps_values(ready_client):
    a = one_article(ready_client)
    response = ready_client.post("/orders/new", data=order_fields(ready_client, item_0=f"article:{a}", qty_0="2", action="more"))
    assert response.status_code == 200
    assert 'name="qty_9"' in response.text and 'name="qty_10"' not in response.text
    assert 'value="Order 1001"' in response.text and 'value="2"' in response.text
    assert order_in_db(ready_client) == []


def test_order_form_defaults_to_today(ready_client):
    page = ready_client.get("/orders/new").text
    assert re.search(r'type="date"[^>]*value="\d{4}-\d{2}-\d{2}"|value="\d{4}-\d{2}-\d{2}"[^>]*type="date"', page)


def test_order_invalid_input(ready_client):
    a = one_article(ready_client)
    cases = [
        {"qty_0": "abc"}, {"qty_0": "0"}, {"qty_0": ""}, {"qty_0": "1.5"}, {"qty_0": "9" * 20},
        {"reference": "  "}, {"order_date": "not a date"}, {"order_date": ""}, {"item_0": "bogus:1"},
        {"item_0": "article:abc"}, {"item_0": "article:99999999999999999999"}, {"item_0": "article:9999"},
        {"reference": "x" * 101},
    ]
    for over in cases:
        data = order_fields(ready_client, item_0=f"article:{a}", qty_0="1")
        data.update(over)
        response = ready_client.post("/orders/new", data=data)
        assert response.status_code == 200, over
    assert order_in_db(ready_client) == [] and reserved(ready_client, a) == 0


def test_order_without_items(ready_client):
    response = ready_client.post("/orders/new", data=order_fields(ready_client))
    assert response.status_code == 200 and en("orders.empty") in response.text


def test_order_double_submit(ready_client):
    a = one_article(ready_client)
    data = order_fields(ready_client, item_0=f"article:{a}", qty_0="2")
    assert ready_client.post("/orders/new", data=data).status_code == 303
    assert ready_client.post("/orders/new", data=data).status_code == 303
    assert len(order_in_db(ready_client)) == 1 and reserved(ready_client, a) == 2


def test_ship_button_only_when_ready(ready_client):  # Spec 6.7
    a = one_article(ready_client)
    o = place(ready_client, a, 2)
    page = ready_client.get(f"/orders/{o}").text
    assert f'action="/orders/{o}/ship"' not in page and f'action="/orders/{o}/cancel"' in page
    assert en("orders.not_ready_badge") in page
    with sm_of(ready_client).begin() as s:
        stock.stock_in(s, a, 2, "user", consume_parts=False)
    page = ready_client.get(f"/orders/{o}").text
    assert f'action="/orders/{o}/ship"' in page and en("orders.ready") in page


def test_ship_books_and_flashes(ready_client):
    a = one_article(ready_client, stock=5)
    o = place(ready_client, a, 2)
    token = form_token_of(ready_client, f"/orders/{o}", f"/orders/{o}/ship")
    response = ready_client.post(f"/orders/{o}/ship", data={"csrf_token": csrf_of(ready_client, f"/orders/{o}"), "form_token": token})
    assert response.status_code == 303 and response.headers["location"] == f"/orders/{o}"
    with sm_of(ready_client).begin() as s:
        art = s.get(Article, a)
        assert (art.stock, art.reserved) == (3, 0) and s.get(Order, o).status == "shipped"
    assert en("orders.shipped", reference="Order 1001") in ready_client.get(f"/orders/{o}").text


def test_ship_double_click_books_once(ready_client):  # Review Focus 1
    a = one_article(ready_client, stock=5)
    o = place(ready_client, a, 2)
    data = {"csrf_token": csrf_of(ready_client, f"/orders/{o}"), "form_token": form_token_of(ready_client, f"/orders/{o}", f"/orders/{o}/ship")}
    first = ready_client.post(f"/orders/{o}/ship", data=data)
    second = ready_client.post(f"/orders/{o}/ship", data=data)
    assert first.status_code == 303 and second.status_code == 303
    with sm_of(ready_client).begin() as s:
        assert s.get(Article, a).stock == 3
        assert len(s.scalars(select(JournalEntry).where(JournalEntry.kind == "order_shipped")).all()) == 1
    page = ready_client.get(f"/orders/{o}").text
    assert en("form.duplicate") in page and en("orders.not_open") not in page


def test_ship_not_ready_post_rejected(ready_client):
    a = one_article(ready_client, stock=1)
    o = place(ready_client, a, 2)
    data = {"csrf_token": csrf_of(ready_client, f"/orders/{o}"), "form_token": form_token_of(ready_client, f"/orders/{o}", f"/orders/{o}/cancel")}
    response = ready_client.post(f"/orders/{o}/ship", data=data)
    assert response.status_code == 200 and en("orders.not_ready") in response.text
    with sm_of(ready_client).begin() as s:
        art = s.get(Article, a)
        assert (art.stock, art.reserved) == (1, 2) and s.get(Order, o).status == "open"


def test_cancel_releases(ready_client):
    a = one_article(ready_client, stock=5)
    o = place(ready_client, a, 2)
    data = {"csrf_token": csrf_of(ready_client, f"/orders/{o}"), "form_token": form_token_of(ready_client, f"/orders/{o}", f"/orders/{o}/cancel")}
    assert ready_client.post(f"/orders/{o}/cancel", data=data).status_code == 303
    assert reserved(ready_client, a) == 0
    assert en("orders.empty_list") in ready_client.get("/orders").text  # no longer open
    closed = ready_client.get("/orders?view=closed").text
    assert "Order 1001" in closed and en("orders.status.cancelled") in closed


def test_closed_order_has_no_buttons(ready_client):
    a = one_article(ready_client, stock=5)
    o = place(ready_client, a, 2)
    data = {"csrf_token": csrf_of(ready_client, f"/orders/{o}"), "form_token": form_token_of(ready_client, f"/orders/{o}", f"/orders/{o}/cancel")}
    ready_client.post(f"/orders/{o}/cancel", data=data)
    page = ready_client.get(f"/orders/{o}").text
    assert f"/orders/{o}/ship" not in page and f"/orders/{o}/cancel" not in page
    forced = {"csrf_token": csrf_of(ready_client, f"/orders/{o}"), "form_token": issue_form_token(ready_client.app.state.session_secret)}
    response = ready_client.post(f"/orders/{o}/ship", data=forced)
    assert response.status_code == 200 and en("orders.not_open") in response.text


def test_unknown_order(ready_client):
    assert ready_client.get("/orders/999").status_code == 404
    data = {"csrf_token": csrf_of(ready_client, "/orders"), "form_token": issue_form_token(ready_client.app.state.session_secret)}
    assert ready_client.post("/orders/999/ship", data=data).status_code == 404


def test_open_list_shows_ready_badge_and_set_group(ready_client):
    with sm_of(ready_client).begin() as s:
        a = make_article(s, name="Template A", stock=1).id
        set_id = catalog.create_set(s, SetInput("Starter Set", None, [PositionInput(1, [a])])).id
        orders.create_order(s, reference="Order 1001", order_date=date(2026, 1, 15), items=[orders.SetItem(set_id, 1, {})], actor="user")
    page = ready_client.get("/orders").text
    assert "Order 1001" in page and en("orders.ready") in page and "Starter Set" in page
    assert "view=closed" in page


def test_orders_require_csrf(ready_client):
    a = one_article(ready_client)
    o = place(ready_client, a)
    data = order_fields(ready_client, item_0=f"article:{a}", qty_0="1")
    del data["csrf_token"]
    assert ready_client.post("/orders/new", data=data).status_code == 403
    assert ready_client.post(f"/orders/{o}/ship", data={"form_token": "x"}).status_code == 403
    assert ready_client.post(f"/orders/{o}/cancel", data={"form_token": "x"}).status_code == 403


def test_orders_german(ready_client):
    a = one_article(ready_client, stock=5)
    o = place(ready_client, a, 1)
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    listing = ready_client.get("/orders").text
    assert "Bestellungen" in listing and "versandbereit" in listing
    detail = ready_client.get(f"/orders/{o}").text
    assert "Versand" in detail and "Storno" in detail
    assert "Bestell-Referenz" in ready_client.get("/orders/new").text or "Referenz" in ready_client.get("/orders/new").text


def test_nav_orders_active(ready_client):
    assert 'class="nav-link active" href="/orders"' in ready_client.get("/orders").text
