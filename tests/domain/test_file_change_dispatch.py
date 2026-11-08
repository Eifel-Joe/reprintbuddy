"""Changing the print file of an article at any time (Spec 5.1, v0.3.10 p135): what happens to prints.

Entries of the work queue not yet handed to Bambuddy print with the new file (the hand-over reads
the article when it reserves). What lies in Bambuddy stays as it is. A change while a hand-over is
unclear sends nothing a second time, and since v0.3.11 (p53) it no longer pauses the article for
nothing: each print keeps the file and plate it was sent with, and the search after an unclear answer
looks for those (Spec 7.6). Two candidates still pause; a print from before the migration (no file
stored) is searched with the article's file as before.
"""

from datetime import timedelta

from sqlalchemy import func, select

from factories import make_article, make_dispatch, make_entry
from reprintbuddy import settings as st
from reprintbuddy.bambuddy.client import BambuddyUnavailable
from reprintbuddy.bambuddy.types import Plate
from reprintbuddy.domain import catalog, dispatch
from reprintbuddy.domain import workqueue as wq
from reprintbuddy.models import Article, Dispatch, WorkQueueEntry, utcnow


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


async def unclear_hand_over(db, fake):
    """An article with two prints; the first one's `enqueue` arrived in Bambuddy, its answer was lost."""
    with db() as s:
        st.set_setting(s, "bambuddy_username", fake.username)
    with db.begin() as s:
        a = article(s).id
        entry(s, a, 2)
    with db.begin() as s:
        res = dispatch.reserve_next(s, cap=10)
    fake.fail_next("enqueue", BambuddyUnavailable("timeout"), after_effect=True)  # arrived, answer lost
    assert await dispatch.hand_over(fake, db, res, insert_next=False) == "unclear"
    return a, res


def test_a_reserved_print_keeps_the_file_and_plate_it_is_sent_with(db):
    with db.begin() as s:
        a = article(s).id
        entry(s, a)
    with db.begin() as s:
        res = dispatch.reserve_next(s, cap=10)
        row = s.get(Dispatch, res.dispatch_id)
        assert (row.library_file_id, row.plate_id) == (res.library_file_id, res.plate_id) == (1, 1)


async def test_change_while_a_hand_over_is_unclear_finds_the_entry_and_sends_nothing_again(db, fake_bambuddy):
    """p53: the entry was made with the old file; the print remembers it and is adopted, no pause."""
    a, res = await unclear_hand_over(db, fake_bambuddy)
    (item,) = await fake_bambuddy.list_all_items()
    await change_file(db, fake_bambuddy, a)

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report.resolved == 1 and report.lost == [] and report.duplicates == []
    assert fake_bambuddy.calls.count("enqueue") == 1  # sent once, never again
    with db.begin() as s:
        row = s.get(Dispatch, res.dispatch_id)
        assert (row.status, row.bambuddy_item_id) == ("queued", item.id)
        art = s.get(Article, a)
        assert not art.paused and art.pause_reason is None


async def test_change_between_reserve_and_send_sends_and_stores_the_reserved_file(db, fake_bambuddy):
    """One source: what goes to Bambuddy is what the print stores at the reservation, and the search
    after an unclear answer looks for that - not for the file the article holds by the time of the send."""
    with db() as s:
        st.set_setting(s, "bambuddy_username", fake_bambuddy.username)
    with db.begin() as s:
        a = article(s).id
        entry(s, a)
    with db.begin() as s:
        res = dispatch.reserve_next(s, cap=10)
    await change_file(db, fake_bambuddy, a)  # after the reservation, before the send
    fake_bambuddy.fail_next("enqueue", BambuddyUnavailable("timeout"), after_effect=True)
    assert await dispatch.hand_over(fake_bambuddy, db, res, insert_next=False) == "unclear"
    (request,) = fake_bambuddy.enqueued
    with db.begin() as s:
        row = s.get(Dispatch, res.dispatch_id)
        assert (request.library_file_id, request.plate_id) == (row.library_file_id, row.plate_id) == (1, 1)
    (item,) = await fake_bambuddy.list_all_items()

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report.resolved == 1 and fake_bambuddy.calls.count("enqueue") == 1
    with db.begin() as s:
        assert s.get(Dispatch, res.dispatch_id).bambuddy_item_id == item.id
        assert not s.get(Article, a).paused


async def test_change_while_unclear_with_two_old_file_entries_still_pauses(db, fake_bambuddy):
    """Never a guess: two own entries of the print's file and plate after the send stay ambiguous."""
    a, res = await unclear_hand_over(db, fake_bambuddy)
    other = fake_bambuddy.add_foreign_item(
        "Template A.gcode.3mf", created_by=fake_bambuddy.username, library_file_id=1, plate_id=1
    )
    await change_file(db, fake_bambuddy, a)

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report.resolved == 0 and len(report.duplicates) == 1 and other.id in report.duplicates[0]
    assert fake_bambuddy.calls.count("enqueue") == 1
    with db.begin() as s:
        assert s.get(Dispatch, res.dispatch_id).status == "missing"
        art = s.get(Article, a)
        assert art.paused and art.pause_reason == "unclear"


async def test_an_entry_of_the_new_file_is_not_taken_for_the_print(db, fake_bambuddy):
    """The print was sent with file 1: an own entry of file 2 is somebody else's, whatever the article
    holds now. The entry of file 1 is gone (deleted in Bambuddy): lost, paused, nothing sent."""
    a, res = await unclear_hand_over(db, fake_bambuddy)
    (sent,) = await fake_bambuddy.list_all_items()
    fake_bambuddy.delete_item(sent.id)
    fake_bambuddy.add_foreign_item(
        "Template A.gcode.3mf", created_by=fake_bambuddy.username, library_file_id=2, plate_id=1
    )
    await change_file(db, fake_bambuddy, a)

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report.resolved == 0 and report.lost == [res.dispatch_id]
    assert fake_bambuddy.calls.count("enqueue") == 1
    with db.begin() as s:
        assert s.get(Dispatch, res.dispatch_id).status == "missing"
        assert s.get(Article, a).pause_reason == "unclear"


async def test_a_print_from_before_the_migration_is_searched_with_the_articles_file(db, fake_bambuddy):
    """No file stored (made before revision 0010): the search falls back to the article, as before."""
    with db() as s:
        st.set_setting(s, "bambuddy_username", fake_bambuddy.username)
    sent_at = utcnow()
    with db.begin() as s:
        a = article(s).id
        d = make_dispatch(s, a, status="unclear", enqueue_sent_at=sent_at - timedelta(seconds=1)).id
        assert s.get(Dispatch, d).library_file_id is None
    item = fake_bambuddy.add_foreign_item(
        "Template A.gcode.3mf", created_by=fake_bambuddy.username, library_file_id=1, plate_id=1,
        created_at=sent_at,
    )

    report = await dispatch.resolve_pending(fake_bambuddy, db)

    assert report.resolved == 1
    with db.begin() as s:
        assert s.get(Dispatch, d).bambuddy_item_id == item.id
