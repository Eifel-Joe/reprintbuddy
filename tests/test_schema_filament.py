from datetime import date

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from reprintbuddy.models import ArticleFilamentSort, ReorderMark

from factories import make_article, make_filament_settings, make_legacy_sort, make_snapshot, make_sort, tick_sorts

TABLES = {"filament_sorts", "filament_settings", "filament_snapshots", "reorder_marks", "article_filament_sorts"}


def test_tables_exist(db):
    assert TABLES <= set(inspect(db.kw["bind"]).get_table_names())


def test_the_database_is_at_the_newest_revision(db):
    from reprintbuddy.db import known_revisions

    with db() as s:
        assert s.execute(text("select version_num from alembic_version")).scalar() == known_revisions()[-1]


def test_make_sort_key_and_label(db):
    with db.begin() as s:
        plain = make_sort(s, brand="Brand A")
        matte = make_sort(s, brand="Brand A", subtype="Matte", color_name="matte", color_hex="#FFFFFF")
        unnamed = make_sort(s, brand="Brand B", material="PETG", color_name=None)
        assert plain.sort_key == "brand a|pla||#000000"
        assert plain.label == "Brand A PLA Black"
        assert matte.sort_key == "brand a|pla|matte|#ffffff"
        assert matte.label == "Brand A PLA Matte"
        assert unnamed.label == "Brand B PETG #000000"
        assert plain.first_seen_at is not None
        assert plain.legacy is False
        old = make_legacy_sort(s)
        assert old.legacy is True and old.sort_key == f"legacy:{old.id}" and old.brand is None


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
        make_sort(s, brand="Brand A")
        make_sort(s, brand="Brand B")  # another brand: another sort
    with pytest.raises(IntegrityError):
        with db.begin() as s:
            make_sort(s, brand="Brand A", color_name="Other name")


def _slot_with(s, *sorts, reordered=None):
    article = make_article(s)
    tick_sorts(s, article.id, 1, *sorts, reordered=reordered)
    return article


def test_one_reordered_sort_per_slot(db):
    with db.begin() as s:
        a = make_sort(s, brand="Brand A")
        b = make_sort(s, brand="Brand B")
        article = _slot_with(s, a, b, reordered=a)  # two ticked, one reordered: fine
        tick_sorts(s, article.id, 2, b)  # another slot has its own reordered sort
        article_id, b_id = article.id, b.id
    with pytest.raises(IntegrityError):
        with db.begin() as s:
            row = s.get(ArticleFilamentSort, (article_id, 1, b_id))
            row.reordered = True


def test_a_sort_is_ticked_once_per_slot(db):
    with db.begin() as s:
        a = make_sort(s).id
        article = _slot_with(s).id
        s.add(ArticleFilamentSort(article_id=article, slot=1, sort_id=a, reordered=True))
    with pytest.raises(IntegrityError):
        with db.begin() as s:
            s.add(ArticleFilamentSort(article_id=article, slot=1, sort_id=a, reordered=False))


def test_ticked_sort_must_exist(db):
    with db.begin() as s:
        article = _slot_with(s).id
    with pytest.raises(IntegrityError):
        with db.begin() as s:
            s.add(ArticleFilamentSort(article_id=article, slot=1, sort_id=999, reordered=True))


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
