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


def post(client, action, **fields):
    """Post one of the booking forms with a fresh CSRF and one-time token."""
    data = {
        "csrf_token": csrf_of(client, "/bookings"),
        "form_token": form_token_of(client, "/bookings", f"/bookings/{action}"),
    }
    data.update(fields)
    return client.post(f"/bookings/{action}", data=data), data


def article(client, **fields):
    with sm_of(client).begin() as s:
        return make_article(s, name="Template A", **fields).id


def part(client, **fields):
    with sm_of(client).begin() as s:
        return make_part(s, name="Insert M4", **fields).id


def number(client, model, ident, field):
    with sm_of(client).begin() as s:
        return getattr(s.get(model, ident), field)


def journal(client):
    with sm_of(client).begin() as s:
        return list(s.scalars(select(JournalEntry).order_by(JournalEntry.id)))


def test_initial_stock_by_inventory(ready_client):  # Spec 6.5
    a = article(ready_client)
    response, _ = post(ready_client, "inventory", article_id=a, field="stock", counted="5")
    assert response.status_code == 303 and response.headers["location"] == "/bookings"
    assert number(ready_client, Article, a, "stock") == 5
    rows = journal(ready_client)
    assert [(r.kind, r.qty) for r in rows] == [("inventory", 5)]


def test_inventory_correction_difference(ready_client):  # Spec 6.2
    a = article(ready_client, stock=5)
    post(ready_client, "inventory", article_id=a, field="stock", counted="3")
    assert en("bookings.inventory_done", item="Template A", counted=3, diff="-2") in ready_client.get("/bookings").text


def test_inventory_awaiting_assembly(ready_client):
    a = article(ready_client, awaiting_assembly=2)
    post(ready_client, "inventory", article_id=a, field="awaiting_assembly", counted="0")
    assert number(ready_client, Article, a, "awaiting_assembly") == 0


def test_inventory_other_field_rejected(ready_client):
    a = article(ready_client, reserved=2)
    for field in ("reserved", "provisional", "nonsense"):
        response, _ = post(ready_client, "inventory", article_id=a, field=field, counted="0")
        assert response.status_code == 200, field
    assert number(ready_client, Article, a, "reserved") == 2 and journal(ready_client) == []


def test_part_receipt(ready_client):
    p = part(ready_client, pack_size=50)
    response, _ = post(ready_client, "part-receipt", part_id=p, qty="50")
    assert response.status_code == 303
    assert number(ready_client, Part, p, "stock") == 50


def test_part_inventory(ready_client):
    p = part(ready_client, stock=50)
    post(ready_client, "part-inventory", part_id=p, counted="48", note="counted twice")
    assert number(ready_client, Part, p, "stock") == 48
    row = journal(ready_client)[0]
    assert (row.kind, row.qty, row.note) == ("inventory", -2, "counted twice")


def test_sale_without_order(ready_client):  # Spec 6.2, 6.4
    a = article(ready_client, stock=3, reserved=2)
    response, _ = post(ready_client, "sale", article_id=a, qty="2")
    assert response.status_code == 303
    assert number(ready_client, Article, a, "stock") == 1
    assert '<td class="num neg">-1</td>' in ready_client.get("/articles").text


def test_sale_over_stock_rejected(ready_client):  # Review Focus 2
    a = article(ready_client, stock=1)
    response, data = post(ready_client, "sale", article_id=a, qty="2")
    assert response.status_code == 200
    assert en("stock.limit.stock", item="Template A") in response.text
    assert number(ready_client, Article, a, "stock") == 1 and journal(ready_client) == []
    data["qty"] = "1"  # corrected, same token
    again = ready_client.post("/bookings/sale", data=data)
    assert again.status_code == 303 and number(ready_client, Article, a, "stock") == 0


def test_error_keeps_input_at_its_form(ready_client):
    a = article(ready_client, stock=1)
    response, _ = post(ready_client, "sale", article_id=a, qty="2", note="my note")
    assert 'value="2"' in response.text and "my note" in response.text


def test_stock_in_form_default_checked(ready_client):
    import re

    page = ready_client.get("/bookings").text
    tag = re.search(r'<input[^>]*name="consume_parts"[^>]*>', page).group(0)
    assert " checked" in tag


@pytest.mark.parametrize("checked,insert_after", [(True, 46), (False, 50)])
def test_stock_in_manual(ready_client, checked, insert_after):  # Spec 6.2, 6.6
    with sm_of(ready_client).begin() as s:
        a = make_article(s, name="Template A").id
        p = make_part(s, name="Insert M4", stock=50).id
        s.add(ArticlePart(article_id=a, part_id=p, qty_per_unit=2))
    extra = {"consume_parts": "on"} if checked else {}
    response, _ = post(ready_client, "stock-in", article_id=a, qty="2", **extra)
    assert response.status_code == 303
    assert number(ready_client, Article, a, "stock") == 2
    assert number(ready_client, Part, p, "stock") == insert_after


def test_stock_in_short_parts_warns(ready_client):  # rule 8.3
    with sm_of(ready_client).begin() as s:
        a = make_article(s, name="Template A").id
        p = make_part(s, name="Insert M4", stock=1).id
        s.add(ArticlePart(article_id=a, part_id=p, qty_per_unit=2))
    response, _ = post(ready_client, "stock-in", article_id=a, qty="2", consume_parts="on")
    assert response.status_code == 303
    assert number(ready_client, Part, p, "stock") == 0
    page = ready_client.get("/bookings").text
    short = en("stock.parts_short", part="Insert M4", had=1, needed=4)
    done = en("bookings.stock_in_done", item="Template A", qty=2)
    assert short in page and done in page
    # The shortage is a warning, the booking itself is good news.
    short_tag = page[page.rindex("<p", 0, page.index(short)) : page.index(short)]
    done_tag = page[page.rindex("<p", 0, page.index(done)) : page.index(done)]
    assert 'class="alert"' in short_tag and 'role="alert"' in short_tag
    assert 'class="flash"' in done_tag and 'role="status"' in done_tag


def test_bookings_double_submit(ready_client):
    a = article(ready_client, stock=5)
    first, data = post(ready_client, "sale", article_id=a, qty="1")
    second = ready_client.post("/bookings/sale", data=data)
    assert first.status_code == 303 and second.status_code == 303
    assert number(ready_client, Article, a, "stock") == 4 and len(journal(ready_client)) == 1
    assert en("form.duplicate") in ready_client.get("/bookings").text


BAD = ["", "abc", "-1", "1.5", "1e9", "9" * 20]


@pytest.mark.parametrize("bad", BAD)
def test_invalid_numbers_no_500(ready_client, bad):
    a = article(ready_client, stock=5)
    p = part(ready_client, stock=5)
    for action, fields in (
        ("sale", {"article_id": a, "qty": bad}),
        ("stock-in", {"article_id": a, "qty": bad}),
        ("part-receipt", {"part_id": p, "qty": bad}),
        ("inventory", {"article_id": a, "field": "stock", "counted": bad}),
        ("part-inventory", {"part_id": p, "counted": bad}),
    ):
        response, _ = post(ready_client, action, **fields)
        assert response.status_code == 200, (action, bad)
    assert number(ready_client, Article, a, "stock") == 5 and journal(ready_client) == []


def test_zero_qty_rejected_but_zero_count_allowed(ready_client):
    a = article(ready_client, stock=5)
    assert post(ready_client, "sale", article_id=a, qty="0")[0].status_code == 200
    assert post(ready_client, "inventory", article_id=a, field="stock", counted="0")[0].status_code == 303


def test_unknown_article_and_part(ready_client):
    response, _ = post(ready_client, "sale", article_id="999", qty="1")
    assert response.status_code == 200 and en("articles.not_found") in response.text
    response, _ = post(ready_client, "part-receipt", part_id="999", qty="1")
    assert response.status_code == 200 and en("parts.not_found") in response.text
    response, _ = post(ready_client, "sale", article_id="abc", qty="1")
    assert response.status_code == 200


def test_forged_token(ready_client):
    a = article(ready_client, stock=5)
    response, _ = post(ready_client, "sale", article_id=a, qty="1", form_token="nonsense")
    assert response.status_code == 400 and en("form.expired") in response.text
    assert number(ready_client, Article, a, "stock") == 5


def test_recent_journal_and_link(ready_client):
    a = article(ready_client, stock=5)
    post(ready_client, "sale", article_id=a, qty="1", note="visible note")
    page = ready_client.get("/bookings").text
    assert "visible note" in page and 'href="/journal"' in page


def test_bookings_german(ready_client):
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    page = ready_client.get("/bookings").text
    for word in ("Inventur", "Zukaufteil-Eingang", "Verkauf ohne Bestellung", "Einbuchen ohne Druck", "Zukaufteile abziehen"):
        assert word in page, word


def test_booking_posts_require_csrf(ready_client):
    for action in ("inventory", "part-inventory", "part-receipt", "sale", "stock-in"):
        assert ready_client.post(f"/bookings/{action}", data={"form_token": "x"}).status_code == 403, action


def test_nav_bookings_active(ready_client):
    assert 'class="nav-link active" href="/bookings"' in ready_client.get("/bookings").text


# ---- p172: every hand booking has a date, today by default, never in the future (Spec 6.2) --------

import re  # noqa: E402
from datetime import timedelta  # noqa: E402

from reprintbuddy.models import utcnow  # noqa: E402
from reprintbuddy.web.formatting import to_local  # noqa: E402

FORMS = ("inventory", "part-inventory", "part-receipt", "sale", "stock-in")


def local_today():
    return to_local(utcnow()).date()


def test_each_booking_form_has_a_date_today_and_no_later(ready_client):
    html = ready_client.get("/bookings").text
    today = f"{local_today():%Y-%m-%d}"
    for action in FORMS:
        form = html.split(f'action="/bookings/{action}"', 1)[1].split("</form>", 1)[0]
        field = re.search(r'<input[^>]*name="booked_on"[^>]*>', form)
        assert field, action
        assert f'type="date"' in field[0] and f'value="{today}"' in field[0] and f'max="{today}"' in field[0], action
        assert "required" in field[0], action


def test_a_booking_dated_yesterday_counts_yesterday_and_keeps_when_it_was_saved(ready_client):
    a = article(ready_client, stock=5)
    yesterday = local_today() - timedelta(days=1)
    response, _ = post(ready_client, "sale", article_id=a, qty="1", booked_on=f"{yesterday:%Y-%m-%d}")
    assert response.status_code == 303
    (row,) = journal(ready_client)
    assert to_local(row.booked_at).date() == yesterday
    assert to_local(row.at).date() == local_today() and row.at > row.booked_at
    assert number(ready_client, Article, a, "stock") == 4


def test_a_booking_dated_today_counts_when_it_is_saved(ready_client):
    a = article(ready_client, stock=5)
    response, _ = post(ready_client, "sale", article_id=a, qty="1", booked_on=f"{local_today():%Y-%m-%d}")
    assert response.status_code == 303
    (row,) = journal(ready_client)
    assert row.booked_at == row.at


@pytest.mark.parametrize("action", FORMS)
def test_a_future_date_is_rejected_and_nothing_is_booked(ready_client, action):
    a, p = article(ready_client, stock=5), part(ready_client, stock=5)
    fields = {"article_id": a, "part_id": p, "qty": "1", "counted": "3", "field": "stock", "consume_parts": "1"}
    tomorrow = f"{local_today() + timedelta(days=1):%Y-%m-%d}"
    response, _ = post(ready_client, action, booked_on=tomorrow, **fields)
    assert response.status_code == 200, action  # the page again, with the message at the form
    assert en("bookings.date_future") in response.text
    assert journal(ready_client) == []
    assert f'value="{tomorrow}"' in response.text  # the typed date stays in its form


@pytest.mark.parametrize("bad", ["", "yesterday", "2026-13-01", "1999-12-31"])
def test_a_missing_or_odd_date_is_rejected(ready_client, bad):
    a = article(ready_client, stock=5)
    response, _ = post(ready_client, "sale", article_id=a, qty="1", booked_on=bad)
    assert response.status_code == 200 and journal(ready_client) == []
    assert 'role="alert"' in response.text


def test_a_form_without_the_date_field_books_today(ready_client):  # a page loaded before the update
    a = article(ready_client, stock=5)
    response, _ = post(ready_client, "sale", article_id=a, qty="1")
    assert response.status_code == 303
    (row,) = journal(ready_client)
    assert row.booked_at == row.at


def test_journal_shows_the_booking_date_and_when_it_was_saved(ready_client):
    a = article(ready_client, stock=5)
    yesterday = local_today() - timedelta(days=1)
    post(ready_client, "sale", article_id=a, qty="1", booked_on=f"{yesterday:%Y-%m-%d}")
    post(ready_client, "sale", article_id=a, qty="2", booked_on=f"{local_today():%Y-%m-%d}")
    html = ready_client.get("/journal").text
    cells = re.findall(r'<td class="nowrap">(.*?)</td>', html, re.S)
    dated, plain = cells[1], cells[0]  # newest first: today's sale, then yesterday's
    assert f"{yesterday:%Y-%m-%d}" in dated and en("journal.saved_at", at="").strip() in dated
    assert "col-note" not in plain
