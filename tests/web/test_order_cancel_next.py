"""Cancelling an order: from the list it returns to the list, from the detail page it stays
on the detail page; `next` only ever accepts a local path."""

import re
from datetime import date

import pytest
from conftest import csrf_of, form_token_of, sm_of
from factories import make_article

from reprintbuddy.domain import orders
from reprintbuddy.domain.orders import ArticleItem
from reprintbuddy.web.i18n import translate


def en(key, **params):
    return translate("en", key, **params)


def place(client):
    with sm_of(client).begin() as s:
        article = make_article(s, name="Template A", stock=5)
        return orders.create_order(
            s, reference="Order 3001", order_date=date(2026, 1, 15),
            items=[ArticleItem(article.id, 1)], actor="user",
        ).id


def status_of(client, order_id):
    with sm_of(client).begin() as s:
        return orders.get_order(s, order_id).status


def list_form(client, order_id):
    """The cancel form of the list row: (csrf, token, extra hidden fields)."""
    page = client.get("/orders").text
    form = re.search(rf'<form class="icon-form"[^>]*action="/orders/{order_id}/cancel">(.*?)</form>', page, re.S).group(1)
    data = {name: value for name, value in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form)}
    return data


def detail_data(client, order_id):
    return {"csrf_token": csrf_of(client, f"/orders/{order_id}"),
            "form_token": form_token_of(client, f"/orders/{order_id}", f"/orders/{order_id}/cancel")}


def test_cancel_from_list_returns_to_list(ready_client):
    o = place(ready_client)
    data = list_form(ready_client, o)
    assert data["next"] == "/orders"
    first = ready_client.post(f"/orders/{o}/cancel", data=data)
    assert first.status_code == 200
    assert 'name="next" value="/orders"' in first.text
    assert f'<a class="btn-link" href="/orders">{en("orders.cancel_keep")}</a>' in first.text
    assert status_of(ready_client, o) == "open"
    passed = {name: value for name, value in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', first.text)}
    assert passed["next"] == "/orders" and passed["confirm"] == "yes"
    second = ready_client.post(f"/orders/{o}/cancel", data=passed)
    assert second.status_code == 303 and second.headers["location"] == "/orders"
    assert status_of(ready_client, o) == "cancelled"
    assert en("orders.cancelled", reference="Order 3001") in ready_client.get("/orders").text


def test_cancel_from_detail_stays_on_detail(ready_client):
    o = place(ready_client)
    first = ready_client.post(f"/orders/{o}/cancel", data=detail_data(ready_client, o))
    assert first.status_code == 200 and 'name="next"' not in first.text
    assert f'<a class="btn-link" href="/orders/{o}">' in first.text
    passed = {name: value for name, value in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', first.text)}
    second = ready_client.post(f"/orders/{o}/cancel", data=passed)
    assert second.status_code == 303 and second.headers["location"] == f"/orders/{o}"
    assert status_of(ready_client, o) == "cancelled"


@pytest.mark.parametrize("bad", ["//evil.example", "https://evil.example", "/" + chr(92) + "evil"])
def test_cancel_next_must_be_local(ready_client, bad):
    o = place(ready_client)
    data = {**detail_data(ready_client, o), "next": bad}
    first = ready_client.post(f"/orders/{o}/cancel", data=data)
    assert first.status_code == 200 and "evil" not in first.text
    assert f'<a class="btn-link" href="/orders/{o}">' in first.text
    second = ready_client.post(f"/orders/{o}/cancel", data={**data, "confirm": "yes"})
    assert second.status_code == 303 and second.headers["location"] == f"/orders/{o}"
    assert status_of(ready_client, o) == "cancelled"


def test_cancel_used_token_follows_next(ready_client):
    o = place(ready_client)
    data = list_form(ready_client, o)
    done = ready_client.post(f"/orders/{o}/cancel", data={**data, "confirm": "yes"})
    assert done.status_code == 303 and done.headers["location"] == "/orders"
    again = ready_client.post(f"/orders/{o}/cancel", data={**data, "confirm": "yes"})
    assert again.status_code == 303 and again.headers["location"] == "/orders"


def test_icon_post_extra_fields_escaped(ready_client):
    env = ready_client.app.state.templates.env
    head = '{% import "_ui.html" as ui with context %}'
    values = {"lang": "en", "csrf_token": "CSRF"}
    html = env.from_string(head + """{{ ui.icon_post('x', 'L', '/a', fields=[("next", '/o?a=1&b="2"')]) }}""").render(**values)
    assert '<input type="hidden" name="next" value="/o?a=1&amp;b=&#34;2&#34;">' in html
    plain = env.from_string(head + "{{ ui.icon_post('x', 'L', '/a') }}").render(**values)
    assert plain.count("<input") == 2
