"""Page "Montage" (Spec 8.3, 10.3): articles whose units wait for assembly, and the booking."""

import html
import re

import pytest
from sqlalchemy import select

from conftest import csrf_of, form_token_of, sm_of
from factories import make_article, make_part
from reprintbuddy import settings as st
from reprintbuddy.domain import stock
from reprintbuddy.models import Article, ArticlePart, JournalEntry, Part
from reprintbuddy.web.i18n import translate


def en(key, **params):
    return translate("en", key, **params)


def waiting(client, awaiting=2, *, stock_=0, parts=(("Insert M4", 2, 5),), name="Template A"):
    """An article with `awaiting` units waiting for assembly; `parts` are (name, qty_per_unit, stock)."""
    with sm_of(client).begin() as s:
        a = make_article(s, name=name, stock=stock_)
        for part_name, qty, part_stock in parts:
            p = make_part(s, name=part_name, stock=part_stock)
            s.add(ArticlePart(article_id=a.id, part_id=p.id, qty_per_unit=qty))
        s.flush()
        if awaiting:
            stock.count_article(s, a.id, "awaiting_assembly", awaiting, "user")
        return a.id


def post(client, article_id, **fields):
    data = {
        "csrf_token": csrf_of(client, "/assembly"),
        "form_token": form_token_of(client, "/assembly", f"/assembly/{article_id}"),
    }
    data.update(fields)
    return client.post(f"/assembly/{article_id}", data=data), data


def number(client, model, ident, field):
    with sm_of(client).begin() as s:
        return getattr(s.get(model, ident), field)


def test_assembly_empty(ready_client):
    page = ready_client.get("/assembly")
    assert page.status_code == 200
    assert en("assembly.empty") in page.text


def test_assembly_without_bambuddy(ready_client):  # the Bambuddy factory of ready_client gives None
    assert ready_client.get("/assembly").status_code == 200


def test_nav_assembly_active(ready_client):
    page = ready_client.get("/assembly").text
    link = re.search(r'<a[^>]*href="/assembly"[^>]*>', page)
    assert link, "no nav link to /assembly"
    assert "active" in link.group(0) or 'aria-current' in link.group(0)


def test_assembly_lists_parts(ready_client):
    waiting(ready_client, 2, parts=(("Insert M4", 2, 3), ("Hex key 2", 1, 9)))
    page = ready_client.get("/assembly").text
    assert "Template A" in page and en("articles.col.awaiting_assembly") in page
    assert en("assembly.part_line", qty=2, stock=3) in page
    # Insert M4: 3 < 2 * 2 -> red; Hex key 2: 9 >= 1 * 2 -> not red
    assert re.search(r'class="neg"[^>]*>[^<]*Insert M4', page)
    assert not re.search(r'class="neg"[^>]*>[^<]*Hex key 2', page)
    assert 'inputmode="numeric"' in page and 'value="2"' in page


def test_assemble_books(ready_client):  # Spec 8.3
    a = waiting(ready_client, 2, parts=(("Insert M4", 2, 5),))
    response, _ = post(ready_client, a, qty="2")
    assert response.status_code == 303 and response.headers["location"] == "/assembly"
    assert number(ready_client, Article, a, "stock") == 2
    assert number(ready_client, Article, a, "awaiting_assembly") == 0
    with sm_of(ready_client).begin() as s:
        assert s.scalars(select(Part.stock).where(Part.name == "Insert M4")).one() == 1
    page = ready_client.get("/assembly").text
    assert en("assembly.done", name="Template A", n=2) in page
    assert en("assembly.empty") in page


def test_assemble_short_parts_flash(ready_client):  # Spec 8.3
    a = waiting(ready_client, 2, parts=(("Insert M4", 2, 3),))
    response, _ = post(ready_client, a, qty="2")
    assert response.status_code == 303
    with sm_of(ready_client).begin() as s:
        assert s.scalars(select(Part.stock).where(Part.name == "Insert M4")).one() == 0
    assert en("stock.parts_short", part="Insert M4", had=3, needed=4) in ready_client.get("/assembly").text


def test_assemble_over_awaiting(ready_client):
    a = waiting(ready_client, 2)
    response, _ = post(ready_client, a, qty="3")
    assert response.status_code == 200
    assert en("stock.limit.awaiting_assembly", item="Template A") in response.text
    assert 'value="3"' in response.text  # typed value kept
    assert number(ready_client, Article, a, "awaiting_assembly") == 2


def test_assemble_unknown_article(ready_client):
    response, _ = post_unknown(ready_client)
    assert response.status_code == 200
    assert en("articles.not_found") in response.text


def post_unknown(client):
    a = waiting(client, 1)
    data = {"csrf_token": csrf_of(client, "/assembly"), "form_token": form_token_of(client, "/assembly", f"/assembly/{a}"),
            "qty": "1"}
    return client.post("/assembly/99999", data=data), data


def test_assemble_double_submit(ready_client):  # Review Focus 1
    a = waiting(ready_client, 4)
    response, data = post(ready_client, a, qty="1")
    again = ready_client.post(f"/assembly/{a}", data=data)
    assert response.status_code == 303 and again.status_code == 303
    assert number(ready_client, Article, a, "stock") == 1
    assert number(ready_client, Article, a, "awaiting_assembly") == 3
    with sm_of(ready_client).begin() as s:
        kinds = [e.kind for e in s.scalars(select(JournalEntry))]
    assert kinds.count("assembly") == 1
    assert en("form.duplicate") in ready_client.get("/assembly").text


@pytest.mark.parametrize("qty", ["", "abc", "0", "9" * 20])
def test_assemble_invalid_numbers(ready_client, qty):
    a = waiting(ready_client, 2)
    response, _ = post(ready_client, a, qty=qty)
    assert response.status_code == 200
    assert 'class="alert"' in response.text
    field = en("bookings.field.qty")
    expected = (en("form.required", field=field) if qty == ""
                else en("form.invalid_number", field=field, min=1, max=stock.MAX_QTY))
    assert html.escape(expected) in response.text
    assert number(ready_client, Article, a, "awaiting_assembly") == 2


def test_assembly_german(ready_client):
    waiting(ready_client, 2)
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    page = ready_client.get("/assembly").text
    assert "<h1>Montage</h1>" in page
    for text in ("Wartet auf Montage", "Montiert buchen", "Menge"):
        assert text in page


# ---- one dense line per article (p89; Spec 10.1) ----------------------------------------------------


def lines(page):
    return re.findall(r'<form[^>]*class="[^"]*assembly-line[^"]*"[^>]*>.*?</form>', page, re.S)


def test_one_line_per_article_with_quantity_and_button(ready_client):
    waiting(ready_client, 2, name="Template A")
    waiting(ready_client, 5, name="Template B", parts=())
    page = ready_client.get("/assembly").text
    rows = lines(page)
    assert len(rows) == 2 and "<table" not in page
    first = rows[0]
    assert "Template A" in first and "Template B" in rows[1]
    assert re.search(r'<span class="assembly-awaiting[^"]*">2</span>', first)
    qty = re.search(r'<input[^>]*name="qty"[^>]*>', first).group(0)
    assert 'value="2"' in qty and 'min="1"' in qty and 'max="2"' in qty and 'inputmode="numeric"' in qty
    assert en("assembly.book") in first
    assert 'name="note"' not in page  # no note field in the line
    assert re.search(r'class="line-head[ "]', page)  # the small column heads


def test_the_parts_stand_small_under_the_name(ready_client):
    waiting(ready_client, 2, parts=(("Insert M4", 2, 3),))
    (row,) = lines(ready_client.get("/assembly").text)
    assert en("assembly.part_line", qty=2, stock=3) in row and "Insert M4" in row


def test_assembly_filter_shows_one_article_with_a_chip(ready_client):
    a = waiting(ready_client, 2, name="Template A")
    b = waiting(ready_client, 1, name="Template B")
    page = ready_client.get(f"/assembly?article={a}").text
    rows = lines(page)
    assert len(rows) == 1 and "Template A" in rows[0]
    assert f'action="/assembly/{b}"' not in page
    chip = re.search(r'<a class="chip"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', page, re.S)
    assert chip and chip.group(1) == "/assembly" and "Template A" in chip.group(2)


@pytest.mark.parametrize("raw", ["abc", "", "0x1", "999999", "-1"])
def test_a_bad_or_unknown_filter_is_no_filter(ready_client, raw):
    waiting(ready_client, 2, name="Template A")
    waiting(ready_client, 1, name="Template B")
    page = ready_client.get("/assembly", params={"article": raw}).text
    assert len(lines(page)) == 2 and 'class="chip"' not in page


def test_filter_on_an_article_with_nothing_waiting(ready_client):
    waiting(ready_client, 2, name="Template A")
    other = waiting(ready_client, 0, name="Template B")
    page = ready_client.get(f"/assembly?article={other}").text
    assert lines(page) == [] and en("assembly.empty_article") in page and 'class="chip"' in page


def test_booking_in_the_filtered_list_returns_to_it(ready_client):
    a = waiting(ready_client, 2, name="Template A")
    waiting(ready_client, 1, name="Template B")
    data = {
        "csrf_token": csrf_of(ready_client, "/assembly"),
        "form_token": form_token_of(ready_client, f"/assembly?article={a}", f"/assembly/{a}"),
        "qty": "1", "article": str(a),
    }
    response = ready_client.post(f"/assembly/{a}", data=data)
    assert response.status_code == 303 and response.headers["location"] == f"/assembly?article={a}"
    assert number(ready_client, Article, a, "awaiting_assembly") == 1


def test_failed_booking_in_the_filtered_list_keeps_filter_and_typed_value(ready_client):
    a = waiting(ready_client, 2, name="Template A")
    b = waiting(ready_client, 1, name="Template B")
    data = {
        "csrf_token": csrf_of(ready_client, "/assembly"),
        "form_token": form_token_of(ready_client, f"/assembly?article={a}", f"/assembly/{a}"),
        "qty": "9", "article": str(a),
    }
    response = ready_client.post(f"/assembly/{a}", data=data)
    assert response.status_code == 200
    assert len(lines(response.text)) == 1 and 'class="chip"' in response.text
    assert 'value="9"' in response.text and f'action="/assembly/{b}"' not in response.text
