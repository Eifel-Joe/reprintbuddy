import pytest
from sqlalchemy import select

from factories import make_article
from reprintbuddy.domain import workqueue as wq
from reprintbuddy.domain.errors import NotFoundError, ValidationError
from reprintbuddy.models import Article, JournalEntry, WorkQueueEntry, utcnow


def ids(s):
    return [e.id for e in wq.list_entries(s)]


def positions(s):
    return list(s.scalars(select(WorkQueueEntry.position).order_by(WorkQueueEntry.position)))


def journal(s, kind=None):
    q = select(JournalEntry).order_by(JournalEntry.id)
    if kind:
        q = q.where(JournalEntry.kind == kind)
    return list(s.scalars(q))


def four(s):
    a = make_article(s).id
    return a, [wq.add_entry(s, a, 1, "manual", "user").id for _ in range(4)]


def test_add_end_start_before(db):
    with db.begin() as s:
        a = make_article(s).id
        e1 = wq.add_entry(s, a, 1, "manual", "user").id
        e2 = wq.add_entry(s, a, 1, "manual", "user").id
        e3 = wq.add_entry(s, a, 1, "manual", "user", where="start").id
        e4 = wq.add_entry(s, a, 1, "manual", "user", where=e2).id
        assert ids(s) == [e3, e1, e4, e2]
        assert positions(s) == [0, 1, 2, 3]


@pytest.mark.parametrize(
    ("kwargs", "key"),
    [
        ({"prints": 0}, "form.invalid_number"),
        ({"prints": 1001}, "form.invalid_number"),
        ({"archived": True}, "queue.article_archived"),
        ({"article_id": 999}, "articles.not_found"),
        ({"where": 999}, "queue.entry_not_found"),
    ],
)
def test_add_validation(db, kwargs, key):
    with db.begin() as s:
        archived = kwargs.pop("archived", False)
        a = make_article(s, archived_at=utcnow() if archived else None).id
        args = {"article_id": a, "prints": 1, **kwargs}
        with pytest.raises((ValidationError, NotFoundError)) as error:
            wq.add_entry(s, args.pop("article_id"), args.pop("prints"), "manual", "user", **args)
        assert error.value.key == key
        if key == "form.invalid_number":
            assert error.value.params["field"] == "queue.field.prints"


def test_add_journal(db):
    with db.begin() as s:
        a = make_article(s).id
        e = wq.add_entry(s, a, 2, "manual", "user", details={"x": 1})
        (row,) = journal(s, "queue_entry_created")
        assert row.qty == 2 and row.actor == "user" and row.article_id == a
        assert row.details["origin"] == "manual"
        assert row.details["entry_id"] == e.id
        assert row.details["x"] == 1


def test_replacement_goes_first(db):
    with db.begin() as s:
        a = make_article(s).id
        wq.add_entry(s, a, 1, "manual", "user")
        wq.add_entry(s, a, 1, "manual", "user")
        r = wq.add_replacement(s, a, 1, "scrap", "user").id
        assert ids(s)[0] == r


def test_move(db):
    with db.begin() as s:
        _, (e1, e2, e3, e4) = four(s)
        n = len(journal(s))
        wq.move_entry(s, e4, "top")
        assert ids(s) == [e4, e1, e2, e3]
        wq.move_entry(s, e4, "bottom")
        assert ids(s) == [e1, e2, e3, e4]
        wq.move_entry(s, e4, e2)  # up
        assert ids(s) == [e1, e4, e2, e3]
        wq.move_entry(s, e1, e3)  # down
        assert ids(s) == [e4, e2, e1, e3]
        wq.move_entry(s, e2, e2)
        assert ids(s) == [e4, e2, e1, e3]
        wq.move_entry(s, e4, "top")
        assert ids(s) == [e4, e2, e1, e3]
        assert positions(s) == [0, 1, 2, 3]
        assert len(journal(s)) == n


def test_move_target_gone(db):
    with db.begin() as s:
        _, (e1, e2, *_rest) = four(s)
        wq.delete_entry(s, e2, "user")
        with pytest.raises(NotFoundError) as error:
            wq.move_entry(s, e1, e2)
        assert error.value.key == "queue.entry_not_found"


def test_hold_release(db):
    with db.begin() as s:
        a = make_article(s).id
        e = wq.add_entry(s, a, 1, "manual", "user").id
        wq.hold_entry(s, e, "user")
        wq.hold_entry(s, e, "user")
        assert s.get(WorkQueueEntry, e).held
        assert len(journal(s, "queue_entry_held")) == 1
        wq.release_entry(s, e, "user")
        wq.release_entry(s, e, "user")
        assert not s.get(WorkQueueEntry, e).held
        assert len(journal(s, "queue_entry_released")) == 1


def test_hold_does_not_pause(db):
    with db.begin() as s:
        a = make_article(s).id
        e = wq.add_entry(s, a, 1, "rule", "auto").id
        wq.hold_entry(s, e, "user")
        assert not s.get(Article, a).paused


def test_set_prints_rule_lower_pauses(db):
    with db.begin() as s:
        a = make_article(s).id
        e = wq.add_entry(s, a, 3, "rule", "auto").id
        wq.set_prints(s, e, 2, "user")
        article = s.get(Article, a)
        assert article.paused and article.pause_reason == "queue_reduced"
        (row,) = journal(s, "queue_entry_changed")
        assert row.details["before"] == 3 and row.details["after"] == 2


def test_set_prints_rule_higher_no_pause(db):
    with db.begin() as s:
        a = make_article(s).id
        e = wq.add_entry(s, a, 2, "rule", "auto").id
        wq.set_prints(s, e, 3, "user")
        assert not s.get(Article, a).paused
        assert s.get(WorkQueueEntry, e).prints == 3


def test_set_prints_manual_lower_no_pause(db):
    with db.begin() as s:
        a = make_article(s).id
        e = wq.add_entry(s, a, 3, "manual", "user").id
        wq.set_prints(s, e, 2, "user")
        assert not s.get(Article, a).paused


def test_set_prints_validation(db):
    with db.begin() as s:
        a = make_article(s).id
        e = wq.add_entry(s, a, 3, "manual", "user").id
        with pytest.raises(ValidationError):
            wq.set_prints(s, e, 0, "user")


def test_delete_rule_entry_pauses(db):
    with db.begin() as s:
        a = make_article(s).id
        e = wq.add_entry(s, a, 1, "rule", "auto").id
        wq.delete_entry(s, e, "user")
        assert s.get(WorkQueueEntry, e) is None
        article = s.get(Article, a)
        assert article.paused and article.pause_reason == "queue_reduced"
        assert len(journal(s, "queue_entry_deleted")) == 1


def test_delete_manual_no_pause(db):
    with db.begin() as s:
        a = make_article(s).id
        e1 = wq.add_entry(s, a, 1, "manual", "user").id
        e2 = wq.add_entry(s, a, 1, "manual", "user").id
        wq.delete_entry(s, e1, "user")
        assert not s.get(Article, a).paused
        assert positions(s) == [0]
        assert ids(s) == [e2]


def test_list_entries_rows(db):
    with db.begin() as s:
        a = make_article(s, name="Template B", paused=True, pause_reason="unclear").id
        wq.add_entry(s, a, 2, "manual", "user")
        (row,) = wq.list_entries(s)
        assert row.article_name == "Template B" and row.article_paused and row.prints == 2


def test_unknown_entry(db):
    with db.begin() as s:
        calls = [
            lambda: wq.move_entry(s, 999, "top"),
            lambda: wq.hold_entry(s, 999, "user"),
            lambda: wq.release_entry(s, 999, "user"),
            lambda: wq.set_prints(s, 999, 2, "user"),
            lambda: wq.delete_entry(s, 999, "user"),
        ]
        for call in calls:
            with pytest.raises(NotFoundError) as error:
                call()
            assert error.value.key == "queue.entry_not_found"
