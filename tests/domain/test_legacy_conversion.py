"""The brandless sorts of 0006 hand over their settings and open marks once (`filament.convert_legacy`).

Migration 0007 marks them legacy; the first spool list afterwards (any `sync_sorts`) moves what they
hold to the spool sort with the most of their spools, by the rule they were built with (delta E 7
within one material and subtype). See test_migration_0007_spool_sorts for the migration itself.
"""

from datetime import date, datetime, timedelta

from sqlalchemy import select

from factories import make_filament_settings, make_legacy_sort, make_snapshot, make_sort
from reprintbuddy.bambuddy.fake import FakeBambuddy
from reprintbuddy.domain import filament
from reprintbuddy.models import FilamentSettings, FilamentSnapshot, FilamentSort, JournalEntry, ReorderMark

KIND = "filament_sorts_converted"
DAY = date(2026, 11, 6)


def journal(s):
    return s.scalars(select(JournalEntry).where(JournalEntry.kind == KIND).order_by(JournalEntry.id)).all()


def mark(s, sort_id, qty=1, *, days_ago=1, received=False):
    at = datetime(2026, 11, 6, 12, 0) - timedelta(days=days_ago)
    row = ReorderMark(item_type="filament", item_id=sort_id, qty=qty, marked_at=at,
                      received_at=at + timedelta(hours=1) if received else None)
    s.add(row)
    s.flush()
    return row


def open_marks(s):
    return s.scalars(
        select(ReorderMark).where(ReorderMark.received_at.is_(None)).order_by(ReorderMark.id)
    ).all()


def test_the_sort_with_most_spools_takes_settings_and_mark(db):
    fake = FakeBambuddy()
    spools = [
        fake.add_spool("PLA", brand="Brand A", color_hex="#000000"),
        fake.add_spool("PLA", brand="Brand B", color_hex="#1A1A1A"),  # 5.5 from the old colour: a member
        fake.add_spool("PLA", brand="Brand B", color_hex="#1A1A1A"),
        fake.add_spool("PLA", brand="Brand C", color_hex="#333333"),  # 13.4: not a member
        fake.add_spool("PLA", brand="Brand C", color_hex="#333333"),
        fake.add_spool("PLA", brand="Brand C", color_hex="#333333"),
        fake.add_spool("PETG", brand="Brand D", color_hex="#000000"),  # another material
        fake.add_spool("PETG", brand="Brand D", color_hex="#000000"),
        fake.add_spool("PETG", brand="Brand D", color_hex="#000000"),
    ]
    with db.begin() as s:
        old = make_legacy_sort(s)
        make_filament_settings(s, old.id, lead_time_days=4, supplier="Example Supplier")
        old_mark = mark(s, old.id, 2)
        ids = filament.sync_sorts(s, spools)
        target = ids[spools[1].id]
        assert s.get(FilamentSettings, old.id) is None
        settings = s.get(FilamentSettings, target)
        assert (settings.lead_time_days, settings.supplier) == (4, "Example Supplier")
        assert (old_mark.item_id, old_mark.qty, old_mark.received_at) == (target, 2, None)
        (row,) = journal(s)
        assert (row.actor, row.qty) == ("auto", 0)
        assert row.details == {
            "from": old.id, "from_label": "PLA Black", "into": target, "into_label": "Brand B PLA #1A1A1A",
            "settings": "moved", "marks": "moved",
        }
        assert "PLA Black" in row.note and "Brand B PLA #1A1A1A" in row.note
        # the old sort stays as a hidden row
        assert s.get(FilamentSort, old.id).legacy is True
        known, _ = filament.known_sorts(s, spools)
        assert old.id not in known and target in known
        assert len(journal(s)) == 1  # the next spool list converts nothing more


def test_a_tie_goes_to_the_lower_id(db):
    fake = FakeBambuddy()
    with db.begin() as s:
        old = make_legacy_sort(s)
        make_filament_settings(s, old.id, lead_time_days=4)
        lower = make_sort(s, brand="Brand B")  # exists before the spools are read: the lower id
        first = fake.add_spool("PLA", brand="Brand A")  # the lower spool id, yet the higher sort id
        second = fake.add_spool("PLA", brand="Brand B")
        ids = filament.sync_sorts(s, [first, second])
        assert ids[second.id] == lower.id < ids[first.id]
        assert s.get(FilamentSettings, lower.id).lead_time_days == 4
        assert s.get(FilamentSettings, ids[first.id]) is None
        assert journal(s)[0].details["into"] == lower.id


def test_without_a_living_spool_settings_are_dropped_and_the_mark_closed(db):
    fake = FakeBambuddy()
    with db.begin() as s:
        old = make_legacy_sort(s, subtype="Matte")
        make_filament_settings(s, old.id, lead_time_days=4, pack_grams=750)
        old_mark = mark(s, old.id, 2)
        spools = [
            fake.add_spool("PLA", brand="Brand A"),  # no subtype: not a spool of a Matte sort
            fake.add_spool("PETG", brand="Brand A", subtype="Matte"),
        ]
        filament.sync_sorts(s, spools)
        assert s.scalars(select(FilamentSettings)).all() == []
        assert old_mark.received_at is not None and old_mark.item_id == old.id
        (row,) = journal(s)
        assert row.details["into"] is None and row.details["into_label"] is None
        assert (row.details["settings"], row.details["marks"]) == ("dropped", "closed")
        dropped = row.details["dropped_settings"]
        assert (dropped["lead_time_days"], dropped["pack_grams"], dropped["supplier"]) == (4, 750, None)


def test_settings_of_the_target_win(db):
    fake = FakeBambuddy()
    with db.begin() as s:
        old = make_legacy_sort(s)
        make_filament_settings(s, old.id, lead_time_days=4)
        target = make_sort(s, brand="Brand A")
        make_filament_settings(s, target.id, lead_time_days=9)
        filament.sync_sorts(s, [fake.add_spool("PLA", brand="Brand A")])
        assert s.get(FilamentSettings, old.id) is None
        assert s.get(FilamentSettings, target.id).lead_time_days == 9
        (row,) = journal(s)
        assert row.details["settings"] == "dropped"
        assert row.details["dropped_settings"]["lead_time_days"] == 4
        assert "marks" not in row.details


def test_two_open_marks_become_one(db):
    fake = FakeBambuddy()
    with db.begin() as s:
        old = make_legacy_sort(s)
        target = make_sort(s, brand="Brand A")
        make_filament_settings(s, target.id)
        earlier = mark(s, old.id, 2, days_ago=5)
        mark(s, target.id, 1, days_ago=2)
        closed = mark(s, old.id, 4, days_ago=20, received=True)  # received long ago: stays where it is
        filament.sync_sorts(s, [fake.add_spool("PLA", brand="Brand A")])
        (left,) = open_marks(s)
        assert (left.item_id, left.qty, left.marked_at) == (target.id, 3, earlier.marked_at)
        assert closed.item_id == old.id and closed.received_at is not None
        assert journal(s)[0].details["marks"] == "moved"


def test_a_second_run_does_nothing(db):
    fake = FakeBambuddy()
    spools = [fake.add_spool("PLA", brand="Brand A")]
    with db.begin() as s:
        old = make_legacy_sort(s)
        make_filament_settings(s, old.id, lead_time_days=4)
        mark(s, old.id, 2)
        filament.sync_sorts(s, spools)
    with db.begin() as s:
        filament.sync_sorts(s, spools)
        filament.sync_sorts(s, [])
        assert len(journal(s)) == 1
        assert len(open_marks(s)) == 1


def test_convert_legacy_counts_the_sorts_it_converted(db):
    fake = FakeBambuddy()
    with db.begin() as s:
        with_settings = make_legacy_sort(s)
        make_filament_settings(s, with_settings.id)
        with_mark = make_legacy_sort(s, material="PETG")
        mark(s, with_mark.id)
        make_legacy_sort(s, material="ABS")  # holds nothing
        target = make_sort(s, brand="Brand A")
        spool = fake.add_spool("PLA", brand="Brand A")
        assert filament.convert_legacy(s, [spool], {spool.id: target.id}) == 2
        assert filament.convert_legacy(s, [spool], {spool.id: target.id}) == 0
        assert len(journal(s)) == 2


def test_a_legacy_sort_holding_nothing_writes_no_journal(db):
    fake = FakeBambuddy()
    with db.begin() as s:
        old = make_legacy_sort(s)
        mark(s, old.id, received=True)  # a received mark is history, not something to move
        filament.sync_sorts(s, [fake.add_spool("PLA", brand="Brand A")])
        assert journal(s) == []
        assert s.get(FilamentSort, old.id).legacy is True


def test_each_spool_belongs_to_its_nearest_legacy_sort(db):
    fake = FakeBambuddy()
    with db.begin() as s:
        black = make_legacy_sort(s, color_hex="#000000")
        dark = make_legacy_sort(s, color_hex="#101010", color_name="Dark")
        make_filament_settings(s, black.id, lead_time_days=1)
        make_filament_settings(s, dark.id, lead_time_days=2)
        a = fake.add_spool("PLA", brand="Brand A", color_hex="#000000")
        b = fake.add_spool("PLA", brand="Brand B", color_hex="#0C0C0C")  # within 7 of both, nearer to #101010
        ids = filament.sync_sorts(s, [a, b])
        assert s.get(FilamentSettings, ids[a.id]).lead_time_days == 1
        assert s.get(FilamentSettings, ids[b.id]).lead_time_days == 2


def test_daily_rows_of_listed_spools_leave_the_legacy_sort(db):
    fake = FakeBambuddy()
    spool = fake.add_spool("PLA", brand="Brand A")
    with db.begin() as s:
        old = make_legacy_sort(s)
        make_snapshot(s, DAY - timedelta(days=1), spool.id, old.id, 600.0)
        make_snapshot(s, DAY - timedelta(days=1), 99, old.id, 400.0)  # a spool that is gone
        ids = filament.sync_sorts(s, [spool])
        rows = {r.spool_id: r.spool_filament_id for r in s.scalars(select(FilamentSnapshot))}
        assert rows == {spool.id: ids[spool.id], 99: old.id}
        stock, day = filament.stock_from_snapshots(s)
        assert day == DAY - timedelta(days=1)
        assert {k: v.stock_g for k, v in stock.items()} == {ids[spool.id]: 600.0}


# ---- review findings of v0.3.7 part A ------------------------------------------------------------


def test_an_empty_spool_list_converts_nothing(db):
    """Bambuddy answers with no spools (a fresh install, another spool mode): nothing is dropped or
    closed, the legacy sort waits for a list with spools."""
    with db.begin() as s:
        old = make_legacy_sort(s)
        make_filament_settings(s, old.id, lead_time_days=4)
        mark(s, old.id, 2)
        filament.sync_sorts(s, [])
        filament.known_sorts(s, [])
        assert s.get(FilamentSettings, old.id) is not None
        assert [m.item_id for m in open_marks(s)] == [old.id]
        assert journal(s) == []


def test_a_mark_moved_onto_other_settings_is_counted_in_their_order_units(db):
    """The old sort ordered 4 x 250 g; the target already has settings with 1000 g packs: the moved
    mark counts 1 x 1000 g, and joined with the target's own open mark 2 x 1000 g."""
    fake = FakeBambuddy()
    spool = fake.add_spool("PLA", brand="Brand A", color_hex="#000000")
    with db.begin() as s:
        old = make_legacy_sort(s)
        make_filament_settings(s, old.id, pack_grams=250)
        mark(s, old.id, 4, days_ago=2)
        target = make_sort(s, brand="Brand A")
        make_filament_settings(s, target.id, pack_grams=1000)
        mark(s, target.id, 1, days_ago=1)
        filament.sync_sorts(s, [spool])
        (joined,) = open_marks(s)
        assert (joined.item_id, joined.qty) == (target.id, 2)


def test_a_moved_mark_rounds_up_to_whole_order_units(db):
    fake = FakeBambuddy()
    spool = fake.add_spool("PLA", brand="Brand A", color_hex="#000000")
    with db.begin() as s:
        old = make_legacy_sort(s)
        make_filament_settings(s, old.id, pack_grams=750)
        mark(s, old.id, 1)
        target = make_sort(s, brand="Brand A")
        make_filament_settings(s, target.id, pack_grams=1000)
        filament.sync_sorts(s, [spool])
        (moved,) = open_marks(s)
        assert (moved.item_id, moved.qty) == (target.id, 1)


def test_a_mark_without_settings_counts_the_default_unit_when_moved(db):
    """Without settings a mark counts 1000 g units (as "on its way" shows it); moved onto 250 g units,
    it keeps its grams: 2 x 1000 g become 8 x 250 g."""
    fake = FakeBambuddy()
    spool = fake.add_spool("PLA", brand="Brand A", color_hex="#000000")
    with db.begin() as s:
        old = make_legacy_sort(s)
        mark(s, old.id, 2)
        target = make_sort(s, brand="Brand A")
        make_filament_settings(s, target.id, pack_grams=250)
        filament.sync_sorts(s, [spool])
        (moved,) = open_marks(s)
        assert (moved.item_id, moved.qty) == (target.id, 8)
