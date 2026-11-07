from datetime import date

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from reprintbuddy.models import ReorderMark

from factories import make_filament_settings, make_snapshot, make_sort

TABLES = {"filament_sorts", "filament_settings", "filament_snapshots", "reorder_marks"}


def test_tables_exist(db):
    assert TABLES <= set(inspect(db.kw["bind"]).get_table_names())


def test_head_is_0006(db):
    with db() as s:
        assert s.execute(text("select version_num from alembic_version")).scalar() == "0006"


def test_make_sort_key_and_label(db):
    with db.begin() as s:
        plain = make_sort(s)
        matte = make_sort(s, subtype="Matte", color_name="matte", color_hex="#FFFFFF")
        unnamed = make_sort(s, material="PETG", color_name=None)
        assert plain.sort_key == "pla||#000000"
        assert plain.label == "PLA Black"
        assert matte.sort_key == "pla|matte|#ffffff"
        assert matte.label == "PLA Matte"
        assert unnamed.label == "PETG #000000"
        assert plain.first_seen_at is not None


def test_settings_defaults(db):
    with db.begin() as s:
        sort = make_sort(s)
        settings = make_filament_settings(s, sort.id)
        s.refresh(settings)
        assert settings.lead_time_days == 0
        assert settings.safety_value == 0
        assert settings.safety_unit == "days"
        assert settings.pack_grams == 1000
        assert settings.min_order_packs == 1
        assert settings.supplier is None
        assert settings.order_url is None


@pytest.mark.parametrize(
    "build",
    [
        lambda s, sort: make_filament_settings(s, sort, safety_unit="weeks"),
        lambda s, sort: make_filament_settings(s, sort, pack_grams=0),
        lambda s, sort: make_filament_settings(s, sort, min_order_packs=0),
        lambda s, sort: _mark(s, sort, item_type="other"),
        lambda s, sort: _mark(s, sort, qty=0),
    ],
    ids=["safety_unit", "pack_grams=0", "min_order_packs=0", "item_type", "qty=0"],
)
def test_checks(db, build):
    with db.begin() as s:
        sort = make_sort(s).id
    with pytest.raises(IntegrityError):
        with db.begin() as s:
            build(s, sort)


def _mark(s, item_id, *, item_type="filament", qty=1):
    mark = ReorderMark(item_type=item_type, item_id=item_id, qty=qty)
    s.add(mark)
    s.flush()
    return mark


def test_mark_defaults(db):
    with db.begin() as s:
        mark = _mark(s, make_sort(s).id)
        s.refresh(mark)
        assert mark.marked_at is not None
        assert mark.received_at is None


def test_sort_key_unique(db):
    with db.begin() as s:
        make_sort(s)
    with pytest.raises(IntegrityError):
        with db.begin() as s:
            make_sort(s, color_name="Other name")


def test_snapshot_unique_per_day_and_spool(db):
    with db.begin() as s:
        sort = make_sort(s).id
        make_snapshot(s, date(2026, 1, 1), 1, sort, 900.0)
        make_snapshot(s, date(2026, 1, 2), 1, sort, 850.0)
        make_snapshot(s, date(2026, 1, 1), 2, sort, 500.0)
    with pytest.raises(IntegrityError):
        with db.begin() as s:
            make_snapshot(s, date(2026, 1, 1), 1, sort, 800.0)


def test_snapshot_needs_known_sort(db):
    with pytest.raises(IntegrityError):
        with db.begin() as s:
            make_snapshot(s, date(2026, 1, 1), 1, 999, 800.0)
