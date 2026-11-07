import dataclasses
from datetime import date, timedelta

from sqlalchemy import func, select

from factories import make_snapshot, make_sort
from reprintbuddy.bambuddy.fake import FakeBambuddy
from reprintbuddy.domain import filament
from reprintbuddy.models import FilamentSnapshot, FilamentSort

DAY = date(2026, 3, 1)


def test_sort_key_and_label():  # Spec 9.1: no brand
    assert filament.sort_key("PLA", None, "#000000") == "pla||#000000"
    assert filament.sort_label("PLA", None, "Black", "#000000") == "PLA Black"
    assert filament.sort_label("PLA", None, None, "#000000") == "PLA #000000"
    assert filament.sort_label("PLA", None, "  ", "#000000") == "PLA #000000"
    assert filament.sort_label("PLA", "Matte", "Matte", None) == "PLA Matte"
    assert filament.sort_label("PLA", "Matte", "Black", "#000000") == "PLA Matte Black"


def test_sort_key_normalized():
    assert filament.sort_key(" pla ", " Matte", " #AABBCC ") == "pla|matte|#aabbcc"


def test_color_name_not_in_key(db):
    fake = FakeBambuddy()
    a = fake.add_spool("PLA", color_name="Black")
    with db.begin() as s:
        ids = filament.sync_sorts(s, [a])
        b = dataclasses.replace(a, color_name="Schwarz")
        ids2 = filament.sync_sorts(s, [b])
        assert ids == ids2
        assert s.scalar(select(func.count()).select_from(FilamentSort)) == 1
        sort = s.get(FilamentSort, ids2[b.id])
        assert sort.label == "PLA Schwarz"
        assert sort.color_name == "Schwarz"


def test_brand_does_not_split_a_sort(db):  # Spec 9.1: PETG is PETG, whoever made it
    fake = FakeBambuddy()
    spools = [
        fake.add_spool("PLA", brand="Example Brand"),
        fake.add_spool("PLA", brand="Other Brand"),
        fake.add_spool("PLA", brand=None),
    ]
    with db.begin() as s:
        ids = filament.sync_sorts(s, spools)
        assert len(set(ids.values())) == 1
        (sort,) = s.scalars(select(FilamentSort)).all()
        assert sort.brand is None and "Brand" not in sort.label


def test_subtype_and_material_still_split(db):
    fake = FakeBambuddy()
    spools = [
        fake.add_spool("PLA"),
        fake.add_spool("PLA", subtype="Matte"),
        fake.add_spool("PETG"),
        fake.add_spool("pla"),  # the same material in other case
        fake.add_spool("PLA", subtype=" matte "),
    ]
    with db.begin() as s:
        ids = filament.sync_sorts(s, spools)
        assert ids[spools[0].id] == ids[spools[3].id]
        assert ids[spools[1].id] == ids[spools[4].id]
        assert len(set(ids.values())) == 3


def _two_spools():
    fake = FakeBambuddy()
    return fake, [
        fake.add_spool("PLA", remaining_g=800.0),
        fake.add_spool("PLA", remaining_g=300.0),
    ]


def test_stock_sums_spools(db):  # Spec 9.1
    _, spools = _two_spools()
    with db.begin() as s:
        stock = filament.stock_by_sort(s, spools)
        (entry,) = stock.values()
        assert entry.stock_g == 1100.0
        assert entry.spools == 2
        assert entry.label == "PLA #000000"
        assert entry.material == "PLA"


def test_archived_spools_not_counted(db):
    fake, spools = _two_spools()
    spools.append(fake.add_spool("PLA", remaining_g=500.0, archived=True))
    with db.begin() as s:
        (entry,) = filament.stock_by_sort(s, spools).values()
        assert entry.stock_g == 1100.0
        assert entry.spools == 2


def test_snapshot_upsert_same_day(db):
    fake, spools = _two_spools()
    with db.begin() as s:
        assert filament.record_snapshot(s, spools, DAY) == 2
    changed = [dataclass_replace(spools[0], 700.0), spools[1]]
    with db.begin() as s:
        assert filament.record_snapshot(s, changed, DAY) == 2
    with db.begin() as s:
        rows = list(s.scalars(select(FilamentSnapshot).order_by(FilamentSnapshot.spool_id)))
        assert [r.remaining_g for r in rows] == [700.0, 300.0]


def dataclass_replace(spool, grams):
    return dataclasses.replace(spool, remaining_g=grams)


def test_stock_from_snapshots_latest_day(db):
    fake, spools = _two_spools()
    with db.begin() as s:
        assert filament.stock_from_snapshots(s) == ({}, None)
        filament.record_snapshot(s, spools, DAY)
        filament.record_snapshot(s, [dataclass_replace(spools[0], 500.0)], DAY + timedelta(days=1))
    with db.begin() as s:
        stock, day = filament.stock_from_snapshots(s)
        assert day == DAY + timedelta(days=1)
        (entry,) = stock.values()
        assert (entry.stock_g, entry.spools) == (500.0, 1)
        assert entry.label == "PLA #000000"


def test_consumption_window(db):  # Entscheidung 7
    today = date(2026, 5, 1)
    with db.begin() as s:
        sort = make_sort(s).id
        for back in range(40):
            make_snapshot(s, today - timedelta(days=back), 1, sort, 1000.0 - 10.0 * (39 - back))
    with db.begin() as s:
        assert filament.consumption(s, sort, today) == (10.0, 30)


def test_known_sorts(db):
    fake, spools = _two_spools()
    with db.begin() as s:
        assert filament.known_sorts(s, None) == (None, None)
        assert filament.known_sorts(s, []) == ({}, None)
        other = make_sort(s, material="PETG", color_hex="#FFFFFF", color_name="White").id
        stock, day = filament.known_sorts(s, spools)
        assert day is None
        assert stock[other].stock_g == 0.0 and stock[other].spools == 0
        assert sorted(e.stock_g for e in stock.values()) == [0.0, 1100.0]
        filament.record_snapshot(s, spools, DAY)
        stock, day = filament.known_sorts(s, None)
        assert day == DAY
        assert stock[other].spools == 0
        assert sorted(e.stock_g for e in stock.values()) == [0.0, 1100.0]


def test_snapshot_drops_rows_of_spools_no_longer_live(db):
    fake, spools = _two_spools()
    with db.begin() as s:
        filament.record_snapshot(s, spools, DAY)
    with db.begin() as s:
        assert filament.record_snapshot(s, spools[:1], DAY) == 1
    with db.begin() as s:
        stock, day = filament.stock_from_snapshots(s)
        (entry,) = stock.values()
        assert (day, entry.stock_g, entry.spools) == (DAY, 800.0, 1)


def test_blank_color_name_keeps_known_label(db):
    fake = FakeBambuddy()
    named = fake.add_spool("PLA", color_name="Black")
    unnamed = fake.add_spool("PLA")
    for order in ([named, unnamed], [unnamed, named]):
        with db.begin() as s:
            ids = filament.sync_sorts(s, order)
            sort = s.get(FilamentSort, ids[named.id])
            assert sort.label == "PLA Black"
            assert sort.color_name == "Black"


def test_unnamed_spools_split_by_color_hex(db):
    fake = FakeBambuddy()
    black = fake.add_spool("PLA", subtype="Matte", color_hex="#000000", color_name=None)
    white = fake.add_spool("PLA", subtype="Matte", color_hex="#FFFFFF", color_name=None)
    black_again = fake.add_spool("PLA", subtype="Matte", color_hex="#000000", color_name=None)
    with db.begin() as s:
        ids = filament.sync_sorts(s, [black, white])
        assert len(set(ids.values())) == 2
        assert s.scalar(select(func.count()).select_from(FilamentSort)) == 2
    with db.begin() as s:
        ids = filament.sync_sorts(s, [black, black_again])
        assert len(set(ids.values())) == 1
        assert s.scalar(select(func.count()).select_from(FilamentSort)) == 2  # the two from above


def test_unnamed_spool_label_shows_hex_once_subtype(db):
    fake = FakeBambuddy()
    spool = fake.add_spool("PLA", subtype="Matte", color_hex="#000000", color_name=None)
    with db.begin() as s:
        ids = filament.sync_sorts(s, [spool])
        label = s.get(FilamentSort, ids[spool.id]).label
    assert label == "PLA Matte #000000"
    assert "#000000" in label and "None" not in label and label.count("Matte") == 1
