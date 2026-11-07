import pytest
from sqlalchemy import select

from factories import make_article
from reprintbuddy.domain import pauses
from reprintbuddy.domain.errors import ConflictError
from reprintbuddy.models import Article, JournalEntry


def journal(s, kind):
    return list(
        s.scalars(select(JournalEntry).where(JournalEntry.kind == kind).order_by(JournalEntry.id))
    )


def test_pause_first_reason_kept(db):
    with db.begin() as s:
        a = make_article(s).id
        assert pauses.pause_article(s, a, "two_failures", "auto") is True
        assert pauses.pause_article(s, a, "queue_reduced", "auto") is False
        article = s.get(Article, a)
        assert article.paused and article.pause_reason == "two_failures"
        rows = journal(s, "automation_paused")
        assert len(rows) == 1
        assert rows[0].details["reason"] == "two_failures"


def test_file_reason_overrides_other_pause(db):
    with db.begin() as s:
        a = make_article(s).id
        pauses.pause_article(s, a, "queue_reduced", "auto")
        assert pauses.pause_article(s, a, "file_changed", "auto") is True
        assert s.get(Article, a).pause_reason == "file_changed"
        rows = journal(s, "automation_paused")
        assert len(rows) == 2
        assert rows[1].details["replaced"] == "queue_reduced"
        assert pauses.pause_article(s, a, "file_changed", "auto") is False
        assert len(journal(s, "automation_paused")) == 2


def test_lift_pause_resets(db):
    with db.begin() as s:
        a = make_article(s, paused=True, pause_reason="queue_reduced", consecutive_failures=1).id
        pauses.lift_pause(s, a, "user")
        article = s.get(Article, a)
        assert not article.paused
        assert article.pause_reason is None
        assert article.consecutive_failures == 0
        rows = journal(s, "automation_resumed")
        assert len(rows) == 1 and rows[0].actor == "user"


@pytest.mark.parametrize(
    ("fields", "key"),
    [
        ({}, "queue.not_paused"),
        ({"paused": True, "pause_reason": "file_changed"}, "articles.pause_needs_file"),
        ({"paused": True, "pause_reason": "file_missing"}, "articles.pause_needs_file"),
        ({"paused": True, "pause_reason": "no_target"}, "articles.pause_needs_target"),
    ],
)
def test_lift_pause_refusals(db, fields, key):
    with db.begin() as s:
        a = make_article(s, **fields).id
        with pytest.raises(ConflictError) as error:
            pauses.lift_pause(s, a, "user")
        assert error.value.key == key


def test_lift_no_target_with_target(db):
    with db.begin() as s:
        a = make_article(s, paused=True, pause_reason="no_target", target_model="A1").id
        pauses.lift_pause(s, a, "user")
        assert not s.get(Article, a).paused
