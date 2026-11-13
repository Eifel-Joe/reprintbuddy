"""Archiving, switching and sets with articles made of parts (Spec 5.5, 5.6, 5.4; 0.4.0 p341)."""

from datetime import date

import pytest
from sqlalchemy import select

from factories import make_article, make_assembled, make_entry
from reprintbuddy.bambuddy.types import FilamentRequirement, LibraryFile, Plate
from reprintbuddy.domain import assembled, catalog, orders, stock
from reprintbuddy.domain.assembled import AssembledInput
from reprintbuddy.domain.catalog import BaseInput, ChoiceInput, ChoiceOptionInput, SetInput
from reprintbuddy.domain.errors import ConflictError, ValidationError
from reprintbuddy.domain.orders import ArticleItem, AssembledItem
from reprintbuddy.models import Article, ArticleComponent, ArticleFilament, JournalEntry


def hose(s, *, mode="order"):
    segment = make_article(s, name="Segment")
    side = make_article(s, name="Side part L")
    s32 = make_article(s, name="Socket L32")
    s40 = make_article(s, name="Socket L40")
    composite = make_assembled(s, name="Hose", sku="EX-H1", mode=mode, base=[(segment.id, 12)], fields=[[
        ("-L32", [(side.id, 1), (s32.id, 1)]), ("-L40", [(side.id, 1), (s40.id, 1)]),
    ]])
    return composite, segment


def test_part_in_active_assembled_cannot_be_archived(db):  # Spec 5.5
    with db.begin() as s:
        composite, segment = hose(s)
        with pytest.raises(ConflictError) as err:
            catalog.archive_article(s, segment.id)
        assert (err.value.key, err.value.params["names"]) == ("articles.archive_in_assembled", "Hose")


def test_assembled_with_open_order_cannot_be_archived(db):
    with db.begin() as s:
        composite, _segment = hose(s)
        key = assembled.variants(s, composite)[0].variant_key
        orders.create_order(s, reference="R-1", order_date=date(2026, 11, 11),
                            items=[AssembledItem(composite.id, 1, key)], actor="user")
        with pytest.raises(ConflictError) as err:
            catalog.archive_article(s, composite.id)
        assert err.value.key == "articles.archive_open_orders"


def test_stock_holder_with_stock_in_an_order_cannot_be_archived(db):
    with db.begin() as s:
        panel = make_article(s, name="Panel A")
        box = make_assembled(s, name="Storage box", base=[(panel.id, 2)], stock=1)
        orders.create_order(s, reference="R-1", order_date=date(2026, 11, 11), items=[ArticleItem(box.id, 1)], actor="user")
        with pytest.raises(ConflictError):
            catalog.archive_article(s, box.id)


def test_archived_assembled_hides_its_variants_everywhere(db):  # review focus 5
    with db.begin() as s:
        composite, segment = hose(s, mode="stock")
        rows = assembled.variants(s, composite)
        rows[0].min_stock = 2  # would make demand
        s.flush()
        catalog.archive_article(s, composite.id)
        assert all(r.archived_at == composite.archived_at for r in rows)
        listed = {r.id for r in catalog.list_articles(s)}
        assert listed.isdisjoint({composite.id, *(r.id for r in rows)})
        assert assembled.assembly_demand(s) == {}
        archived = {r.id for r in catalog.list_articles(s, archived=True)}
        assert composite.id in archived and archived.isdisjoint({r.id for r in rows})
        with pytest.raises(ValidationError):
            orders.create_order(s, reference="R-1", order_date=date(2026, 11, 11),
                                items=[ArticleItem(rows[0].id, 1)], actor="user")
        # the numbers stay taken
        with pytest.raises(ConflictError):
            catalog.validate_sku(s, "EX-H1-L32")


def test_restore_brings_variants_back(db):
    with db.begin() as s:
        composite, _segment = hose(s, mode="stock")
        rows = assembled.variants(s, composite)
        catalog.archive_article(s, composite.id)
        catalog.restore_article(s, composite.id)
        assert composite.archived_at is None
        assert [r.id for r in assembled.variants(s, composite)] == [r.id for r in rows]
        assert all(r.archived_at is None for r in rows)


def test_restore_keeps_variants_dropped_before_archived(db):
    with db.begin() as s:
        composite, segment = hose(s, mode="stock")
        rows = assembled.variants(s, composite)
        options = assembled.options(s, composite)[0]
        side, s32 = (c.part_id for c in s.scalars(select(ArticleComponent).where(ArticleComponent.option_id == options[0].id)))
        assembled.update_assembled(s, composite.id, AssembledInput(
            name="Hose", sku="EX-H1", mode="stock", base=[BaseInput(12, segment.id)],
            fields=[[assembled.OptionInput(options[0].id, "-L32", [BaseInput(1, side), BaseInput(1, s32)]),
                     assembled.OptionInput(None, "-X", [BaseInput(1, make_article(s, name="Socket X").id)])]],
            parts={},
        ))
        dropped = rows[1]
        assert dropped.archived_at is not None
        catalog.archive_article(s, composite.id)
        catalog.restore_article(s, composite.id)
        assert dropped.archived_at is not None
        assert [r.sku for r in assembled.variants(s, composite)] == ["EX-H1-L32", "EX-H1-X"]


def _file(file_id=7, name="Lid.gcode.3mf"):
    return LibraryFile(id=file_id, filename=name, folder_id=1, fingerprint="v1", printer_model="A1")


def test_print_to_assembled_keeps_name_number_and_journal(db):  # Spec 5.6 "Wechsel"
    with db.begin() as s:
        article = make_article(s, name="Lamp", sku="EX-L1", auto_enabled=True, target_model="A1", thumbnail=b"x")
        s.add(ArticleFilament(article_id=article.id, slot=1, grams=10.0))
        stock.count_article(s, article.id, "stock", 0, "user", note="before")
        clip = make_article(s, name="Clip")
        assembled.convert_to_assembled(s, article.id, AssembledInput(
            name="Lamp", sku="EX-L1", mode="stock", base=[BaseInput(2, clip.id)], fields=[], parts={},
        ))
        assert (article.kind, article.sku, article.library_file_id, article.thumbnail) == ("assembled", "EX-L1", None, None)
        assert (article.auto_enabled, article.paused) == (False, False)
        assert s.scalars(select(ArticleFilament).where(ArticleFilament.article_id == article.id)).all() == []
        assert s.scalars(select(JournalEntry).where(JournalEntry.article_id == article.id)).all()


def test_switch_refused_with_stock_or_running_prints(db):
    with db.begin() as s:
        article = make_article(s, name="Lamp", stock=1)
        clip = make_article(s, name="Clip")
        data = AssembledInput(name="Lamp", sku="EX-LAMP", mode="stock", base=[BaseInput(2, clip.id)], fields=[], parts={})
        with pytest.raises(ConflictError) as err:
            assembled.convert_to_assembled(s, article.id, data)
        assert err.value.key == "assembled.switch_busy"
        article.stock = 0
        make_entry(s, article.id, 1)
        with pytest.raises(ConflictError) as err:
            assembled.convert_to_assembled(s, article.id, data)
        assert err.value.key == "articles.archive_in_queue"


def test_part_of_an_assembled_article_cannot_become_one(db):
    with db.begin() as s:
        _composite, segment = hose(s)
        clip = make_article(s, name="Clip")
        with pytest.raises(ConflictError) as err:
            assembled.convert_to_assembled(s, segment.id, AssembledInput(
                name="Segment", sku="EX-SEGMENT", mode="stock", base=[BaseInput(2, clip.id)], fields=[], parts={},
            ))
        assert err.value.key == "articles.archive_in_assembled"


def test_assembled_back_to_print_via_plate(db):
    with db.begin() as s:
        clip = make_article(s, name="Clip")
        lamp = make_assembled(s, name="Lamp", sku="EX-L1", base=[(clip.id, 2)])
        assembled.convert_to_print(
            s, lamp.id, file=_file(), plate=Plate(index=1, name=None),
            requirements=[FilamentRequirement(1, 5.0, "PLA", "#000000")], thumbnail=b"t", actor="user",
        )
        assert (lamp.kind, lamp.assembly_mode, lamp.library_file_id, lamp.plate_id, lamp.sku) == ("print", None, 7, 1, "EX-L1")
        assert s.scalars(select(ArticleComponent).where(ArticleComponent.article_id == lamp.id)).all() == []
        assert len(s.scalars(select(ArticleFilament).where(ArticleFilament.article_id == lamp.id)).all()) == 1


def test_set_refuses_part_not_sold_alone_and_order_mode_assembled(db):  # Spec 5.4
    with db.begin() as s:
        lid = make_article(s, name="Lid")
        part = make_article(s, name="Side part L", sold_alone=False)
        composite, _segment = hose(s)
        panel = make_article(s, name="Panel A")
        box = make_assembled(s, name="Storage box", base=[(panel.id, 2)])
        catalog.create_set(s, SetInput("Starter", "EX-STARTER", [BaseInput(1, box.id), BaseInput(1, lid.id)], []))
        for other in (part, composite, assembled.variants(s, composite)[0]):
            with pytest.raises(ValidationError) as err:
                catalog.create_set(s, SetInput("Kit", "EX-KIT", [BaseInput(1, lid.id), BaseInput(1, other.id)], []))
            assert err.value.key == "sets.article_not_allowed"
        with pytest.raises(ValidationError):
            catalog.create_set(s, SetInput("Kit", "EX-S9", [BaseInput(1, lid.id)],
                                           [ChoiceInput(1, [ChoiceOptionInput(part.id, "-A"), ChoiceOptionInput(panel.id, "-B")])]))
