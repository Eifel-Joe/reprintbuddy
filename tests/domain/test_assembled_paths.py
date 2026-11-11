"""Print-only paths, labels and reorder demand with articles made of parts (0.4.0 p341)."""

from datetime import date

import pytest

from factories import make_article, make_assembled, make_part
from reprintbuddy.domain import assembled, orders, reorder, workqueue
from reprintbuddy.domain.errors import ValidationError
from reprintbuddy.domain.orders import AssembledItem
from reprintbuddy.models import ArticleFilament, ArticleFilamentSort, ArticlePart
from reprintbuddy.web import labels_view

from factories import make_sort


# ---- Aufgabe 9: printing and labels --------------------------------------------------------------


def test_manual_entry_only_for_print_articles(db):  # Spec 7.4
    with db.begin() as s:
        panel = make_article(s, name="Panel A")
        box = make_assembled(s, name="Storage box", base=[(panel.id, 2)])
        with pytest.raises(ValidationError) as err:
            workqueue.add_entry(s, box.id, 1, "manual", "user")
        assert err.value.key == "queue.print_only"
        workqueue.add_entry(s, panel.id, 1, "manual", "user")


def test_label_of_assembled_leaves_print_fields_empty(db):  # Teil C 9
    with db.begin() as s:
        panel = make_article(s, name="Panel A")
        side = make_article(s, name="Side part L")
        box = make_assembled(s, name="Storage box", sku="EX-B1", base=[(panel.id, 2)], min_stock=3, buffer=1)
        data = labels_view.article_data(box, "en")
        assert (data.units_per_print, data.print_target) == (None, None)
        assert (data.min_stock, data.buffer, data.target_qty) == (3, 1, 4)
        hose = make_assembled(s, name="Hose", sku="EX-H1", base=[(panel.id, 12)],
                              fields=[[("-L", [(side.id, 1)]), ("-R", [(make_article(s, name="R").id, 1)])]])
        data = labels_view.article_data(hose, "en", has_choices=True)
        assert (data.min_stock, data.buffer, data.target_qty) == (None, None, None)
        printed = labels_view.article_data(panel, "en")
        assert printed.units_per_print == 1


# ---- Aufgabe 10: reorder ------------------------------------------------------------------------


def test_filament_demand_includes_assembly_demand(db):  # Spec 9.2
    with db.begin() as s:
        panel = make_article(s, name="Panel A", units_per_print=1)
        s.add(ArticleFilament(article_id=panel.id, slot=1, grams=10.0, material="PLA", color_hex="#000000"))
        sort = make_sort(s)
        s.add(ArticleFilamentSort(article_id=panel.id, slot=1, sort_id=sort.id, reordered=True))
        lid = make_article(s, name="Lid")
        make_assembled(s, name="Storage box", base=[(panel.id, 2), (lid.id, 1)], min_stock=1, buffer=1)
        s.flush()
        assert reorder.filament_demand(s)[sort.id] == 10.0 * 4  # box misses 2 -> 4 panels


def test_purchased_parts_of_stock_assembled_by_missing(db):
    with db.begin() as s:
        panel = make_article(s, name="Panel A", stock=100)
        insert = make_part(s, name="Thread insert")
        box = make_assembled(s, name="Storage box", base=[(panel.id, 2)], min_stock=2, buffer=1, stock=1)
        s.add(ArticlePart(article_id=box.id, part_id=insert.id, qty_per_unit=4))
        s.flush()
        assert reorder.part_demand(s)[insert.id] == 4 * 2  # target 3 - available 1


def test_purchased_parts_of_order_assembled_by_open_orders(db):
    with db.begin() as s:
        segment = make_article(s, name="Segment")
        clamp = make_part(s, name="Clamp")
        hose = make_assembled(s, name="Hose", mode="order", base=[(segment.id, 12)])
        s.add(ArticlePart(article_id=hose.id, part_id=clamp.id, qty_per_unit=2))
        s.flush()
        orders.create_order(s, reference="R-1", order_date=date(2026, 11, 11),
                            items=[AssembledItem(hose.id, 3)], actor="user")
        assert reorder.part_demand(s)[clamp.id] == 6


def test_consumption_counts_assembly_and_shipping(db):
    with db.begin() as s:
        panel = make_article(s, name="Panel A", stock=10)
        insert = make_part(s, name="Thread insert", stock=20)
        box = make_assembled(s, name="Storage box", base=[(panel.id, 2)])
        s.add(ArticlePart(article_id=box.id, part_id=insert.id, qty_per_unit=4))
        s.flush()
        assembled.build(s, box.id, 2, "user")
        assert reorder.part_consumption(s, insert.id, date.today()) > 0
