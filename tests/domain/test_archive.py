from datetime import date

import pytest
from sqlalchemy import select

from factories import make_article, make_dispatch, make_entry, make_part
from reprintbuddy.bambuddy.types import FilamentRequirement, LibraryFile, Plate
from reprintbuddy.domain import catalog, orders
from reprintbuddy.domain.catalog import ArticleInput, PositionInput, SetInput
from reprintbuddy.domain.errors import ConflictError, ValidationError
from reprintbuddy.domain.orders import ArticleItem, SetItem
from reprintbuddy.models import Article, Dispatch, ArticleFilament, ArticlePart, JournalEntry, Part, ProductSet


def new_order(s, items, reference="Order 1001"):
    return orders.create_order(s, reference=reference, order_date=date(2026, 1, 15), items=items, actor="user")


def archive_article(s, article_id):
    catalog.archive_article(s, article_id)


def test_archive_article_blocked_by_open_order(db):
    with db.begin() as s:
        a = make_article(s, name="Template A", stock=2).id
        order_id = new_order(s, [ArticleItem(a, 1)]).id
    with pytest.raises(ConflictError) as info:
        with db.begin() as s:
            archive_article(s, a)
    assert info.value.key == "articles.archive_open_orders"
    with db.begin() as s:
        orders.cancel_order(s, order_id, "user")
    with db.begin() as s:
        archive_article(s, a)  # closed orders do not block
        assert s.get(Article, a).archived_at is not None


def test_archive_article_blocked_by_provisional(db):
    with db.begin() as s:
        a = make_article(s, provisional=1).id
    with pytest.raises(ConflictError) as info:
        with db.begin() as s:
            archive_article(s, a)
    assert info.value.key == "articles.archive_provisional"


def test_archive_article_blocked_by_awaiting_assembly(db):
    with db.begin() as s:
        a = make_article(s, awaiting_assembly=1).id
    with pytest.raises(ConflictError) as info:
        with db.begin() as s:
            archive_article(s, a)
    assert info.value.key == "articles.archive_awaiting_assembly"


def test_archive_keeps_stock(db):
    with db.begin() as s:
        a = make_article(s, stock=5).id
    with db.begin() as s:
        archive_article(s, a)
    with db.begin() as s:
        assert s.get(Article, a).stock == 5
        catalog.restore_article(s, a)
    with db.begin() as s:
        art = s.get(Article, a)
        assert (art.stock, art.archived_at) == (5, None)
        assert s.scalars(select(JournalEntry)).all() == []


def test_archived_plate_is_free(db):  # Spec 5.5
    with db.begin() as s:
        a = make_article(s, name="Template A", library_file_id=1, plate_id=1).id
        archive_article(s, a)
    with db.begin() as s:
        file = LibraryFile(1, "Template A.gcode.3mf", None, "v1", "A1")
        catalog.create_article(
            s, file=file, plate=Plate(1, None), requirements=[], thumbnail=None, data=ArticleInput(name="Template A2")
        )


def test_restore_plate_taken(db):
    with db.begin() as s:
        a = make_article(s, name="Template A", library_file_id=1, plate_id=1).id
        archive_article(s, a)
        make_article(s, name="Template A2", library_file_id=1, plate_id=1)
    with pytest.raises(ConflictError) as info:
        with db.begin() as s:
            catalog.restore_article(s, a)
    assert info.value.key == "articles.restore_plate_taken"
    assert info.value.params["name"] == "Template A2"
    with db.begin() as s:
        assert s.get(Article, a).archived_at is not None


def test_restore_with_other_plate(db):
    with db.begin() as s:
        a = make_article(s, name="Template A", library_file_id=1, plate_id=1).id
        s.add(ArticleFilament(article_id=a, slot=1, grams=10.0, material="PLA", color_hex="#000000"))
        archive_article(s, a)
        make_article(s, name="Template A2", library_file_id=1, plate_id=1)
    file_b = LibraryFile(2, "Template B.gcode.3mf", None, "v7", "A1")
    with db.begin() as s:
        catalog.restore_article_with_plate(
            s, a, file=file_b, plate=Plate(2, None),
            requirements=[FilamentRequirement(1, 20.5, "PETG", "#ffffff"), FilamentRequirement(2, 3.0, "PLA", None)],
            thumbnail=b"\x89PNG-b",
        )
    with db.begin() as s:
        art = s.get(Article, a)
        assert art.archived_at is None
        assert (art.file_name, art.library_file_id, art.plate_id, art.file_fingerprint) == ("Template B.gcode.3mf", 2, 2, "v7")
        assert art.thumbnail == b"\x89PNG-b"
        rows = s.scalars(select(ArticleFilament).where(ArticleFilament.article_id == a).order_by(ArticleFilament.slot)).all()
        assert [(r.slot, r.grams, r.material) for r in rows] == [(1, 20.5, "PETG"), (2, 3.0, "PLA")]


def test_restore_with_plate_still_taken(db):
    with db.begin() as s:
        a = make_article(s, name="Template A", library_file_id=1, plate_id=1).id
        archive_article(s, a)
        make_article(s, name="Template A2", library_file_id=1, plate_id=1)
        make_article(s, name="Template B", library_file_id=2, plate_id=2)
    with pytest.raises(ConflictError) as info:
        with db.begin() as s:
            catalog.restore_article_with_plate(
                s, a, file=LibraryFile(2, "Template B.gcode.3mf", None, "v1", "A1"), plate=Plate(2, None),
                requirements=[], thumbnail=None,
            )
    assert info.value.key == "articles.plate_taken" and info.value.params["name"] == "Template B"
    with db.begin() as s:
        assert s.get(Article, a).archived_at is not None


def test_archive_twice_and_restore_active(db):
    with db.begin() as s:
        a = make_article(s).id
        with pytest.raises(ConflictError) as info:
            catalog.restore_article(s, a)
        assert info.value.key == "catalog.not_archived"
        archive_article(s, a)
        with pytest.raises(ConflictError) as info:
            archive_article(s, a)
        assert info.value.key == "catalog.already_archived"


def test_archive_unknown(db):
    from reprintbuddy.domain.errors import NotFoundError

    with pytest.raises(NotFoundError):
        with db.begin() as s:
            archive_article(s, 999)
    with pytest.raises(NotFoundError):
        with db.begin() as s:
            catalog.archive_part(s, 999)
    with pytest.raises(NotFoundError):
        with db.begin() as s:
            catalog.archive_set(s, 999)


def test_archive_part_in_use(db):  # Spec 5.5
    with db.begin() as s:
        a = make_article(s, name="Template A")
        p = make_part(s, name="Insert M4")
        s.add(ArticlePart(article_id=a.id, part_id=p.id, qty_per_unit=2))
        ids = (a.id, p.id)
    with pytest.raises(ConflictError) as info:
        with db.begin() as s:
            catalog.archive_part(s, ids[1])
    assert info.value.key == "parts.archive_in_use" and info.value.params["article"] == "Template A"
    with db.begin() as s:
        archive_article(s, ids[0])
    with db.begin() as s:
        catalog.archive_part(s, ids[1])
        assert s.get(Part, ids[1]).archived_at is not None


def test_restore_part(db):
    with db.begin() as s:
        p = make_part(s, stock=7).id
        catalog.archive_part(s, p)
    with db.begin() as s:
        catalog.restore_part(s, p)
    with db.begin() as s:
        part = s.get(Part, p)
        assert (part.archived_at, part.stock) == (None, 7)
        with pytest.raises(ConflictError) as info:
            catalog.restore_part(s, p)
        assert info.value.key == "catalog.not_archived"


def test_restore_article_with_archived_part_rejected(db):
    with db.begin() as s:
        a = make_article(s, name="Template A")
        p = make_part(s, name="Insert M4")
        s.add(ArticlePart(article_id=a.id, part_id=p.id, qty_per_unit=1))
        ids = (a.id, p.id)
    with db.begin() as s:
        archive_article(s, ids[0])
    with db.begin() as s:
        catalog.archive_part(s, ids[1])
    with pytest.raises(ValidationError) as info:
        with db.begin() as s:
            catalog.restore_article(s, ids[0])
    assert info.value.key == "parts.archived"


def test_archive_set_always(db):  # Spec 5.5
    with db.begin() as s:
        a = make_article(s, name="Template A", stock=3).id
        set_id = catalog.create_set(s, SetInput("Starter Set", None, [PositionInput(1, [a])])).id
        order_id = new_order(s, [SetItem(set_id, 1, {})]).id
    with db.begin() as s:
        catalog.archive_set(s, set_id)
    with db.begin() as s:
        orders.ship_order(s, order_id, "user")  # the open order still ships
        assert s.get(Article, a).stock == 2


def test_restore_set(db):
    with db.begin() as s:
        a = make_article(s).id
        set_id = catalog.create_set(s, SetInput("Starter Set", "S-1", [PositionInput(1, [a])])).id
        catalog.archive_set(s, set_id)
    with db.begin() as s:
        catalog.restore_set(s, set_id)
        assert s.get(ProductSet, set_id).archived_at is None
        with pytest.raises(ConflictError):
            catalog.restore_set(s, set_id)


def test_sku_stays_reserved_while_archived(db):
    with db.begin() as s:
        a = make_article(s, sku="A-100").id
        archive_article(s, a)
    with pytest.raises(ConflictError) as info:
        with db.begin() as s:
            catalog.validate_sku(s, "A-100")
    assert info.value.key == "catalog.sku_taken"


def test_lists_hide_archived(db):
    with db.begin() as s:
        a = make_article(s, name="Template A").id
        make_article(s, name="Template B")
        p = make_part(s, name="Insert M4").id
        make_part(s, name="Hex key 2")
        set_a = catalog.create_set(s, SetInput("Set A", None, [PositionInput(1, [a])])).id
        catalog.create_set(s, SetInput("Set B", None, [PositionInput(1, [a])]))
        catalog.archive_set(s, set_a)
        catalog.archive_part(s, p)
        archive_article(s, a)
    with db.begin() as s:
        assert [r.name for r in catalog.list_articles(s)] == ["Template B"]
        assert [r.name for r in catalog.list_articles(s, archived=True)] == ["Template A"]
        assert [p.name for p in catalog.list_parts(s)] == ["Hex key 2"]
        assert [p.name for p in catalog.list_parts(s, archived=True)] == ["Insert M4"]
        assert [r.name for r in catalog.list_sets(s)] == ["Set B"]
        assert [r.name for r in catalog.list_sets(s, archived=True)] == ["Set A"]


def test_archived_not_allowed_as_new_reference(db):
    with db.begin() as s:
        a = make_article(s, name="Template A", stock=5)
        b = make_article(s, name="Template B", stock=5)
        c = make_article(s, name="Template C", stock=5)
        p = make_part(s, name="Insert M4")
        starter = catalog.create_set(s, SetInput("Starter Set", None, [PositionInput(1, [a.id])])).id
        choice = catalog.create_set(s, SetInput("Choice Set", None, [PositionInput(1, [b.id, c.id])])).id
        choice_position = [r for r in catalog.list_sets(s) if r.name == "Choice Set"][0].positions[0].id
        ids = dict(a=a.id, b=b.id, c=c.id, p=p.id, starter=starter, choice=choice, cpos=choice_position)
    with db.begin() as s:
        catalog.archive_part(s, ids["p"])
        archive_article(s, ids["c"])
        catalog.archive_set(s, ids["starter"])

    with pytest.raises(ValidationError) as info:
        with db.begin() as s:
            catalog.update_article(s, ids["a"], ArticleInput(name="Template A", parts={ids["p"]: 1}))
    assert info.value.key == "parts.archived"

    with pytest.raises(ValidationError) as info:
        with db.begin() as s:
            catalog.create_set(s, SetInput("New Set", None, [PositionInput(1, [ids["c"]])]))
    assert info.value.key == "sets.article_archived"

    cases = [
        [ArticleItem(ids["c"], 1)],
        [SetItem(ids["starter"], 1, {})],
        [SetItem(ids["choice"], 1, {ids["cpos"]: ids["c"]})],
    ]
    for items in cases:
        with pytest.raises(ValidationError) as info:
            with db.begin() as s:
                new_order(s, items)
        assert info.value.key == "orders.item_archived", items


def test_set_with_archived_option(db):
    with db.begin() as s:
        a = make_article(s, name="Template A", stock=10).id
        b = make_article(s, name="Template B", stock=3).id
        c = make_article(s, name="Template C", stock=5).id
        catalog.create_set(s, SetInput("Starter Set", None, [PositionInput(1, [a]), PositionInput(1, [b, c])]))
    with db.begin() as s:
        assert catalog.list_sets(s)[0].available == 8
        archive_article(s, c)
    with db.begin() as s:
        row = catalog.list_sets(s)[0]
        assert row.available == 3 and row.orderable is True
        assert row.positions[1].article_ids == [b] and not row.positions[1].is_choice
        archive_article(s, a)
    with db.begin() as s:
        row = catalog.list_sets(s)[0]
        assert row.orderable is False and row.available == 0


def test_order_with_remaining_single_active_option_needs_no_choice(db):
    with db.begin() as s:
        b = make_article(s, name="Template B", stock=3).id
        c = make_article(s, name="Template C", stock=5).id
        set_id = catalog.create_set(s, SetInput("Starter Set", None, [PositionInput(1, [b, c])])).id
        archive_article(s, c)
        new_order(s, [SetItem(set_id, 1, {})])
    with db.begin() as s:
        assert s.get(Article, b).reserved == 1


def test_archived_article_cannot_be_edited(db):
    with db.begin() as s:
        a = make_article(s, name="Template A").id
        archive_article(s, a)
    with pytest.raises(ConflictError) as info:
        with db.begin() as s:
            catalog.update_article(s, a, ArticleInput(name="Renamed"))
    assert info.value.key == "articles.edit_archived"
    with db.begin() as s:
        assert s.get(Article, a).name == "Template A"


def test_update_keeps_links_to_parts_that_are_not_offered(db):
    """An active article that still links an archived part keeps the link when it is edited
    (the form only offers active parts), and may name it again."""
    with db.begin() as s:
        a = make_article(s, name="Template A")
        old = make_part(s, name="Old part", archived_at=__import__("datetime").datetime(2026, 1, 1))
        new = make_part(s, name="New part")
        s.add(ArticlePart(article_id=a.id, part_id=old.id, qty_per_unit=3))
        ids = (a.id, old.id, new.id)
    with db.begin() as s:
        catalog.update_article(s, ids[0], ArticleInput(name="Template A", parts={ids[2]: 1}))
    with db.begin() as s:
        links = {l.part_id: l.qty_per_unit for l in s.scalars(select(ArticlePart))}
        assert links == {ids[1]: 3, ids[2]: 1}
        catalog.update_article(s, ids[0], ArticleInput(name="Template A", parts={ids[1]: 5}))  # already linked
    with db.begin() as s:
        assert {l.part_id: l.qty_per_unit for l in s.scalars(select(ArticlePart))} == {ids[1]: 5}
    other = None
    with db.begin() as s:
        other = make_part(s, name="Other archived", archived_at=__import__("datetime").datetime(2026, 1, 1)).id
    with pytest.raises(ValidationError) as info:
        with db.begin() as s:
            catalog.update_article(s, ids[0], ArticleInput(name="Template A", parts={other: 1}))  # new reference
    assert info.value.key == "parts.archived"


def test_archive_blocked_by_queue(db):
    with db.begin() as s:
        a = make_article(s).id
        make_entry(s, a, 1)
    with pytest.raises(ConflictError) as info:
        with db.begin() as s:
            archive_article(s, a)
    assert info.value.key == "articles.archive_in_queue"


def test_archive_blocked_by_open_dispatch(db):
    with db.begin() as s:
        a = make_article(s).id
        d = make_dispatch(s, a, status="queued").id
    with pytest.raises(ConflictError) as info:
        with db.begin() as s:
            archive_article(s, a)
    assert info.value.key == "articles.archive_in_progress"
    with db.begin() as s:
        s.get(Dispatch, d).status = "completed"
    with db.begin() as s:
        archive_article(s, a)
        assert s.get(Article, a).archived_at is not None
