"""Checking the print file and the print target of active articles (Spec 5.1, 7.7).

Plan B4 decisions 8, 9, 10; assumptions A8, A9 (maintenance mode: inactive = waiting).
"""

import pytest
from sqlalchemy import select

from factories import make_article, make_sort, tick_sorts
from reprintbuddy.bambuddy.client import BambuddyRejected
from reprintbuddy.bambuddy.fake import FakeBambuddy
from reprintbuddy.bambuddy.types import FilamentRequirement, Plate
from reprintbuddy.domain import catalog, pauses
from reprintbuddy.domain.errors import ConflictError
from reprintbuddy.domain.tracking import ArticleCheck, check_articles
from reprintbuddy.models import Article, ArticleFilament, ArticleFilamentSort, JournalEntry, utcnow


def journal(s, kind):
    return list(
        s.scalars(select(JournalEntry).where(JournalEntry.kind == kind).order_by(JournalEntry.id))
    )


def article(db, name="Template A", **fields):
    """Template A on file 1, plate 1; fingerprint and name as the fake holds them."""
    fields.setdefault("target_model", "A1")
    fields.setdefault("file_fingerprint", "v1")
    fields.setdefault("file_name", "Template A.gcode.3mf")
    fields.setdefault("plate_id", 1)
    fields.setdefault("library_file_id", 1)
    with db.begin() as s:
        return make_article(s, name=name, **fields).id


def state(db, article_id):
    with db() as s:
        a = s.get(Article, article_id)
        return a.paused, a.pause_reason


# ---- check_articles --------------------------------------------------------------------


async def test_clean_article_not_paused(db, fake_bambuddy):
    a = article(db)
    result = await check_articles(fake_bambuddy, db)
    assert result == ArticleCheck(None, frozenset())
    assert state(db, a) == (False, None)


async def test_file_missing_pauses(db, fake_bambuddy):
    a = article(db)
    fake_bambuddy.remove_file(1)
    result = await check_articles(fake_bambuddy, db)
    assert result.error is None
    assert state(db, a) == (True, "file_missing")
    with db() as s:
        (row,) = journal(s, "automation_paused")
        assert row.actor == "auto"
        assert row.details["reason"] == "file_missing"


async def test_file_changed_pauses(db, fake_bambuddy):
    a = article(db)
    fake_bambuddy.update_file(1, fingerprint="v2")
    await check_articles(fake_bambuddy, db)
    assert state(db, a) == (True, "file_changed")
    with db() as s:
        (row,) = journal(s, "automation_paused")
        assert row.details["old_fingerprint"] == "v1"
        assert row.details["new_fingerprint"] == "v2"


async def test_file_renamed_pauses(db, fake_bambuddy):
    a = article(db)
    fake_bambuddy.update_file(1, filename="Template A2.gcode.3mf")
    await check_articles(fake_bambuddy, db)
    assert state(db, a) == (True, "file_changed")
    with db() as s:
        (row,) = journal(s, "automation_paused")
        assert row.details["old_name"] == "Template A.gcode.3mf"
        assert row.details["new_name"] == "Template A2.gcode.3mf"


async def test_file_change_overrides_queue_reduced(db, fake_bambuddy):
    a = article(db, paused=True, pause_reason="queue_reduced")
    fake_bambuddy.update_file(1, fingerprint="v2")
    await check_articles(fake_bambuddy, db)
    assert state(db, a) == (True, "file_changed")
    with db.begin() as s:
        with pytest.raises(ConflictError) as err:
            pauses.lift_pause(s, a, "user")
        assert err.value.key == "articles.pause_needs_file"


@pytest.mark.parametrize(
    "fields",
    [
        {"target_model": None},
        {"target_model": None, "printer_id": 9},
        {"target_model": "X1"},
    ],
)
async def test_target_checks(db, fake_bambuddy, fields):
    a = article(db, **fields)
    await check_articles(fake_bambuddy, db)
    assert state(db, a) == (True, "no_target")


async def test_target_ok_is_not_paused(db, fake_bambuddy):
    a = article(db, target_model="A1")
    b = article(db, name="Other", target_model=None, printer_id=1, plate_id=2)
    result = await check_articles(fake_bambuddy, db)
    assert state(db, a) == (False, None)
    assert state(db, b) == (False, None)
    assert result.waiting_for_printer == frozenset()


async def test_inactive_printer_does_not_pause(db):
    fake = FakeBambuddy()
    fake.add_file("Template A.gcode.3mf", None)
    fake.add_printer("Printer 1", "A1", active=False)
    a = article(db, target_model=None, printer_id=1)
    result = await check_articles(fake, db)
    assert state(db, a) == (False, None)
    assert result.waiting_for_printer == frozenset({a})


async def test_model_all_inactive_does_not_pause(db):
    fake = FakeBambuddy()
    fake.add_file("Template A.gcode.3mf", None)
    fake.add_printer("Printer 1", "A1")
    fake.add_printer("Printer 3", "P1S", active=False)
    a = article(db, target_model="P1S")
    result = await check_articles(fake, db)
    assert state(db, a) == (False, None)
    assert result.waiting_for_printer == frozenset({a})


async def test_model_with_one_active_printer_is_not_waiting(db):
    fake = FakeBambuddy()
    fake.add_file("Template A.gcode.3mf", None)
    fake.add_printer("Printer 1", "A1", active=False)
    fake.add_printer("Printer 2", "A1")
    a = article(db, target_model="A1")
    result = await check_articles(fake, db)
    assert state(db, a) == (False, None)
    assert a not in result.waiting_for_printer


async def test_archived_not_checked(db, fake_bambuddy):
    a = article(db, archived_at=utcnow())
    fake_bambuddy.remove_file(1)
    fake_bambuddy.calls.clear()
    await check_articles(fake_bambuddy, db)
    assert state(db, a) == (False, None)
    assert "get_file" not in fake_bambuddy.calls


async def test_check_unavailable_changes_nothing(db, fake_bambuddy):
    a = article(db)
    fake_bambuddy.remove_file(1)
    fake_bambuddy.unavailable = True
    result = await check_articles(fake_bambuddy, db)
    assert result == ArticleCheck("unavailable", frozenset())
    assert state(db, a) == (False, None)


async def test_check_rejected_changes_nothing(db, fake_bambuddy):
    a = article(db)
    fake_bambuddy.remove_file(1)
    fake_bambuddy.fail_next("list_printers", BambuddyRejected("Forbidden", status=403))
    result = await check_articles(fake_bambuddy, db)
    assert result.error == "403: Forbidden"
    assert result.waiting_for_printer == frozenset()
    assert state(db, a) == (False, None)


# ---- confirm / relink ------------------------------------------------------------------


def add_filament(db, article_id, color="#000000"):
    """Slot 1 (PLA) with a filament group of two spool sorts; returns their ids (reordered first)."""
    with db.begin() as s:
        s.add(ArticleFilament(article_id=article_id, slot=1, grams=42.5, material="PLA", color_hex=color))
        s.flush()
        a = make_sort(s, brand="Brand A", color_hex=color)
        b = make_sort(s, brand="Brand B", color_hex=color)
        tick_sorts(s, article_id, 1, a, b, reordered=a)
        return a.id, b.id


def group(s, article_id):
    return sorted(
        (r.slot, r.sort_id, r.reordered)
        for r in s.scalars(select(ArticleFilamentSort).where(ArticleFilamentSort.article_id == article_id))
    )


async def test_confirm_file(db, fake_bambuddy):
    a = article(db, paused=True, pause_reason="file_changed")
    reordered, other = add_filament(db, a)
    fake_bambuddy.update_file(1, fingerprint="v2")
    file = await fake_bambuddy.get_file(1)
    with db.begin() as s:
        catalog.confirm_article_file(
            s, a, file=file, requirements=[FilamentRequirement(1, 50.0, "PLA", "#000000")],
            thumbnail=b"png", actor="user",
        )
    with db() as s:
        row = s.get(Article, a)
        assert row.file_fingerprint == "v2"
        assert not row.paused and row.pause_reason is None
        assert row.thumbnail == b"png"
        (f,) = s.scalars(select(ArticleFilament).where(ArticleFilament.article_id == a))
        assert f.grams == 50.0
        assert group(s, a) == [(1, reordered, True), (1, other, False)]  # same material and colour: kept
        assert len(journal(s, "article_file_confirmed")) == 1


async def test_confirm_drops_mapping_when_color_changes(db, fake_bambuddy):
    a = article(db, paused=True, pause_reason="file_changed")
    add_filament(db, a)
    file = await fake_bambuddy.get_file(1)
    with db.begin() as s:
        catalog.confirm_article_file(
            s, a, file=file, requirements=[FilamentRequirement(1, 50.0, "PLA", "#FFFFFF")],
            thumbnail=None, actor="user",
        )
    with db() as s:
        (f,) = s.scalars(select(ArticleFilament).where(ArticleFilament.article_id == a))
        assert f.color_hex == "#FFFFFF"
        assert group(s, a) == []  # another colour: the group is dropped


async def test_confirm_keeps_other_pause(db, fake_bambuddy):
    a = article(db, paused=True, pause_reason="two_failures")
    file = await fake_bambuddy.get_file(1)
    with db.begin() as s:
        catalog.confirm_article_file(
            s, a, file=file, requirements=[], thumbnail=None, actor="user"
        )
    assert state(db, a) == (True, "two_failures")


async def test_confirm_archived_refused(db, fake_bambuddy):
    a = article(db, archived_at=utcnow())
    file = await fake_bambuddy.get_file(1)
    with db.begin() as s:
        with pytest.raises(ConflictError) as err:
            catalog.confirm_article_file(
                s, a, file=file, requirements=[], thumbnail=None, actor="user"
            )
        assert err.value.key == "articles.edit_archived"


async def test_relink_active_article(db, fake_bambuddy):
    a = article(db, paused=True, pause_reason="file_missing")
    article(db, name="Other", library_file_id=2, plate_id=1)
    file_b = await fake_bambuddy.get_file(2)
    with db.begin() as s:
        with pytest.raises(ConflictError) as err:
            catalog.relink_article(
                s, a, file=file_b, plate=Plate(1, None), requirements=[],
                thumbnail=None, actor="user",
            )
        assert err.value.key == "articles.plate_taken"
    with db.begin() as s:
        catalog.relink_article(
            s, a, file=file_b, plate=Plate(2, None),
            requirements=[FilamentRequirement(1, 10.0, "PETG", "#111111")],
            thumbnail=b"x", actor="user",
        )
    with db() as s:
        row = s.get(Article, a)
        assert (row.library_file_id, row.plate_id) == (2, 2)
        assert row.file_name == "Template B.gcode.3mf"
        assert row.file_fingerprint == file_b.fingerprint
        assert not row.paused
        assert len(journal(s, "article_relinked")) == 1


async def test_relink_keeps_the_group_of_a_slot_with_the_same_material_and_colour(db, fake_bambuddy):
    """Spec 5.1 (v0.3.10): another file; a slot that is the same keeps its filament group."""
    a = article(db)
    reordered, other = add_filament(db, a)
    file_b = await fake_bambuddy.get_file(2)
    with db.begin() as s:
        assert catalog.slots_losing_group(s, a, [FilamentRequirement(1, 42.5, "PLA", "#000000")]) == []
        catalog.relink_article(
            s, a, file=file_b, plate=Plate(1, None),
            requirements=[FilamentRequirement(1, 42.5, "PLA", "#000000")], thumbnail=None, actor="user",
        )
    with db() as s:
        assert group(s, a) == [(1, reordered, True), (1, other, False)]


async def test_relink_drops_the_group_of_a_changed_slot_only(db, fake_bambuddy):
    a = article(db)
    reordered, other = add_filament(db, a)
    with db.begin() as s:  # a second slot with a group of its own
        s.add(ArticleFilament(article_id=a, slot=2, grams=5.0, material="PETG", color_hex="#FF0000"))
        s.flush()
        red = make_sort(s, brand="Brand C", material="PETG", color_hex="#FF0000")
        tick_sorts(s, a, 2, red, reordered=red)
        red_id = red.id
    file_b = await fake_bambuddy.get_file(2)
    new = [FilamentRequirement(1, 42.5, "PLA", "#000000"), FilamentRequirement(2, 5.0, "PETG", "#00FF00")]
    with db.begin() as s:
        assert catalog.slots_losing_group(s, a, new) == [2]
        catalog.relink_article(s, a, file=file_b, plate=Plate(1, None), requirements=new, thumbnail=None, actor="user")
    with db() as s:
        assert group(s, a) == [(1, reordered, True), (1, other, False)]
        assert red_id not in {sort for _, sort, _ in group(s, a)}


async def test_slots_losing_group_ignores_slots_nobody_ticked(db, fake_bambuddy):
    """No hint for a slot that never had a group, nor for a slot the new file no longer has."""
    a = article(db)
    add_filament(db, a)  # slot 1 ticked
    with db.begin() as s:
        s.add(ArticleFilament(article_id=a, slot=2, grams=5.0, material="PETG", color_hex="#FF0000"))
    with db() as s:
        new = [FilamentRequirement(1, 1.0, "PLA", "#000000"), FilamentRequirement(2, 5.0, "ABS", "#FF0000")]
        assert catalog.slots_losing_group(s, a, new) == []  # slot 2 changed, but it had no group
        # the new file has no slot 1: its group goes, but there is nothing to tick again
        assert catalog.slots_losing_group(s, a, [FilamentRequirement(2, 5.0, "ABS", "#FF0000")]) == []


async def test_relink_archived_refused(db, fake_bambuddy):
    a = article(db, archived_at=utcnow())
    file_b = await fake_bambuddy.get_file(2)
    with db.begin() as s:
        with pytest.raises(ConflictError) as err:
            catalog.relink_article(
                s, a, file=file_b, plate=Plate(1, None), requirements=[],
                thumbnail=None, actor="user",
            )
        assert err.value.key == "articles.edit_archived"


def test_lift_no_target_with_printer_id(db):
    with db.begin() as s:
        a = make_article(s, paused=True, pause_reason="no_target", printer_id=1).id
        pauses.lift_pause(s, a, "user")
        assert not s.get(Article, a).paused


# ---- fix round 1: re-check before pausing ----------------------------------------------


class Racing:
    """A client whose `get_file` first lets something else change the article (a web POST)."""

    def __init__(self, inner, change):
        self._inner, self._change, self._done = inner, change, False

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def get_file(self, file_id):
        result = await self._inner.get_file(file_id)
        if not self._done:
            self._done = True
            self._change()
        return result


async def test_concurrent_confirm_is_not_overwritten(db, fake_bambuddy):
    a = article(db)
    fake_bambuddy.update_file(1, fingerprint="v2")

    def confirm():
        with db.begin() as s:
            s.get(Article, a).file_fingerprint = "v2"

    await check_articles(Racing(fake_bambuddy, confirm), db)
    assert state(db, a) == (False, None)
    with db() as s:
        assert journal(s, "automation_paused") == []


async def test_concurrent_target_change_is_not_overwritten(db, fake_bambuddy):
    a = article(db, target_model="X1")

    def choose():
        with db.begin() as s:
            s.get(Article, a).target_model = "A1"

    await check_articles(Racing(fake_bambuddy, choose), db)
    assert state(db, a) == (False, None)


async def test_concurrent_archive_is_not_overwritten(db, fake_bambuddy):
    a = article(db)
    fake_bambuddy.remove_file(1)

    def archive():
        with db.begin() as s:
            s.get(Article, a).archived_at = utcnow()

    await check_articles(Racing(fake_bambuddy, archive), db)
    assert state(db, a) == (False, None)


async def test_paused_or_paused_by_verdict_is_not_waiting(db):
    fake = FakeBambuddy()
    fake.add_file("Template A.gcode.3mf", None)
    fake.add_printer("Printer 1", "A1", active=False)
    paused = article(db, paused=True, pause_reason="two_failures")
    changed = article(db, name="Other", plate_id=2, file_fingerprint="old")
    result = await check_articles(fake, db)
    assert result.waiting_for_printer == frozenset()
    assert state(db, changed) == (True, "file_changed")
    assert state(db, paused) == (True, "two_failures")


async def test_confirm_with_other_file_refused(db, fake_bambuddy):
    a = article(db)
    file_b = await fake_bambuddy.get_file(2)
    with db.begin() as s:
        with pytest.raises(ConflictError) as err:
            catalog.confirm_article_file(
                s, a, file=file_b, requirements=[], thumbnail=None, actor="user"
            )
        assert err.value.key == "articles.confirm_other_file"
