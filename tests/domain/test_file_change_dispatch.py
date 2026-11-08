"""Changing the print file of an article at any time (Spec 5.1, v0.3.10 p135): what happens to prints.

Entries of the work queue not yet handed to Bambuddy print with the new file (the hand-over reads
the article when it reserves). What lies in Bambuddy stays as it is. A change while a hand-over is
unclear sends nothing a second time: the search no longer finds the entry (it looks for the
article's file), the print is `missing` and the article pauses (Spec 7.6, p53).
"""

from sqlalchemy import func, select

from factories import make_article, make_entry
from reprintbuddy import settings as st
from reprintbuddy.bambuddy.client import BambuddyUnavailable
from reprintbuddy.bambuddy.types import Plate
from reprintbuddy.domain import catalog, dispatch
from reprintbuddy.domain import workqueue as wq
from reprintbuddy.models import Article, Dispatch, WorkQueueEntry


def article(s, name="Template A", **fields):
    fields.setdefault("target_model", "A1")
    return make_article(s, name=name, library_file_id=1, plate_id=1, **fields)


def entry(s, article_id, prints=1):
    position = s.scalar(select(func.count()).select_from(WorkQueueEntry))
    return make_entry(s, article_id, prints, position=position)


async def change_file(db, fake, article_id, file_id=2):
    file = await fake.get_file(file_id)
    with db.begin() as s:
        catalog.relink_article(
            s, article_id, file=file, plate=Plate(1, None), requirements=[], thumbnail=None, actor="user"
        )


async def test_waiting_entry_prints_with_the_new_file(db, fake_bambuddy):
    with db.begin() as s:
        a = article(s).id
        entry(s, a, 2)
    await change_file(db, fake_bambuddy, a)
    with db.begin() as s:
        res = dispatch.reserve_next(s, cap=10)
    assert (res.library_file_id, res.plate_id) == (2, 1)
    assert await dispatch.hand_over(fake_bambuddy, db, res, insert_next=False) == "queued"
    request = fake_bambuddy.enqueued[-1]
    assert (request.library_file_id, request.plate_id) == (2, 1)
    with db.begin() as s:
        assert wq.list_entries(s)[0].prints == 1  # the entry itself stays, with its remaining prints


async def test_print_already_in_bambuddy_stays_as_it_is(db, fake_bambuddy):
    with db.begin() as s:
        a = article(s).id
        entry(s, a)
    with db.begin() as s:
        res = dispatch.reserve_next(s, cap=10)
    assert await dispatch.hand_over(fake_bambuddy, db, res, insert_next=False) == "queued"
    (before,) = await fake_bambuddy.list_all_items()
    await change_file(db, fake_bambuddy, a)
    (after,) = await fake_bambuddy.list_all_items()
    assert after == before and before.library_file_id == 1
    assert len(fake_bambuddy.enqueued) == 1
    with db.begin() as s:
        row = s.scalars(select(Dispatch)).one()
        assert (row.status, row.bambuddy_item_id) == ("queued", before.id)  # still followed by its Bambuddy id


async def test_reserved_but_never_sent_goes_back_and_prints_with_the_new_file(db, fake_bambuddy):
    with db.begin() as s:
        a = article(s).id
        entry(s, a)
    with db.begin() as s:
        dispatch.reserve_next(s, cap=10)  # reserved with file 1, then the process stops before the send
    await change_file(db, fake_bambuddy, a)
    report = await dispatch.resolve_pending(fake_bambuddy, db)
    assert report.lost == [] and fake_bambuddy.enqueued == []
    with db.begin() as s:
        res = dispatch.reserve_next(s, cap=10)
    assert res.library_file_id == 2
    await dispatch.hand_over(fake_bambuddy, db, res, insert_next=False)
    assert [r.library_file_id for r in fake_bambuddy.enqueued] == [2]


async def test_change_while_a_hand_over_is_unclear_sends_nothing_again(db, fake_bambuddy):
    with db() as s:
        st.set_setting(s, "bambuddy_username", fake_bambuddy.username)
    with db.begin() as s:
        a = article(s).id
        entry(s, a, 2)
    with db.begin() as s:
        res = dispatch.reserve_next(s, cap=10)
    fake_bambuddy.fail_next("enqueue", BambuddyUnavailable("timeout"), after_effect=True)  # arrived, answer lost
    assert await dispatch.hand_over(fake_bambuddy, db, res, insert_next=False) == "unclear"
    assert len(await fake_bambuddy.list_all_items()) == 1
    await change_file(db, fake_bambuddy, a)

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report.lost == [res.dispatch_id]  # not found under the new file: a pause, no guess
    assert fake_bambuddy.calls.count("enqueue") == 1  # sent once, never again
    with db.begin() as s:
        row = s.get(Dispatch, res.dispatch_id)
        assert row.status == "missing"
        art = s.get(Article, a)
        assert art.paused and art.pause_reason == "unclear"
        assert dispatch.open_count(s) == 0
