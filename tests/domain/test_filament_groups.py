"""The filament group of a slot: the spool sorts ticked for it, one of them reordered (Spec 5.2, 9.1)."""

from datetime import datetime

import pytest
from sqlalchemy import select

from factories import assign_sort, make_article, make_legacy_sort, make_sort, tick_sorts
from reprintbuddy.domain import filament
from reprintbuddy.domain.errors import ConflictError, NotFoundError, ValidationError
from reprintbuddy.models import ArticleFilament, ArticleFilamentSort

BLACK, NEAR_BLACK, ANTHRACITE = "#000000", "#1A1A1A", "#333333"  # 0 / 5.5 / 13.4 apart from black


def _article(s, name="Template A", slots=((1, "PLA", BLACK),)):
    article = make_article(s, name=name)
    for slot, material, color in slots:
        s.add(ArticleFilament(article_id=article.id, slot=slot, grams=10.0, material=material, color_hex=color))
    s.flush()
    return article


def _rows(s, article_id):
    return sorted(
        (r.slot, r.sort_id, r.reordered)
        for r in s.scalars(select(ArticleFilamentSort).where(ArticleFilamentSort.article_id == article_id))
    )


# ---- candidates ----------------------------------------------------------------------------------


def test_candidate_needs_the_material(db):
    with db.begin() as s:
        pla = make_sort(s)
        petg = make_sort(s, material="PETG")
        assert filament.is_candidate(pla, "PLA", BLACK)
        assert filament.is_candidate(pla, " pla ", BLACK)
        assert not filament.is_candidate(petg, "PLA", BLACK)


def test_candidate_colour_up_to_delta_e_7(db):
    with db.begin() as s:
        near = make_sort(s, color_hex=NEAR_BLACK, color_name=None)
        far = make_sort(s, color_hex=ANTHRACITE, color_name=None)
        assert filament.is_candidate(near, "PLA", BLACK)  # 5.5
        assert not filament.is_candidate(far, "PLA", BLACK)  # 13.4


def test_candidate_brand_and_subtype_do_not_count(db):
    with db.begin() as s:
        a = make_sort(s, brand="Brand A")
        b = make_sort(s, brand="Brand B", subtype="Matte")
        none = make_sort(s, brand=None, subtype="Silk")
        assert all(filament.is_candidate(x, "PLA", BLACK) for x in (a, b, none))


def test_transparent_only_to_transparent(db):
    with db.begin() as s:
        clear = make_sort(s, color_hex="#00000000", color_name=None)
        solid = make_sort(s)
        assert not filament.is_candidate(clear, "PLA", BLACK)
        assert not filament.is_candidate(solid, "PLA", "#00000000")
        assert filament.is_candidate(clear, "PLA", "#FFFFFF00")  # the RGB of transparent carries no meaning


def test_slot_without_colour_or_material_has_no_candidates(db):
    with db.begin() as s:
        sort = make_sort(s)
        assert not filament.is_candidate(sort, "PLA", None)
        assert not filament.is_candidate(sort, "PLA", "")
        assert not filament.is_candidate(sort, None, BLACK)
        assert not filament.is_candidate(sort, " ", BLACK)


def test_a_legacy_sort_is_never_a_candidate(db):
    with db.begin() as s:
        old = make_legacy_sort(s)
        assert not filament.is_candidate(old, "PLA", BLACK)


# ---- slot_groups -------------------------------------------------------------------------------


def test_slot_groups_lists_candidates_by_label_nothing_ticked(db):
    with db.begin() as s:
        b = make_sort(s, brand="Brand B")
        a = make_sort(s, brand="Brand A", color_hex=NEAR_BLACK, color_name="Jet")
        make_sort(s, color_hex=ANTHRACITE, color_name="Grey")
        make_sort(s, material="PETG")
        make_legacy_sort(s)
        article = _article(s, slots=((2, "PETG", None), (1, "PLA", BLACK)))
        first, second = filament.slot_groups(s, article.id)
        assert (first.slot, first.material, first.color_hex, first.grams) == (1, "PLA", BLACK, 10.0)
        assert first.choices == (a.id, b.id)  # "Brand A PLA Jet" before "Brand B PLA Black"
        assert first.ticked == frozenset() and first.reordered is None
        assert second.slot == 2 and second.choices == ()  # no colour: no candidates


def test_slot_groups_keep_a_ticked_sort_that_no_longer_fits(db):
    with db.begin() as s:
        fits = make_sort(s)
        grey = make_sort(s, color_hex=ANTHRACITE, color_name="Grey")
        article = _article(s)
        tick_sorts(s, article.id, 1, grey, fits, reordered=fits)
        (group,) = filament.slot_groups(s, article.id)
        assert set(group.choices) == {fits.id, grey.id}
        assert group.ticked == {fits.id, grey.id} and group.reordered == fits.id


# ---- set_groups --------------------------------------------------------------------------------


def test_one_ticked_sort_is_reordered_by_itself(db):
    with db.begin() as s:
        sort = make_sort(s)
        article = _article(s)
        filament.set_groups(s, article.id, {1: ([sort.id], None)})
        assert _rows(s, article.id) == [(1, sort.id, True)]


def test_several_ticked_need_the_reordered_one(db):
    with db.begin() as s:
        a = make_sort(s)
        b = make_sort(s, brand="Brand B")
        article = _article(s)
        with pytest.raises(ValidationError) as e:
            filament.set_groups(s, article.id, {1: ([a.id, b.id], None)})
        assert e.value.key == "filament.choose_reordered"
        with pytest.raises(ValidationError) as e:
            filament.set_groups(s, article.id, {1: ([a.id], b.id), 2: ([], None)})
        assert e.value.key == "filament.unknown_slot"
        c = make_sort(s, brand="Brand C")
        with pytest.raises(ValidationError) as e:
            filament.set_groups(s, article.id, {1: ([a.id, b.id], c.id)})
        assert e.value.key == "filament.reordered_not_ticked"
        assert _rows(s, article.id) == []
        filament.set_groups(s, article.id, {1: ([a.id, b.id], b.id)})
        assert _rows(s, article.id) == [(1, a.id, False), (1, b.id, True)]


def test_only_candidates_can_be_ticked(db):
    with db.begin() as s:
        fits = make_sort(s)
        grey = make_sort(s, color_hex=ANTHRACITE, color_name="Grey")
        petg = make_sort(s, material="PETG")
        article = _article(s)
        for wrong in (grey, petg):
            with pytest.raises(ValidationError) as e:
                filament.set_groups(s, article.id, {1: ([fits.id, wrong.id], fits.id)})
            assert e.value.key == "filament.not_a_candidate"
        assert _rows(s, article.id) == []


def test_legacy_or_unknown_sort_not_found(db):
    with db.begin() as s:
        fits = make_sort(s)
        old = make_legacy_sort(s)
        article = _article(s)
        for missing in (old.id, 999):
            with pytest.raises(NotFoundError) as e:
                filament.set_groups(s, article.id, {1: ([fits.id, missing], fits.id)})
            assert e.value.key == "filament.sort_not_found"
        assert _rows(s, article.id) == []


def test_archived_or_unknown_article(db):
    with db.begin() as s:
        sort = make_sort(s)
        article = _article(s)
        article.archived_at = datetime(2026, 3, 1)
        with pytest.raises(ConflictError) as e:
            filament.set_groups(s, article.id, {1: ([sort.id], None)})
        assert e.value.key == "articles.edit_archived"
        with pytest.raises(NotFoundError) as e:
            filament.set_groups(s, 999, {1: ([sort.id], None)})
        assert e.value.key == "articles.not_found"
        assert _rows(s, article.id) == []


def test_nothing_is_written_when_one_slot_is_wrong(db):
    with db.begin() as s:
        a = make_sort(s)
        b = make_sort(s, brand="Brand B")
        article = _article(s, slots=((1, "PLA", BLACK), (2, "PLA", BLACK)))
        filament.set_groups(s, article.id, {2: ([b.id], None)})
        with pytest.raises(ValidationError):
            filament.set_groups(s, article.id, {1: ([a.id], None), 2: ([a.id, b.id], None)})
        assert _rows(s, article.id) == [(2, b.id, True)]


def test_other_slots_stay_and_an_empty_group_clears_the_slot(db):
    with db.begin() as s:
        a = make_sort(s)
        b = make_sort(s, brand="Brand B")
        article = _article(s, slots=((1, "PLA", BLACK), (2, "PLA", BLACK)))
        filament.set_groups(s, article.id, {1: ([a.id], None), 2: ([b.id], None)})
        filament.set_groups(s, article.id, {1: ([a.id, b.id], b.id)})
        assert _rows(s, article.id) == [(1, a.id, False), (1, b.id, True), (2, b.id, True)]
        filament.set_groups(s, article.id, {1: ([], a.id)})
        assert _rows(s, article.id) == [(2, b.id, True)]
        assert filament.unmapped_slots(s, article.id) == [(article.id, "Template A", 1)]


def test_a_ticked_sort_that_no_longer_fits_may_stay_ticked(db):
    with db.begin() as s:
        fits = make_sort(s)
        grey = make_sort(s, color_hex=ANTHRACITE, color_name="Grey")
        article = _article(s)
        tick_sorts(s, article.id, 1, grey)  # ticked when it still fitted
        filament.set_groups(s, article.id, {1: ([grey.id, fits.id], fits.id)})
        assert _rows(s, article.id) == [(1, fits.id, True), (1, grey.id, False)]
        filament.set_groups(s, article.id, {1: ([fits.id], None)})  # once unticked it cannot come back
        with pytest.raises(ValidationError) as e:
            filament.set_groups(s, article.id, {1: ([grey.id, fits.id], fits.id)})
        assert e.value.key == "filament.not_a_candidate"


# ---- what the other pages read -----------------------------------------------------------------


def test_unmapped_slots_lists_active_only(db):
    with db.begin() as s:
        sort = make_sort(s)
        b = _article(s, name="Bravo", slots=((1, "PLA", BLACK), (2, "PLA", BLACK)))
        a = _article(s, name="Alpha")
        old = _article(s, name="Old")
        old.archived_at = datetime(2026, 3, 1)
        filament.set_groups(s, b.id, {1: ([sort.id], None)})
        assert filament.unmapped_slots(s) == [(a.id, "Alpha", 1), (b.id, "Bravo", 2)]
        assert filament.unmapped_slots(s, b.id) == [(b.id, "Bravo", 2)]
        assert filament.unmapped_slots(s, old.id) == []


def test_ticked_sort_ids_only_of_active_articles(db):  # p46, Spec 9.3
    with db.begin() as s:
        used = make_sort(s, material="PLA")
        second = make_sort(s, material="PLA", brand="Brand B")
        both = make_sort(s, material="PETG")
        archived_only = make_sort(s, material="ABS")
        make_sort(s, material="TPU")  # ticked nowhere
        one = assign_sort(s, used, name="One")
        tick_sorts(s, one.id, 1, second, reordered=used)  # ticked, not reordered: listed as well
        assign_sort(s, both, name="Two")
        assign_sort(s, both, name="Three")
        assign_sort(s, archived_only, name="Four", archived=True)
        _article(s, name="Five")  # a slot without a group counts for nothing
        assert filament.ticked_sort_ids(s) == {used.id, second.id, both.id}


def test_reorder_groups(db):
    with db.begin() as s:
        a = make_sort(s)
        b = make_sort(s, brand="Brand B")
        c = make_sort(s, brand="Brand C")
        one = _article(s, name="One", slots=((1, "PLA", BLACK), (2, "PLA", BLACK)))
        two = _article(s, name="Two")
        gone = _article(s, name="Gone")
        tick_sorts(s, one.id, 1, a, b, reordered=a)
        tick_sorts(s, one.id, 2, b, reordered=b)
        tick_sorts(s, two.id, 1, a, c, reordered=a)
        tick_sorts(s, gone.id, 1, c, reordered=c)
        gone.archived_at = datetime(2026, 3, 1)
        s.flush()
        groups = filament.reorder_groups(s)
        assert set(groups) == {a.id, b.id}  # c is reordered only in an archived article
        assert groups[a.id].slots == ((one.id, 1), (two.id, 1))
        assert groups[a.id].members == {a.id, b.id, c.id}
        assert groups[b.id].slots == ((one.id, 2),) and groups[b.id].members == {b.id}
