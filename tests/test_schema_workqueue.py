import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from reprintbuddy.models import Inspection

from factories import make_article, make_dispatch, make_entry

TABLES = {"work_queue", "dispatches", "inspections"}


def test_tables_exist(db):
    assert TABLES <= set(inspect(db.kw["bind"]).get_table_names())



def test_entry_defaults(db):
    with db.begin() as s:
        a = make_article(s)
        entry = make_entry(s, a.id)
        dispatch = make_dispatch(s, a.id)
        s.refresh(entry)
        s.refresh(dispatch)
        assert entry.held is False
        assert entry.created_at is not None
        assert dispatch.enqueue_sent_at is None
        assert dispatch.created_at is not None


def _entry(s, a, **f):
    return make_entry(s, a, **f)


@pytest.mark.parametrize(
    "build",
    [
        lambda s, a: make_entry(s, a, prints=0),
        lambda s, a: make_entry(s, a, origin="bogus"),
        lambda s, a: make_dispatch(s, a, origin="bogus"),
        lambda s, a: make_dispatch(s, a, status="lost"),
        lambda s, a: make_dispatch(s, a, status="rejected"),
        lambda s, a: make_dispatch(s, a, units=0),
        lambda s, a: _inspection(s, a, units=2, good_units=3),
        lambda s, a: _inspection(s, a, units=0),
    ],
    ids=[
        "prints=0", "entry origin", "dispatch origin", "status=lost", "status=rejected",
        "units=0", "good_units>units", "inspection units=0",
    ],
)
def test_checks(db, build):
    with db.begin() as s:
        a = make_article(s).id
    with pytest.raises(IntegrityError):
        with db.begin() as s:
            build(s, a)


def _inspection(s, article_id, *, units, good_units=None):
    d = make_dispatch(s, article_id)
    i = Inspection(dispatch_id=d.id, article_id=article_id, units=units, good_units=good_units)
    s.add(i)
    s.flush()
    return i


def test_external_ref_unique(db):
    with db.begin() as s:
        a = make_article(s).id
        make_dispatch(s, a, external_ref="same")
    with pytest.raises(IntegrityError):
        with db.begin() as s:
            make_dispatch(s, a, external_ref="same")


def test_one_inspection_per_dispatch(db):
    with db.begin() as s:
        a = make_article(s).id
        d = make_dispatch(s, a).id
        s.add(Inspection(dispatch_id=d, article_id=a, units=1))
    with pytest.raises(IntegrityError):
        with db.begin() as s:
            s.add(Inspection(dispatch_id=d, article_id=a, units=1))
