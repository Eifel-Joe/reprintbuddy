"""Which spool sort a spool belongs to: brand, material, subtype and colour value, exactly (Spec 9.1).

Nothing is merged any more: two brands of the same colour are two spool sorts, and so are two
colours a few delta E apart. Which sorts an article may use is ticked per slot (test_filament_groups).
"""

import dataclasses
from datetime import date, datetime, timedelta

from sqlalchemy import func, select

from factories import make_filament_settings, make_legacy_sort, make_snapshot, make_sort
from reprintbuddy.bambuddy.fake import FakeBambuddy
from reprintbuddy.domain import filament, reorder
from reprintbuddy.models import FilamentSnapshot, FilamentSort, ReorderMark

BLACK, NEAR_BLACK = "#000000", "#1A1A1A"  # 5.5 apart: similar, but not the same colour value
NOW = datetime(2026, 11, 6, 12, 0)
DAY = date(2026, 11, 6)


def sort_count(s):
    return s.scalar(select(func.count()).select_from(FilamentSort))


# ---- key and label -----------------------------------------------------------------------------


def test_key_and_label_carry_the_brand():
    assert filament.sort_key("Brand A", "PLA", None, "#000000") == "brand a|pla||#000000"
    assert filament.sort_key(" Brand A ", " pla ", " Matte", " #AABBCC ") == "brand a|pla|matte|#aabbcc"
    assert filament.sort_key(None, "PLA", None, None) == "|pla||"
    assert filament.sort_label("Brand A", "PLA", None, "Black", "#000000") == "Brand A PLA Black"
    assert filament.sort_label("Brand A", "PLA", "Matte", None, "#000000") == "Brand A PLA Matte #000000"
    assert filament.sort_label(None, "PLA", None, "  ", "#000000") == "PLA #000000"
    assert filament.sort_label("Brand A", "PLA", "Matte", "matte", None) == "Brand A PLA Matte"


def test_alpha_is_part_of_the_key():
    opaque = filament.sort_key("Brand A", "PETG", None, "#000000")
    clear = filament.sort_key("Brand A", "PETG", None, "#00000000")
    half = filament.sort_key("Brand A", "PETG", None, "#00000080")
    assert len({opaque, clear, half}) == 3
    assert filament.sort_label("Brand A", "PETG", None, None, "#00000000") == "Brand A PETG transparent"
    assert filament.sort_label("Brand A", "PETG", None, None, "#00000080") == "Brand A PETG #000000"


def test_two_brands_of_one_colour_are_two_sorts(db):
    fake = FakeBambuddy()
    a = fake.add_spool("PETG", brand="Brand A", color_hex=BLACK, color_name="Black")
    b = fake.add_spool("PETG", brand="Brand B", color_hex=BLACK, color_name="Black")
    a2 = fake.add_spool("PETG", brand="brand a ", color_hex=BLACK)  # the same brand, written otherwise
    with db.begin() as s:
        ids = filament.sync_sorts(s, [a, b, a2])
        assert ids[a.id] == ids[a2.id] != ids[b.id] and sort_count(s) == 2
        assert s.get(FilamentSort, ids[a.id]).label == "Brand A PETG Black"
        assert s.get(FilamentSort, ids[b.id]).label == "Brand B PETG Black"
        assert s.get(FilamentSort, ids[b.id]).brand == "Brand B"


def test_similar_colours_are_not_merged(db):
    fake = FakeBambuddy()
    a = fake.add_spool("PLA", brand="Brand A", color_hex="#AABBCC")
    b = fake.add_spool("PLA", brand="Brand A", color_hex="#ABBCCD")  # under 1 apart
    c = fake.add_spool("PLA", brand="Brand A", color_hex="#aabbcc")  # the same value, other case
    with db.begin() as s:
        ids = filament.sync_sorts(s, [a, b, c])
        assert ids[a.id] == ids[c.id] != ids[b.id] and sort_count(s) == 2


def test_material_and_subtype_split(db):
    fake = FakeBambuddy()
    spools = [
        fake.add_spool("PLA", brand="Brand A"),
        fake.add_spool("PLA", brand="Brand A", subtype="Matte"),
        fake.add_spool("PETG", brand="Brand A"),
        fake.add_spool("pla", brand="Brand A"),  # the same material in other case
        fake.add_spool("PLA", brand="Brand A", subtype=" matte "),
    ]
    with db.begin() as s:
        ids = filament.sync_sorts(s, spools)
        assert ids[spools[0].id] == ids[spools[3].id]
        assert ids[spools[1].id] == ids[spools[4].id]
        assert len(set(ids.values())) == 3


def test_transparent_spools_keep_to_their_colour_value(db):
    fake = FakeBambuddy()
    black = fake.add_spool("PETG", brand="Brand A", color_hex="#000000")
    clear = fake.add_spool("PETG", brand="Brand A", color_hex="#00000000")
    clear_again = fake.add_spool("PETG", brand="Brand A", color_hex="#00000000")
    half = fake.add_spool("PETG", brand="Brand A", color_hex="#00000080")
    with db.begin() as s:
        ids = filament.sync_sorts(s, [black, clear, clear_again, half])
        assert ids[clear.id] == ids[clear_again.id]
        assert len({ids[black.id], ids[clear.id], ids[half.id]}) == 3
        assert s.get(FilamentSort, ids[clear.id]).label == "Brand A PETG transparent"


def test_spools_without_a_colour(db):
    fake = FakeBambuddy()
    none1 = fake.add_spool("PLA", brand="Brand A", color_hex=None, color_name="Natural")
    none2 = fake.add_spool("PLA", brand="Brand A", color_hex=None)
    black = fake.add_spool("PLA", brand="Brand A", color_hex=BLACK)
    with db.begin() as s:
        ids = filament.sync_sorts(s, [none1, none2, black])
        assert ids[none1.id] == ids[none2.id] != ids[black.id]
        assert s.get(FilamentSort, ids[none1.id]).label == "Brand A PLA Natural"


def test_colour_name_is_not_in_the_key_and_follows_the_lowest_named_spool(db):
    fake = FakeBambuddy()
    a = fake.add_spool("PLA", brand="Brand A", color_name="Black")
    b = fake.add_spool("PLA", brand="Brand A", color_name="Jet")
    with db.begin() as s:
        ids = filament.sync_sorts(s, [b, a])
        assert ids[a.id] == ids[b.id] and sort_count(s) == 1
        sort = s.get(FilamentSort, ids[a.id])
        assert (sort.color_name, sort.label) == ("Black", "Brand A PLA Black")


def test_an_unnamed_spool_never_erases_a_known_name(db):
    fake = FakeBambuddy()
    named = fake.add_spool("PLA", brand="Brand A", color_name="Black")
    unnamed = fake.add_spool("PLA", brand="Brand A")
    for spools in ([named, unnamed], [unnamed, named], [unnamed]):  # the named one may be gone
        with db.begin() as s:
            ids = filament.sync_sorts(s, spools)
            sort = s.get(FilamentSort, ids[unnamed.id])
            assert (sort.label, sort.color_name) == ("Brand A PLA Black", "Black")


def test_a_second_call_and_resolve_agree(db):
    fake = FakeBambuddy()
    spools = [
        fake.add_spool("PLA", brand="Brand A", color_hex=h) for h in ("#000000", "#1A1A1A", "#00000000")
    ] + [fake.add_spool("PLA", brand="Brand B")]
    with db.begin() as s:
        first = filament.sync_sorts(s, spools)
    with db.begin() as s:
        assert filament.sync_sorts(s, spools) == first == filament.resolve_sorts(s, spools)
        assert sort_count(s) == 4


def test_resolve_sorts_writes_nothing(db):
    fake = FakeBambuddy()
    a = fake.add_spool("PLA", brand="Brand A")
    b = fake.add_spool("PLA", brand="Brand B")
    with db.begin() as s:
        assert filament.resolve_sorts(s, [a, b]) == {a.id: None, b.id: None}
        assert sort_count(s) == 0
        sort_id = filament.sync_sorts(s, [a])[a.id]
        assert filament.resolve_sorts(s, [a, b]) == {a.id: sort_id, b.id: None}
        assert sort_count(s) == 1


def test_a_spool_never_lands_on_a_legacy_sort(db):
    fake = FakeBambuddy()
    with db.begin() as s:
        old = make_legacy_sort(s)
        spool = fake.add_spool("PLA", brand=None, color_hex="#000000")
        assert filament.resolve_sorts(s, [spool]) == {spool.id: None}
        sort_id = filament.sync_sorts(s, [spool])[spool.id]
        assert sort_id != old.id and not s.get(FilamentSort, sort_id).legacy


# ---- stock -------------------------------------------------------------------------------------


def test_stock_counts_each_brand_apart(db):
    fake = FakeBambuddy()
    spools = [
        fake.add_spool("PETG", brand="Brand A", remaining_g=400.0),
        fake.add_spool("PETG", brand="Brand A", remaining_g=100.0),
        fake.add_spool("PETG", brand="Brand B", remaining_g=250.0),
    ]
    with db.begin() as s:
        stock = filament.stock_by_sort(s, spools)
        assert sorted((e.label, e.stock_g, e.spools) for e in stock.values()) == [
            ("Brand A PETG #000000", 500.0, 2),
            ("Brand B PETG #000000", 250.0, 1),
        ]


# ---- the daily rows follow the spool -----------------------------------------------------------


def test_daily_rows_follow_the_spool_to_its_sort_also_earlier_days(db):
    """A spool whose brand is corrected in Bambuddy takes its whole history to its new sort."""
    fake = FakeBambuddy()
    spool = fake.add_spool("PLA", brand="Brand A", remaining_g=800.0)
    other = fake.add_spool("PLA", brand="Brand A", remaining_g=500.0)
    with db.begin() as s:
        filament.record_snapshot(s, [spool, other], DAY - timedelta(days=2))
        filament.record_snapshot(s, [spool, other], DAY - timedelta(days=1))
        old_id = filament.resolve_sorts(s, [spool])[spool.id]
    moved = dataclasses.replace(spool, brand="Brand B")  # the same spool, its brand corrected
    with db.begin() as s:
        new_id = filament.sync_sorts(s, [moved, other])[spool.id]
        assert new_id != old_id
        rows = s.scalars(select(FilamentSnapshot).where(FilamentSnapshot.spool_id == spool.id)).all()
        assert len(rows) == 2 and {r.spool_filament_id for r in rows} == {new_id}
        rest = s.scalars(select(FilamentSnapshot).where(FilamentSnapshot.spool_id == other.id)).all()
        assert {r.spool_filament_id for r in rest} == {old_id}  # the other spool stays
        assert filament.consumption(s, new_id, DAY) == (0.0, 1)  # its history counts for the new sort


def test_rows_of_a_spool_not_listed_stay(db):
    with db.begin() as s:
        sort = make_sort(s)
        make_snapshot(s, DAY, 99, sort.id, 300.0)
        fake = FakeBambuddy()
        filament.sync_sorts(s, [fake.add_spool("PLA", brand="Brand B")])
        assert s.scalar(select(FilamentSnapshot.spool_filament_id)) == sort.id


def test_rows_of_a_legacy_sort_count_for_nothing(db):
    with db.begin() as s:
        old = make_legacy_sort(s)
        sort = make_sort(s)
        make_snapshot(s, DAY, 1, old.id, 700.0)
        make_snapshot(s, DAY, 2, sort.id, 300.0)
        stock, day = filament.stock_from_snapshots(s)
        assert day == DAY and list(stock) == [sort.id] and stock[sort.id].stock_g == 300.0
        known, _ = filament.known_sorts(s, None)
        assert old.id not in known and known[sort.id].stock_g == 300.0


# ---- an "ordered" mark closes only with spools of its own spool sort (Spec 9.4) -----------------


def _ordered(db, old):
    with db.begin() as s:
        sort_id = filament.sync_sorts(s, [old])[old.id]
        make_filament_settings(s, sort_id, pack_grams=1000)
        mark = reorder.mark_ordered(s, "filament", sort_id, 1)
        mark.marked_at = NOW - timedelta(days=1)


def test_filament_mark_not_closed_by_another_brand_of_the_same_colour(db):
    fake = FakeBambuddy()
    old = fake.add_spool("PETG", brand="Brand A", created_at=NOW - timedelta(days=9))
    _ordered(db, old)
    other = fake.add_spool("PETG", brand="Brand B", created_at=NOW - timedelta(hours=2))
    with db.begin() as s:
        assert reorder.close_arrived_marks(s, [old, other], NOW) == 0
        assert s.query(ReorderMark).one().received_at is None
    same = fake.add_spool("PETG", brand="Brand A", created_at=NOW - timedelta(hours=1))
    with db.begin() as s:
        filament.sync_sorts(s, [old, other, same])
        assert reorder.close_arrived_marks(s, [old, other, same], NOW) == 1
        assert s.query(ReorderMark).one().received_at == NOW


def test_filament_mark_not_closed_by_a_similar_colour(db):
    fake = FakeBambuddy()
    old = fake.add_spool("PETG", brand="Brand A", created_at=NOW - timedelta(days=9))
    _ordered(db, old)
    near = fake.add_spool("PETG", brand="Brand A", color_hex=NEAR_BLACK, created_at=NOW - timedelta(hours=2))
    with db.begin() as s:
        assert reorder.close_arrived_marks(s, [old, near], NOW) == 0
