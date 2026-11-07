from datetime import datetime

import pytest

from factories import assign_sort, make_article, make_sort
from reprintbuddy.domain import filament
from reprintbuddy.domain.errors import ConflictError, NotFoundError, ValidationError
from reprintbuddy.models import ArticleFilament, FilamentSort


def _sorts(s) -> dict[int, filament.SortStock]:
    from sqlalchemy import select

    return {
        x.id: filament.SortStock(x.id, x.label, x.material, x.color_hex, 0.0, 0)
        for x in s.scalars(select(FilamentSort))
    }


def _article(s, name="Template A", slots=((1, "PLA", "#000000"),)):
    article = make_article(s, name=name)
    for slot, material, color in slots:
        s.add(ArticleFilament(article_id=article.id, slot=slot, grams=10.0, material=material, color_hex=color))
    s.flush()
    return article


def test_suggest_unique_match(db):  # Spec 5.2
    with db.begin() as s:
        black = make_sort(s)
        make_sort(s, color_hex="#FFFFFF", color_name="White")
        make_sort(s, material="PETG")
        assert filament.suggest_sort("PLA", "#000000", _sorts(s)) == black.id


def test_suggest_case_insensitive(db):
    with db.begin() as s:
        black = make_sort(s)
        assert filament.suggest_sort("pla", "#000000", _sorts(s)) == black.id
        blue = make_sort(s, color_hex="#AABBCC", color_name="Blue")
        assert filament.suggest_sort(" PLA ", "#aabbcc", _sorts(s)) == blue.id


def test_suggest_none_when_ambiguous_or_missing(db):
    with db.begin() as s:
        make_sort(s)
        make_sort(s, subtype="Matte")
        sorts = _sorts(s)
        assert filament.suggest_sort("PLA", "#000000", sorts) is None  # two subtypes, same colour
        assert filament.suggest_sort("PLA", None, sorts) is None
        assert filament.suggest_sort(None, "#000000", sorts) is None
        assert filament.suggest_sort("PLA", "#123456", sorts) is None


def test_suggest_takes_a_similar_colour(db):  # Spec 9.1: at most delta E 7
    with db.begin() as s:
        black = make_sort(s)
        sorts = _sorts(s)
        assert filament.suggest_sort("PLA", "#1A1A1A", sorts) == black.id  # 5.5 away
        assert filament.suggest_sort("PLA", "#333333", sorts) is None  # 13.4 away


def test_set_and_read_mapping(db):
    with db.begin() as s:
        sort = make_sort(s)
        article = _article(s, slots=((2, "PLA", "#000000"), (1, "PLA", "#000000")))
        filament.set_mapping(s, article.id, {1: sort.id})
        rows = filament.slot_rows(s, article.id, _sorts(s))
        assert [r.slot for r in rows] == [1, 2]
        assert rows[0].mapped == sort.id and rows[1].mapped is None
        assert rows[0].suggested == sort.id  # also when a mapping exists
        filament.set_mapping(s, article.id, {1: None})
        assert filament.slot_rows(s, article.id, _sorts(s))[0].mapped is None


def test_set_mapping_errors(db):
    with db.begin() as s:
        sort = make_sort(s)
        article = _article(s)
        with pytest.raises(ValidationError) as e:
            filament.set_mapping(s, article.id, {9: sort.id})
        assert e.value.key == "filament.unknown_slot"
        with pytest.raises(NotFoundError) as e:
            filament.set_mapping(s, article.id, {1: 999})
        assert e.value.key == "filament.sort_not_found"
        with pytest.raises(ValidationError):  # no partial update
            filament.set_mapping(s, article.id, {1: sort.id, 9: sort.id})
        assert filament.slot_rows(s, article.id, _sorts(s))[0].mapped is None
        article.archived_at = datetime(2026, 3, 1)
        with pytest.raises(ConflictError) as e:
            filament.set_mapping(s, article.id, {1: sort.id})
        assert e.value.key == "articles.edit_archived"


def test_unmapped_slots_lists_active_only(db):
    with db.begin() as s:
        sort = make_sort(s)
        b = _article(s, name="Bravo", slots=((1, "PLA", "#000000"), (2, "PLA", "#000000")))
        a = _article(s, name="Alpha")
        old = _article(s, name="Old")
        old.archived_at = datetime(2026, 3, 1)
        filament.set_mapping(s, b.id, {1: sort.id})
        assert filament.unmapped_slots(s) == [(a.id, "Alpha", 1), (b.id, "Bravo", 2)]


def test_assigned_sort_ids_only_of_active_articles(db):  # p46
    with db.begin() as s:
        used = make_sort(s, material="PLA")
        both = make_sort(s, material="PETG")
        archived_only = make_sort(s, material="ABS")
        unused = make_sort(s, material="TPU")
        assign_sort(s, used, name="One")
        assign_sort(s, both, name="Two")
        assign_sort(s, both, name="Three")
        assign_sort(s, archived_only, name="Four", archived=True)
        _article(s, name="Five")  # a slot without a sort counts for nothing
        assert filament.assigned_sort_ids(s) == {used.id, both.id}
        assert unused.id not in filament.assigned_sort_ids(s)
