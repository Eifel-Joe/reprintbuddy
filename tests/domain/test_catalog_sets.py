import pytest
from sqlalchemy import select

from factories import make_article
from reprintbuddy.domain import catalog
from reprintbuddy.domain.catalog import ArticleInput, PositionInput, SetInput
from reprintbuddy.domain.errors import ConflictError, NotFoundError, ValidationError
from reprintbuddy.models import SetPosition, SetPositionOption


def test_set_available_minimum(db):  # Spec 5.4
    with db.begin() as s:
        a = make_article(s, name="Template A", stock=3).id
        b = make_article(s, name="Template B", stock=5).id
        catalog.create_set(
            s, SetInput("Starter Set", None, [PositionInput(1, [a]), PositionInput(2, [b])])
        )
    with db.begin() as s:
        assert catalog.list_sets(s)[0].available == 2


def test_set_with_choice(db):  # Spec 5.4 choice
    with db.begin() as s:
        a = make_article(s, name="Template A", stock=10).id
        b = make_article(s, name="Template B", stock=3).id
        c = make_article(s, name="Template C", stock=5).id
        catalog.create_set(
            s, SetInput("Starter Set", None, [PositionInput(1, [a]), PositionInput(1, [b, c])])
        )
    with db.begin() as s:
        row = catalog.list_sets(s)[0]
        assert row.available == 8
        assert [p.is_choice for p in row.positions] == [False, True]
        assert row.positions[1].article_names == ["Template B", "Template C"]


def test_set_negative_shown(db):
    with db.begin() as s:
        a = make_article(s, name="Template A", stock=0, reserved=1).id
        catalog.create_set(s, SetInput("Starter Set", None, [PositionInput(1, [a])]))
    with db.begin() as s:
        assert catalog.list_sets(s)[0].available == -1


def test_set_validation(db):
    with db.begin() as s:
        a = make_article(s).id
    cases = [
        (SetInput("S", None, []), "sets.no_positions"),
        (SetInput("S", None, [PositionInput(0, [a])]), "form.invalid_number"),
        (SetInput("S", None, [PositionInput(1001, [a])]), "form.invalid_number"),
        (SetInput("S", None, [PositionInput(1, [])]), "sets.position_empty"),
        (SetInput("  ", None, [PositionInput(1, [a])]), "form.required"),
        (SetInput("S", None, [PositionInput(1, [999])]), "articles.not_found"),
    ]
    for data, key in cases:
        with pytest.raises((ValidationError, NotFoundError)) as info:
            with db.begin() as s:
                catalog.create_set(s, data)
        assert info.value.key == key, data
    with db.begin() as s:
        assert catalog.list_sets(s) == []


def test_duplicate_articles_in_position_merged(db):
    with db.begin() as s:
        a = make_article(s).id
        catalog.create_set(s, SetInput("S", None, [PositionInput(1, [a, a])]))
    with db.begin() as s:
        row = catalog.list_sets(s)[0]
        assert row.positions[0].article_ids == [a] and not row.positions[0].is_choice


def test_update_replaces_positions(db):
    with db.begin() as s:
        a = make_article(s, name="Template A", stock=2).id
        b = make_article(s, name="Template B", stock=9).id
        set_id = catalog.create_set(s, SetInput("Starter Set", "S-1", [PositionInput(1, [a])])).id
    with db.begin() as s:
        catalog.update_set(s, set_id, SetInput("Starter Set 2", "S-1", [PositionInput(3, [b])]))
    with db.begin() as s:
        row = catalog.list_sets(s)[0]
        assert (row.name, row.available) == ("Starter Set 2", 3)
        assert [p.article_ids for p in row.positions] == [[b]]
        assert len(s.scalars(select(SetPosition)).all()) == 1
        assert len(s.scalars(select(SetPositionOption)).all()) == 1


def test_get_set_unknown(db):
    with pytest.raises(NotFoundError) as info:
        with db.begin() as s:
            catalog.get_set(s, 999)
    assert info.value.key == "sets.not_found"


def test_set_sku_conflicts_with_article(db):
    with db.begin() as s:
        a = make_article(s, sku="A-100").id
    with pytest.raises(ConflictError) as info:
        with db.begin() as s:
            catalog.create_set(s, SetInput("S", " A-100 ", [PositionInput(1, [a])]))
    assert info.value.key == "catalog.sku_taken"


def test_set_keeps_own_sku_on_update(db):
    with db.begin() as s:
        a = make_article(s).id
        set_id = catalog.create_set(s, SetInput("S", "S-1", [PositionInput(1, [a])])).id
    with db.begin() as s:
        catalog.update_set(s, set_id, SetInput("S2", "S-1", [PositionInput(2, [a])]))


def test_article_sku_conflicts_with_set(db):
    with db.begin() as s:
        a = make_article(s).id
        catalog.create_set(s, SetInput("S", "X-1", [PositionInput(1, [a])]))
    with pytest.raises(ConflictError):
        with db.begin() as s:
            catalog.update_article(s, a, ArticleInput(name="Template A", sku="X-1"))
