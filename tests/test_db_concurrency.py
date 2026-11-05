"""Doppelte Buchungen und verlorene Aktualisierungen bei gleichzeitigen Anfragen."""

import threading
import time

from sqlalchemy import func, select

from factories import make_article
from reprintbuddy.domain import stock
from reprintbuddy.models import Article, JournalEntry
from reprintbuddy.web.formtoken import FormTokenUsed, consume_form_token, issue_form_token


def test_concurrent_double_submit_books_once(db):
    with db.begin() as s:
        a = make_article(s).id
    tok, barrier, results = issue_form_token("secret"), threading.Barrier(2), []

    def submit():
        barrier.wait()
        try:
            with db.begin() as s:
                consume_form_token(s, "secret", tok)
                stock.stock_in(s, a, 1, "user", consume_parts=False)
            results.append("ok")
        except FormTokenUsed:
            results.append("dup")
        except Exception as e:  # any other exception fails the test
            results.append(repr(e))

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["dup", "ok"]
    with db.begin() as s:
        assert s.get(Article, a).stock == 1
        assert s.scalar(select(func.count()).select_from(JournalEntry)) == 1


def test_concurrent_bookings_do_not_lose_updates(db):
    with db.begin() as s:
        a = make_article(s).id
    errors = []

    def book():
        try:
            with db.begin() as s:
                s.get(Article, a)  # read first ...
                time.sleep(0.2)  # ... then write: a deferred transaction would lose or lock here
                stock.stock_in(s, a, 1, "user", consume_parts=False)
        except Exception as e:
            errors.append(repr(e))

    threads = [threading.Thread(target=book) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with db.begin() as s:
        assert s.get(Article, a).stock == 2
        assert s.scalar(select(func.count()).select_from(JournalEntry)) == 2
