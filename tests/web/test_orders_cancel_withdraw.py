"""What a cancel takes back, on the pages (Spec 7.2a, 0.4.1 p382): the confirmation says it before,
the message after; the overview warns while Bambuddy could not be reached or refused."""

import re
from datetime import date

from conftest import csrf_of, form_token_of, sm_of
from factories import make_article, make_dispatch, make_entry
from sqlalchemy import select

from reprintbuddy.domain import orders
from reprintbuddy.domain.engine import EngineStatus, collect_warnings, warning_text
from reprintbuddy.domain.orders import ArticleItem
from reprintbuddy.models import ActivityEntry, JournalEntry, Withdrawal, WorkQueueEntry
from reprintbuddy.web.i18n import translate


def en(key, **params):
    return translate("en", key, **params)


def place(client, *, entries=2, waiting=3, qty=5):
    """Minimum 2, buffer 1, stock 3: an order of `qty` plans prints, here `entries` still in the queue
    and `waiting` handed over and waiting in Bambuddy."""
    with sm_of(client).begin() as s:
        article = make_article(s, name="Template A", min_stock=2, buffer=1, stock=3, target_model="A1")
        order_id = orders.create_order(
            s, reference="Order 3001", order_date=date(2026, 11, 11), items=[ArticleItem(article.id, qty)], actor="user",
        ).id
        if entries:
            make_entry(s, article.id, entries)
        for item in range(waiting):
            make_dispatch(s, article.id, bambuddy_item_id=item + 1)
        return article.id, order_id


def confirm_page(client, order_id):
    data = {"csrf_token": csrf_of(client, f"/orders/{order_id}"),
            "form_token": form_token_of(client, f"/orders/{order_id}", f"/orders/{order_id}/cancel")}
    return client.post(f"/orders/{order_id}/cancel", data=data)


def hidden(html):
    return dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', html))


def counts(client):
    with sm_of(client)() as s:
        return (
            len(s.scalars(select(JournalEntry)).all()),
            len(s.scalars(select(ActivityEntry)).all()),
            [(e.prints, e.held) for e in s.scalars(select(WorkQueueEntry))],
        )


def test_the_confirmation_says_what_comes_back_and_changes_nothing(ready_client):  # A3
    _, order_id = place(ready_client)
    live = ready_client.app.state.live
    before, published = counts(ready_client), live.state
    page = confirm_page(ready_client, order_id)
    assert page.status_code == 200
    assert en("orders.cancel_withdraw_both", queue=2, bambuddy=3) in page.text
    assert counts(ready_client)[0] == before[0] and counts(ready_client)[2] == before[2]
    assert live.state == published  # the rolled-back preview published nothing


def test_only_queue_or_only_bambuddy_or_nothing(ready_client):
    _, first = place(ready_client, entries=2, waiting=0, qty=2)
    assert en("orders.cancel_withdraw_queue", queue=2) in confirm_page(ready_client, first).text
    with sm_of(ready_client).begin() as s:
        s.execute(WorkQueueEntry.__table__.delete())
    _, second = place(ready_client, entries=0, waiting=1, qty=1)
    html = confirm_page(ready_client, second).text
    assert en("orders.cancel_withdraw_bambuddy", bambuddy=1) in html


def test_no_sentence_without_anything_to_take_back(ready_client):
    _, order_id = place(ready_client, entries=0, waiting=0)
    html = confirm_page(ready_client, order_id).text
    assert 'class="withdraw-preview"' not in html


def test_cancelling_says_what_came_back_and_wakes_the_engine(ready_client):  # A1
    article_id, order_id = place(ready_client)
    calls: list[int] = []
    ready_client.app.state.ticker.request_tick = lambda: calls.append(1)
    first = confirm_page(ready_client, order_id)
    done = ready_client.post(f"/orders/{order_id}/cancel", data=hidden(first.text))
    assert done.status_code == 303 and calls
    page = ready_client.get(done.headers["location"]).text
    assert en("orders.cancelled_withdrawn_both", reference="Order 3001", queue=2, bambuddy=3) in page
    with sm_of(ready_client)() as s:
        assert s.scalars(select(WorkQueueEntry)).all() == []
        assert s.get(Withdrawal, article_id).prints == 3
    # the same form once more: cancelled and taken back only once
    again = ready_client.post(f"/orders/{order_id}/cancel", data=hidden(first.text))
    assert again.status_code in (200, 303)
    with sm_of(ready_client)() as s:
        lines = s.scalars(select(JournalEntry).where(JournalEntry.kind == "queue_entry_withdrawn")).all()
        assert len(lines) == 1


def test_a_cancel_without_anything_to_take_back_keeps_the_old_message(ready_client):
    _, order_id = place(ready_client, entries=0, waiting=0)
    first = confirm_page(ready_client, order_id)
    done = ready_client.post(f"/orders/{order_id}/cancel", data=hidden(first.text))
    assert en("orders.cancelled", reference="Order 3001") in ready_client.get(done.headers["location"]).text


# ---- warnings on the overview (A2) ---------------------------------------------------------------


def _warnings(client):
    with sm_of(client)() as s:
        return [warning_text("en", w) for w in collect_warnings(s, EngineStatus())]


def test_no_warning_between_the_cancel_and_the_run(ready_client):
    article_id, _ = place(ready_client)
    with sm_of(ready_client).begin() as s:
        s.add(Withdrawal(article_id=article_id, prints=3))
    assert not [w for w in _warnings(ready_client) if "Bambuddy" in w and "3" in w]


def test_waiting_warning_once_a_run_could_not_reach_bambuddy(ready_client):
    article_id, _ = place(ready_client)
    with sm_of(ready_client).begin() as s:
        s.add(Withdrawal(article_id=article_id, prints=3, unreached=True))
    assert en("warning.withdraw_waiting", count=3) in _warnings(ready_client)


def test_refused_warning_names_the_article_and_bambuddys_answer(ready_client):
    article_id, _ = place(ready_client)
    message = "403: API key owner does not have 'queue:delete_own' permission"
    with sm_of(ready_client).begin() as s:
        s.add(Withdrawal(article_id=article_id, prints=3, refused=message))
    assert en("warning.withdraw_refused", name="Template A", count=3, message=message) in _warnings(ready_client)


def test_german_texts_exist_and_differ():
    for key in (
        "orders.cancel_withdraw_both", "orders.cancel_withdraw_queue", "orders.cancel_withdraw_bambuddy",
        "orders.cancelled_withdrawn_both", "orders.cancelled_withdrawn_queue", "orders.cancelled_withdrawn_bambuddy",
        "warning.withdraw_waiting", "warning.withdraw_refused",
    ):
        assert translate("de", key) != translate("en", key) != key
