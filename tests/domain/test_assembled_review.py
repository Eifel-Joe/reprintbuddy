"""Findings of the review of p341 (0.4.0), each with the case that showed it."""

from datetime import date

import pytest
from sqlalchemy import select

from factories import make_article, make_assembled
from reprintbuddy.bambuddy.types import LibraryFile, Plate
from reprintbuddy.domain import assembled, catalog, orders, stock
from reprintbuddy.domain.assembled import AssembledInput, OptionInput
from reprintbuddy.domain.catalog import ArticleInput, BaseInput, SetInput
from reprintbuddy.domain.errors import ConflictError, NotFoundError, ValidationError
from reprintbuddy.domain.orders import AssembledItem
from reprintbuddy.models import Article


def B(qty, article_id):
    return BaseInput(qty, article_id)


def _order(s, item):
    return orders.create_order(s, reference="R-1", order_date=date(2026, 11, 11), items=[item], actor="user")


# ---- finding 1: an open order of an article built on order without a number ----------------------


def test_mode_switch_refused_with_open_order_without_number(db):
    with db.begin() as s:
        segment = make_article(s, name="Segment", stock=24)
        hose = assembled.create_assembled(s, AssembledInput(
            name="Hose", sku=None, mode="order", base=[B(12, segment.id)], fields=[], parts={}))
        _order(s, AssembledItem(hose.id, 1))
        with pytest.raises(ConflictError) as err:
            assembled.update_assembled(s, hose.id, AssembledInput(
                name="Hose", sku=None, mode="stock", base=[B(12, segment.id)], fields=[], parts={}))
        assert err.value.key == "assembled.mode_switch_busy"
        with pytest.raises(ConflictError) as err:
            assembled.convert_to_print(
                s, hose.id, file=LibraryFile(id=9, filename="h.gcode.3mf", folder_id=1, fingerprint="v1", printer_model="A1"),
                plate=Plate(index=1, name=None), requirements=[], thumbnail=None, actor="user",
            )
        assert err.value.key == "assembled.switch_busy"


# ---- finding 2: a set keeps only what a set may hold ----------------------------------------------


def test_article_in_a_set_cannot_get_choices_or_order_mode(db):
    with db.begin() as s:
        panel = make_article(s, name="Panel A")
        clip = make_article(s, name="Clip")
        lid = make_article(s, name="Lid")
        box = make_assembled(s, name="Storage box", sku="EX-B1", base=[(panel.id, 2)])
        catalog.create_set(s, SetInput("Starter", None, [B(1, box.id), B(1, clip.id)], []))
        for change in (
            dict(mode="order"),
            dict(fields=[[OptionInput(None, "-A", [B(1, lid.id)]), OptionInput(None, "-B", [B(1, clip.id)])]]),
        ):
            data = AssembledInput(name="Storage box", sku="EX-B1", mode="stock", base=[B(2, panel.id)], fields=[], parts={})
            for key, value in change.items():
                setattr(data, key, value)
            with pytest.raises(ConflictError) as err:
                assembled.update_assembled(s, box.id, data)
            assert (err.value.key, err.value.params["names"]) == ("assembled.in_set", "Starter")


def test_sold_alone_off_refused_while_in_a_set(db):
    with db.begin() as s:
        clip = make_article(s, name="Clip")
        lid = make_article(s, name="Lid")
        catalog.create_set(s, SetInput("Starter", None, [B(1, lid.id), B(1, clip.id)], []))
        with pytest.raises(ConflictError) as err:
            catalog.update_article(s, clip.id, ArticleInput(name="Clip", sold_alone=False))
        assert (err.value.key, err.value.params["names"]) == ("articles.sold_alone_in_set", "Starter")


# ---- finding 4: an option id handed out again ------------------------------------------------------


def _bracket(s):
    a, b, c = (make_article(s, name=n).id for n in ("Part A", "Part B", "Part C"))
    composite = assembled.create_assembled(s, AssembledInput(
        name="Bracket", sku="EX-K1", mode="stock", base=[B(2, a)],
        fields=[[OptionInput(None, "-B", [B(1, b)]), OptionInput(None, "-C", [B(1, c)])]], parts={}))
    return composite, a, b, c


def test_a_dropped_variant_is_not_revived_by_a_reused_option_id(db):
    with db.begin() as s:
        composite, a, b, c = _bracket(s)
        rows = {r.sku: r.id for r in assembled.variants(s, composite)}
        assembled.update_assembled(s, composite.id, AssembledInput(  # both options go: their rows are archived
            name="Bracket", sku="EX-K1", mode="stock", base=[B(2, a)], fields=[], parts={}))
        assembled.update_assembled(s, composite.id, AssembledInput(  # "-C" comes back as a new option
            name="Bracket", sku="EX-K1", mode="stock", base=[B(2, a)],
            fields=[[OptionInput(None, "-C", [B(1, c)]), OptionInput(None, "-D", [B(1, b)])]], parts={}))
        back = {r.sku: r.id for r in assembled.variants(s, composite)}
        assert back["EX-K1-C"] == rows["EX-K1-C"]  # the row of that number, whatever id the option got
        assert s.get(Article, rows["EX-K1-B"]).archived_at is not None


def test_the_same_option_id_twice_is_two_options(db):
    with db.begin() as s:
        composite, a, b, c = _bracket(s)
        first = assembled.options(s, composite)[0][0].id
        assembled.update_assembled(s, composite.id, AssembledInput(
            name="Bracket", sku="EX-K1", mode="stock", base=[B(2, a)],
            fields=[[OptionInput(first, "-B", [B(1, b)]), OptionInput(first, "-E", [B(1, c)])]], parts={}))
        assert sorted(r.sku for r in assembled.variants(s, composite)) == ["EX-K1-B", "EX-K1-E"]


# ---- finding 5, 6: rows that are no print article on the print paths ------------------------------


def test_a_variant_row_is_neither_restored_nor_edited_on_its_own(db):
    with db.begin() as s:
        composite, a, b, c = _bracket(s)
        variant = assembled.variants(s, composite)[0]
        catalog.archive_article(s, composite.id)
        with pytest.raises(NotFoundError):
            catalog.restore_article(s, variant.id)
        catalog.restore_article(s, composite.id)
        for target in (variant.id, composite.id):
            with pytest.raises(NotFoundError):
                catalog.update_article(s, target, ArticleInput(name="X", sold_alone=False))


def test_an_article_is_not_its_own_part(db):
    with db.begin() as s:
        lamp = make_article(s, name="Lamp")
        clip = make_article(s, name="Clip")
        with pytest.raises(ValidationError) as err:
            assembled.convert_to_assembled(s, lamp.id, AssembledInput(
                name="Lamp", sku=None, mode="stock", base=[B(1, lamp.id), B(1, clip.id)], fields=[], parts={}))
        assert err.value.key == "assembled.part_is_self"


# ---- finding 7: a suffix removed and typed again in one save -------------------------------------


def test_suffix_removed_and_added_again_in_one_save_keeps_its_row(db):
    with db.begin() as s:
        composite, a, b, c = _bracket(s)
        rows = {r.sku: r.id for r in assembled.variants(s, composite)}
        options = assembled.options(s, composite)[0]
        assembled.update_assembled(s, composite.id, AssembledInput(
            name="Bracket", sku="EX-K1", mode="stock", base=[B(2, a)],
            fields=[[OptionInput(options[1].id, "-C", [B(1, c)]), OptionInput(None, "-B", [B(1, b)])]], parts={}))
        assert {r.sku: r.id for r in assembled.variants(s, composite)} == rows


# ---- finding 8: "awaiting assembly" exists only for print articles -------------------------------


def test_awaiting_assembly_is_not_counted_on_an_assembled_article(db):
    with db.begin() as s:
        panel = make_article(s, name="Panel A")
        box = make_assembled(s, name="Storage box", base=[(panel.id, 2)])
        with pytest.raises(ValidationError) as err:
            stock.count_article(s, box.id, "awaiting_assembly", 3, "user")
        assert err.value.key == "bookings.awaiting_print_only"


# ---- finding 9: the page journal keeps dropped variants -----------------------------------------


def test_journal_ids_cover_every_variant_row(db):
    with db.begin() as s:
        composite, a, b, c = _bracket(s)
        all_rows = {r.id for r in s.scalars(select(Article).where(Article.parent_id == composite.id))}
        assembled.update_assembled(s, composite.id, AssembledInput(
            name="Bracket", sku="EX-K1", mode="stock", base=[B(2, a)], fields=[], parts={}))
        assert set(assembled.journal_ids(s, composite)) == {composite.id} | all_rows


def test_print_article_in_a_set_becomes_assembled_only_as_set_material(db):  # sister path of finding 2
    with db.begin() as s:
        lamp = make_article(s, name="Lamp")
        clip = make_article(s, name="Clip")
        lid = make_article(s, name="Lid")
        catalog.create_set(s, SetInput("Starter", None, [B(1, lamp.id), B(1, lid.id)], []))
        with pytest.raises(ConflictError) as err:
            assembled.convert_to_assembled(s, lamp.id, AssembledInput(
                name="Lamp", sku=None, mode="order", base=[B(2, clip.id)], fields=[], parts={}))
        assert err.value.key == "assembled.in_set"
        assembled.convert_to_assembled(s, lamp.id, AssembledInput(
            name="Lamp", sku=None, mode="stock", base=[B(2, clip.id)], fields=[], parts={}))
