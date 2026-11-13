"""The questions about filament sorts and an article's slots, asked in one place (refactoring before 1.0.0, R5)."""

from __future__ import annotations

from factories import make_article, make_legacy_sort, make_sort
from reprintbuddy.domain import filament
from reprintbuddy.models import ArticleFilament, ArticleFilamentSort


def test_current_sorts_leave_the_sorts_of_0006_out(db):
    with db.begin() as s:
        current = make_sort(s).id
        make_legacy_sort(s, color_hex="#ffffff", color_name="White")
    with db() as s:
        assert [sort.id for sort in s.scalars(filament.current_sorts())] == [current]


def test_article_slots_in_slot_order(db):
    with db.begin() as s:
        article_id = make_article(s).id
        for slot in (3, 1, 2):
            s.add(ArticleFilament(article_id=article_id, slot=slot, grams=1.0, material="PLA", color_hex="#000000"))
    with db() as s:
        assert [row.slot for row in filament.article_slots(s, article_id)] == [1, 2, 3]


def test_ticked_rows_by_slot(db):
    with db.begin() as s:
        article_id = make_article(s).id
        black, white = make_sort(s).id, make_sort(s, color_hex="#ffffff", color_name="White").id
        s.add(ArticleFilament(article_id=article_id, slot=1, grams=1.0, material="PLA", color_hex="#000000"))
        s.add(ArticleFilamentSort(article_id=article_id, slot=1, sort_id=black, reordered=True))
        s.add(ArticleFilamentSort(article_id=article_id, slot=1, sort_id=white))
    with db() as s:
        rows = filament.ticked_rows(s, article_id)
        assert sorted((r.sort_id, r.reordered) for r in rows[1]) == sorted([(black, True), (white, False)])
        assert rows.get(2, []) == []

