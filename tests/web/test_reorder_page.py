"""The reorder page: suggestions, "ordered" marks, "under way" and "arrived" (Spec 9.3, 9.4; B6 Task 10)."""

import re

import pytest

from reprintbuddy import settings as st
from conftest import csrf_of, form_token_of, sm_of
from factories import make_article, make_filament_settings, make_part, make_sort
from reprintbuddy.models import ArticlePart, ReorderMark


def add_part(client, **fields):
    """Insert M4 (pack 50), needed twice per unit by Template A: a suggestion of 1 x 50."""
    values = dict(
        stock=10, pack_size=50, min_order_packs=1, lead_time_days=0, safety_value=20,
        safety_unit="qty", supplier="Example Supplier", order_url="https://shop.example/insert-m4",
    )
    values.update(fields)
    with sm_of(client).begin() as s:
        part = make_part(s, **values)
        art = make_article(s, needs_assembly=True, stock=0, min_stock=3, buffer=0)
        s.add(ArticlePart(article_id=art.id, part_id=part.id, qty_per_unit=2))
        s.flush()
        return part.id


def add_filament(client, fake):
    """A black PLA sort with 100 g and a safety cushion of 2000 g: 2 x 1000 g."""
    fake.add_spool("PLA", color_hex="#000000", color_name="Black", remaining_g=100.0)
    with sm_of(client).begin() as s:
        sort = make_sort(s)
        make_filament_settings(
            s, sort.id, pack_grams=1000, min_order_packs=1, safety_value=2000, safety_unit="grams",
            supplier="Example Supplier", order_url="https://shop.example/pla-black",
        )
        return sort.id


def mark_form(client, item_type, item_id, packs):
    token = form_token_of(client, "/reorder", "/reorder/mark")
    return client.post("/reorder/mark", data={
        "csrf_token": csrf_of(client, "/reorder"), "form_token": token, "item_type": item_type,
        "item_id": str(item_id), "packs": packs,
    })


def marks(client):
    with sm_of(client).begin() as s:
        return s.query(ReorderMark).all()


def test_reorder_lists_part_and_filament(bb_client, fake_bambuddy):  # Spec 9.4
    add_part(bb_client)
    add_filament(bb_client, fake_bambuddy)
    html = bb_client.get("/reorder").text
    assert "Insert M4" in html and "1 × 50 (50 pcs)" in html
    assert "Example Brand PLA Black" in html and "2 × 1000 g" in html
    assert html.count("Example Supplier") >= 2
    for url in ("https://shop.example/insert-m4", "https://shop.example/pla-black"):
        assert f'href="{url}" target="_blank" rel="noopener noreferrer"' in html
    assert html.count('action="/reorder/mark"') == 2
    assert re.search(r'name="packs"[^>]*value="2"', html)


def test_mark_hides_and_lists_under_way(bb_client):
    pid = add_part(bb_client)
    response = mark_form(bb_client, "part", pid, "1")
    assert response.status_code == 303 and response.headers["location"] == "/reorder"
    html = bb_client.get("/reorder").text
    assert 'action="/reorder/mark"' not in html  # the suggestion is gone
    assert "On its way" in html and "Insert M4" in html and "1 × 50" in html
    assert re.search(r'action="/reorder/marks/\d+/received"', html)


def test_mark_double_submit(bb_client):  # Review Focus 4
    pid = add_part(bb_client)
    token = form_token_of(bb_client, "/reorder", "/reorder/mark")
    data = {"csrf_token": csrf_of(bb_client, "/reorder"), "form_token": token, "item_type": "part",
            "item_id": str(pid), "packs": "1"}
    first = bb_client.post("/reorder/mark", data=data)
    second = bb_client.post("/reorder/mark", data=data)
    assert first.status_code == 303 and second.status_code == 303
    assert len(marks(bb_client)) == 1
    assert "already" in bb_client.get("/reorder").text.lower()


def test_received_button(bb_client):
    pid = add_part(bb_client)
    mark_form(bb_client, "part", pid, "1")
    token = form_token_of(bb_client, "/reorder")
    html = bb_client.get("/reorder").text
    action = re.search(r'action="(/reorder/marks/\d+/received)"', html).group(1)
    token = form_token_of(bb_client, "/reorder", action)
    response = bb_client.post(action, data={"csrf_token": csrf_of(bb_client, "/reorder"), "form_token": token})
    assert response.status_code == 303 and response.headers["location"] == "/reorder"
    assert marks(bb_client)[0].received_at is not None
    assert "On its way" not in bb_client.get("/reorder").text


@pytest.mark.parametrize("bad", ["0", "abc", "9" * 20, "", "1001"])
def test_mark_invalid_packs(bb_client, bad):
    pid = add_part(bb_client)
    response = mark_form(bb_client, "part", pid, bad)
    assert response.status_code == 200 and "Enter a number of order units" in response.text
    assert marks(bb_client) == []
    assert 'action="/reorder/mark"' in response.text  # the form is shown again


def test_mark_unknown_item(bb_client):
    add_part(bb_client)  # so that the page has a form to take the token from
    response = mark_form(bb_client, "part", 9999, "1")
    assert response.status_code == 200 and "cannot be ordered" in response.text
    response = mark_form(bb_client, "bogus", "x", "1")
    assert response.status_code == 200 and marks(bb_client) == []


def test_received_unknown_mark(bb_client):
    add_part(bb_client)
    token = form_token_of(bb_client, "/reorder", "/reorder/mark")
    response = bb_client.post(
        "/reorder/marks/9999/received", data={"csrf_token": csrf_of(bb_client, "/reorder"), "form_token": token}
    )
    assert response.status_code == 200 and "does not exist" in response.text


def test_reorder_without_any_filament_stock(bb_client, fake_bambuddy):  # Review Focus 2
    add_part(bb_client)
    with sm_of(bb_client).begin() as s:
        sort = make_sort(s)
        make_filament_settings(s, sort.id, safety_value=2000, safety_unit="grams")
    fake_bambuddy.unavailable = True
    response = bb_client.get("/reorder")
    assert response.status_code == 200
    assert "Insert M4" in response.text and "Example Brand PLA Black" not in response.text
    assert "filament stock is not known" in response.text.lower()


def test_reorder_empty(bb_client):
    assert "Nothing to order" in bb_client.get("/reorder").text


def test_reorder_german(bb_client):
    pid = add_part(bb_client, name="Hex key 2")
    add_part(bb_client)  # stays a suggestion, so the button "mark as ordered" is on the page
    with sm_of(bb_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    mark_form(bb_client, "part", pid, "1")
    html = bb_client.get("/reorder").text
    for word in ("Bestellvorschläge", "als bestellt markieren", "Unterwegs", "eingetroffen"):
        assert word in html


def test_nav_reorder_active(bb_client):
    html = bb_client.get("/reorder").text
    active = re.findall(r'class="nav-link active"[^>]*>.*?<span class="nav-label">(.*?)</span>', html, re.S)
    assert set(active) == {"Reorder"}
    assert 'href="/reorder"' in html
