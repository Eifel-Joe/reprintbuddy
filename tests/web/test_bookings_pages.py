import pytest
from sqlalchemy import select

from conftest import csrf_of, form_token_of, sm_of
from factories import make_article, make_part
from texts import en
from reprintbuddy import settings as st
from reprintbuddy.domain import stock
from reprintbuddy.models import Article, ArticlePart, JournalEntry, Part


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


# ---- p210: an inventory dated back counts against the value of that day (Spec 6.2) --------------


def test_inventory_dated_back_keeps_the_later_sale_and_says_so(ready_client):
    a = article(ready_client, stock=12)
    yesterday = local_today() - timedelta(days=1)
    post(ready_client, "sale", article_id=a, qty="2")  # today: 10
    response, _ = post(ready_client, "inventory", article_id=a, field="stock", counted="10",
                       booked_on=f"{yesterday:%Y-%m-%d}")
    assert response.status_code == 303
    assert number(ready_client, Article, a, "stock") == 8
    html = ready_client.get("/bookings").text
    assert en("bookings.inventory_done_then", item="Template A", counted=10, day=f"{yesterday:%Y-%m-%d}",
              then=12, diff="-2") in html


def test_inventory_dated_before_a_later_inventory_is_refused_with_its_date(ready_client):
    p = part(ready_client, stock=5)
    post(ready_client, "part-inventory", part_id=p, counted="7")
    response, _ = post(ready_client, "part-inventory", part_id=p, counted="3",
                       booked_on=f"{local_today() - timedelta(days=1):%Y-%m-%d}")
    assert response.status_code == 200
    assert en("bookings.later_inventory", at=f"{local_today():%Y-%m-%d}") in response.text
    assert number(ready_client, Part, p, "stock") == 7


def test_journal_shows_the_value_of_that_day_of_an_inventory_dated_back(ready_client):
    a = article(ready_client, stock=12)
    post(ready_client, "sale", article_id=a, qty="2")
    post(ready_client, "inventory", article_id=a, field="stock", counted="10",
         booked_on=f"{local_today() - timedelta(days=1):%Y-%m-%d}")
    html = ready_client.get("/journal").text
    assert en("journal.then", before=12, after=10) in html
    assert "Stock 10 → 8" in html


# ---- p260: an inventory dated back holds for the end of that day (Spec 6.2) -----------------------

from datetime import time  # noqa: E402

from reprintbuddy.localtime import end_of_local_day  # noqa: E402


def test_an_inventory_dated_back_holds_for_the_end_of_that_day(ready_client):
    a = article(ready_client, stock=5)
    yesterday = local_today() - timedelta(days=1)
    response, _ = post(ready_client, "inventory", article_id=a, field="stock", counted="5",
                       booked_on=f"{yesterday:%Y-%m-%d}")
    assert response.status_code == 303
    (row,) = journal(ready_client)
    assert to_local(row.booked_at).date() == yesterday
    assert to_local(row.booked_at).time() == time(23, 59, 59, 999999)


def test_a_sale_in_the_evening_before_the_count_is_held_in_it(ready_client):
    """The example of p260: counted 10 last night, typed in this morning. The sale at 18:00 lies
    before the count; the stock stays 10, not 9."""
    a = article(ready_client, stock=11)
    yesterday = local_today() - timedelta(days=1)
    evening = end_of_local_day(yesterday) - timedelta(hours=6)  # 18:00 of yesterday
    with sm_of(ready_client).begin() as s:
        stock.sell_unreserved(s, a, 1, "user", booked_at=evening)  # 11 -> 10
    response, _ = post(ready_client, "inventory", article_id=a, field="stock", counted="10",
                       booked_on=f"{yesterday:%Y-%m-%d}")
    assert response.status_code == 303
    assert number(ready_client, Article, a, "stock") == 10
    assert journal(ready_client)[-1].qty == 0


def test_a_part_inventory_dated_back_holds_for_the_end_of_that_day(ready_client):  # sister path
    p = part(ready_client, stock=5)
    yesterday = local_today() - timedelta(days=1)
    post(ready_client, "part-inventory", part_id=p, counted="4", booked_on=f"{yesterday:%Y-%m-%d}")
    (row,) = journal(ready_client)
    assert to_local(row.booked_at).time() == time(23, 59, 59, 999999)


def test_other_bookings_dated_back_keep_the_clock_time(ready_client):
    a = article(ready_client, stock=5)
    yesterday = local_today() - timedelta(days=1)
    post(ready_client, "sale", article_id=a, qty="1", booked_on=f"{yesterday:%Y-%m-%d}")
    (row,) = journal(ready_client)
    assert to_local(row.booked_at).time() != time(23, 59, 59, 999999)
    assert abs((row.at - row.booked_at) - timedelta(days=1)) < timedelta(hours=2)  # same clock time


def test_a_booking_of_the_counted_day_is_refused_after_a_count_dated_back(ready_client):  # p260 + p261
    a = article(ready_client, stock=10)
    yesterday = f"{local_today() - timedelta(days=1):%Y-%m-%d}"
    post(ready_client, "inventory", article_id=a, field="stock", counted="10", booked_on=yesterday)
    response, _ = post(ready_client, "sale", article_id=a, qty="2", booked_on=yesterday)
    assert response.status_code == 200
    assert en("bookings.counted_later", item="Template A", at=yesterday) in response.text
    assert number(ready_client, Article, a, "stock") == 10


@pytest.mark.parametrize("action", ["sale", "stock-in", "part-receipt"])
def test_each_dated_booking_before_a_count_is_refused_with_its_day(ready_client, action):  # p261
    a, p = article(ready_client, stock=5), part(ready_client, stock=5)
    post(ready_client, "inventory", article_id=a, field="stock", counted="5")
    post(ready_client, "part-inventory", part_id=p, counted="5")
    fields = {"article_id": a, "part_id": p, "qty": "1"}
    response, _ = post(ready_client, action, booked_on=f"{local_today() - timedelta(days=2):%Y-%m-%d}", **fields)
    assert response.status_code == 200, action
    item = "Insert M4" if action == "part-receipt" else "Template A"
    assert en("bookings.counted_later", item=item, at=f"{local_today():%Y-%m-%d}") in response.text
    assert len(journal(ready_client)) == 2  # the two counts only


def test_a_sale_dated_to_a_summer_day_stays_on_its_day_in_the_zone_of_the_server(ready_client, monkeypatch):
    """Review finding: the clock time of an earlier day took the offset of today. Typed in winter at
    23:30 and dated to a summer day, the sale landed on the next day (in a zone with summer time)."""
    from datetime import UTC, date, datetime

    from reprintbuddy import localtime
    from reprintbuddy.web.routes import bookings

    monkeypatch.setattr(localtime, "LOCAL_ZONE", localtime.SERVER_ZONE)
    winter_evening = datetime.combine(date(2026, 11, 10), time(23, 30)).astimezone().astimezone(UTC)
    monkeypatch.setattr(bookings, "utcnow", lambda: winter_evening.replace(tzinfo=None))
    a = article(ready_client, stock=5)
    response, _ = post(ready_client, "sale", article_id=a, qty="1", booked_on="2026-10-24")
    assert response.status_code == 303
    (row,) = journal(ready_client)
    assert localtime.to_local(row.booked_at).replace(tzinfo=None) == datetime(2026, 10, 24, 23, 30)


def test_a_stock_in_deducting_parts_is_refused_when_a_part_was_counted_later(ready_client):  # p261
    a, p = article(ready_client, stock=0), part(ready_client, stock=10)
    with sm_of(ready_client).begin() as s:
        s.add(ArticlePart(article_id=a, part_id=p, qty_per_unit=1))
    post(ready_client, "part-inventory", part_id=p, counted="10")
    two_days_ago = f"{local_today() - timedelta(days=2):%Y-%m-%d}"
    response, _ = post(ready_client, "stock-in", article_id=a, qty="2", consume_parts="1", booked_on=two_days_ago)
    assert response.status_code == 200
    assert en("bookings.counted_later", item="Insert M4", at=f"{local_today():%Y-%m-%d}") in response.text
    assert (number(ready_client, Article, a, "stock"), number(ready_client, Part, p, "stock")) == (0, 10)
    response, _ = post(ready_client, "stock-in", article_id=a, qty="2", booked_on=two_days_ago)  # unticked
    assert response.status_code == 303
    assert (number(ready_client, Article, a, "stock"), number(ready_client, Part, p, "stock")) == (2, 10)
