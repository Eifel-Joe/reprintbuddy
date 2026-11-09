"""A new article's filament page starts from a similar article (Spec 5.2, 9.1, p156).

Similar: any slot of an existing, not archived article with the same material and the same colour;
the slot number does not matter. Several: the article created last (highest id) that has a group in
such a slot. Nothing is stored: the function only says what the page may tick."""

import pytest
from sqlalchemy import select

from factories import make_article, make_legacy_sort, make_sort, tick_sorts
from reprintbuddy.domain import catalog, filament
from reprintbuddy.models import ArticleFilament, ArticleFilamentSort, utcnow

BLACK, WHITE, ANTHRACITE = "#000000", "#FFFFFF", "#333333"  # anthracite is 13.4 apart from black


def article(s, name, slots=((1, "PLA", BLACK),), **fields):
    art = make_article(s, name=name, **fields)
    for slot, material, color in slots:
        s.add(ArticleFilament(article_id=art.id, slot=slot, grams=10.0, material=material, color_hex=color))
    s.flush()
    return art


def sorts(s):
    return (
        make_sort(s, brand="Brand A", color_hex=BLACK),
        make_sort(s, brand="Brand B", color_hex=BLACK, color_name="Schwarz"),
    )


def test_one_similar_article_gives_its_sorts_and_what_is_reordered(db):
    with db.begin() as s:
        a, b = sorts(s)
        source = article(s, "Source")
        tick_sorts(s, source.id, 1, a, b, reordered=b)
        new = article(s, "New")
        found = filament.suggest_from_similar(s, new.id)
        assert found == {1: filament.Suggestion(source.id, "Source", frozenset({a.id, b.id}), b.id)}


def test_several_similar_articles_the_last_created_wins(db):
    with db.begin() as s:
        a, b = sorts(s)
        old = article(s, "Old")
        tick_sorts(s, old.id, 1, a)
        young = article(s, "Young")
        tick_sorts(s, young.id, 1, b)
        new = article(s, "New")
        found = filament.suggest_from_similar(s, new.id)[1]
        assert (found.source_id, found.source_name, found.sort_ids) == (young.id, "Young", frozenset({b.id}))


def test_no_similar_article_no_suggestion(db):
    with db.begin() as s:
        a, _ = sorts(s)
        other = article(s, "Other", slots=((1, "PLA", WHITE),))
        tick_sorts(s, other.id, 1, make_sort(s, color_hex=WHITE))
        new = article(s, "New")
        assert filament.suggest_from_similar(s, new.id) == {}
        assert filament.suggest_from_similar(s, other.id) == {}  # black is nobody's colour here


def test_same_material_other_colour_and_same_colour_other_material_are_no_match(db):
    with db.begin() as s:
        a, _ = sorts(s)
        near = article(s, "Near colour", slots=((1, "PLA", ANTHRACITE),))  # similar enough to be offered, not the same
        tick_sorts(s, near.id, 1, make_sort(s, color_hex=ANTHRACITE, color_name="Anthracite"))
        petg = article(s, "Other material", slots=((1, "PETG", BLACK),))
        tick_sorts(s, petg.id, 1, make_sort(s, material="PETG", color_hex=BLACK))
        new = article(s, "New")
        assert filament.suggest_from_similar(s, new.id) == {}


def test_an_archived_article_is_no_source(db):
    with db.begin() as s:
        a, b = sorts(s)
        old = article(s, "Old")
        tick_sorts(s, old.id, 1, a)
        young = article(s, "Young archived", archived_at=utcnow())
        tick_sorts(s, young.id, 1, b)
        new = article(s, "New")
        assert filament.suggest_from_similar(s, new.id)[1].source_id == old.id


def test_an_article_without_a_group_in_the_slot_is_skipped(db):
    with db.begin() as s:
        a, _ = sorts(s)
        old = article(s, "Old")
        tick_sorts(s, old.id, 1, a)
        article(s, "Younger without any group")
        new = article(s, "New")
        assert filament.suggest_from_similar(s, new.id)[1].source_id == old.id


def test_the_article_itself_is_no_source(db):
    with db.begin() as s:
        a, _ = sorts(s)
        new = article(s, "New")
        tick_sorts(s, new.id, 1, a)  # its own group does not count: it is no "other" article
        assert filament.suggest_from_similar(s, new.id) == {}


def test_the_slot_number_does_not_matter(db):
    with db.begin() as s:
        a, _ = sorts(s)
        source = article(s, "Source", slots=((1, "PLA", BLACK),))
        tick_sorts(s, source.id, 1, a)
        new = article(s, "New", slots=((1, "PLA", WHITE), (2, "PLA", BLACK)))  # black sits in slot 2 of the new file
        found = filament.suggest_from_similar(s, new.id)
        assert set(found) == {2} and found[2].sort_ids == frozenset({a.id})


def test_several_equal_slots_in_the_source_the_smallest_number_with_a_group_wins(db):
    with db.begin() as s:
        a, b = sorts(s)
        source = article(s, "Source", slots=((1, "PLA", WHITE), (2, "PLA", BLACK), (3, "PLA", BLACK)))
        tick_sorts(s, source.id, 3, b)
        tick_sorts(s, source.id, 2, a)
        new = article(s, "New")
        assert filament.suggest_from_similar(s, new.id)[1].sort_ids == frozenset({a.id})  # slot 2, not slot 3
        # the smaller of the two equal slots has no group: the next one with a group counts
        for row in s.scalars(select(ArticleFilamentSort).where(
            ArticleFilamentSort.article_id == source.id, ArticleFilamentSort.slot == 2
        )).all():
            s.delete(row)
        s.flush()
        assert filament.suggest_from_similar(s, new.id)[1].sort_ids == frozenset({b.id})


def test_each_slot_of_the_new_article_looks_for_its_own_source(db):
    with db.begin() as s:
        a, b = sorts(s)
        white = make_sort(s, brand="Brand W", color_hex=WHITE, color_name="White")
        black_source = article(s, "Black source")
        tick_sorts(s, black_source.id, 1, a)
        white_source = article(s, "White source", slots=((1, "PLA", WHITE),))
        tick_sorts(s, white_source.id, 1, white)
        new = article(s, "New", slots=((1, "PLA", BLACK), (2, "PLA", WHITE), (3, "PLA", ANTHRACITE)))
        found = filament.suggest_from_similar(s, new.id)
        assert {slot: f.source_name for slot, f in found.items()} == {1: "Black source", 2: "White source"}


def test_only_sorts_that_the_new_slot_offers_are_suggested(db):
    with db.begin() as s:
        a, _ = sorts(s)
        legacy = make_legacy_sort(s)
        far = make_sort(s, brand="Far", color_hex=ANTHRACITE, color_name="Anthracite")  # ticked once, no candidate of black
        source = article(s, "Source")
        tick_sorts(s, source.id, 1, a, far, legacy, reordered=far)
        new = article(s, "New")
        found = filament.suggest_from_similar(s, new.id)[1]
        assert found.sort_ids == frozenset({a.id})
        assert found.reordered == a.id  # the reordered sort fell away: the only one left is reordered


def test_a_group_of_legacy_sorts_only_is_no_group(db):
    with db.begin() as s:
        a, _ = sorts(s)
        old = article(s, "Old")
        tick_sorts(s, old.id, 1, a)
        young = article(s, "Young")
        tick_sorts(s, young.id, 1, make_legacy_sort(s))
        new = article(s, "New")
        assert filament.suggest_from_similar(s, new.id)[1].source_id == old.id


def test_when_the_reordered_sort_falls_away_among_several_none_is_reordered(db):
    with db.begin() as s:
        a, b = sorts(s)
        far = make_sort(s, brand="Far", color_hex=ANTHRACITE, color_name="Anthracite")
        source = article(s, "Source")
        tick_sorts(s, source.id, 1, a, b, far, reordered=far)
        new = article(s, "New")
        found = filament.suggest_from_similar(s, new.id)[1]
        assert found.sort_ids == frozenset({a.id, b.id}) and found.reordered is None  # the person has to choose


def test_a_slot_without_material_or_colour_gets_no_suggestion(db):
    with db.begin() as s:
        a, _ = sorts(s)
        source = article(s, "Source", slots=((1, None, None),))
        tick_sorts(s, source.id, 1, a)
        new = article(s, "New", slots=((1, None, None),))
        assert filament.suggest_from_similar(s, new.id) == {}


def test_the_suggestion_writes_nothing(db):
    with db.begin() as s:
        a, _ = sorts(s)
        source = article(s, "Source")
        tick_sorts(s, source.id, 1, a)
        new = article(s, "New")
        new_id = new.id
        filament.suggest_from_similar(s, new_id)
        assert filament.slot_groups(s, new_id)[0].ticked == frozenset()


@pytest.mark.parametrize(
    "old,new,same",
    [
        (("PLA", BLACK), ("PLA", BLACK), True),
        (("PLA", BLACK), ("PETG", BLACK), False),
        (("PLA", BLACK), ("PLA", "#000001"), False),
        (("PLA", BLACK), None, False),
        (None, None, True),  # a slot that is not there in either: the rule of "keep the group" as it was
        ((None, None), (None, None), True),
    ],
)
def test_one_comparison_serves_keeping_a_group_and_the_suggestion(old, new, same):
    assert catalog.same_filament_kind(old, new) is same
