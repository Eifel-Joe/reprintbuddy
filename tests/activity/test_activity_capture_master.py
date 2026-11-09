"""Master data in the activity log (Spec 10.7, group 3): created, changed (which fields, no values),
archived, restored; the article's own automation; target quantity; order settings; reorder marks.

Only the fields of the edit forms count; stock, pause and print file belong in the journal. Rows
deleted and written again with the same content are no change.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

from factories import make_article, make_filament_settings, make_legacy_sort, make_part, make_set, make_sort, tick_sorts
from reprintbuddy.activity import capture
from reprintbuddy.bambuddy.fake import FakeBambuddy
from reprintbuddy.bambuddy.types import Plate
from reprintbuddy.domain import catalog, filament, reorder, stock
from reprintbuddy.domain.catalog import ArticleInput, PartInput, PositionInput, SetInput
from reprintbuddy.domain.filament import FilamentSettingsInput
from reprintbuddy.domain.pauses import pause_article
from reprintbuddy.models import ActivityEntry, ArticlePart


def since(db, last: int) -> list[tuple[str, dict | None, str]]:
    with db() as s:
        rows = s.scalars(select(ActivityEntry).where(ActivityEntry.id > last).order_by(ActivityEntry.id))
        return [(row.kind, row.details, row.actor) for row in rows]


def mark(db) -> int:
    with db() as s:
        return s.scalar(select(func.max(ActivityEntry.id))) or 0


def article_input(article, **changes) -> ArticleInput:
    values = dict(
        name=article.name, sku=article.sku, label_text=article.label_text,
        units_per_print=article.units_per_print, min_stock=article.min_stock, buffer=article.buffer,
        target_model=article.target_model, printer_id=article.printer_id,
        needs_assembly=article.needs_assembly, auto_enabled=article.auto_enabled,
    )
    values.update(changes)
    return ArticleInput(**values)


def test_an_article_is_created_with_its_children_in_one_line(db):
    with db.begin() as s:
        part = make_part(s, name="Insert")
        a = make_article(s, name="Bracket", target_model="A1")
        s.add(ArticlePart(article_id=a.id, part_id=part.id, qty_per_unit=2))
        a_id = a.id
    assert since(db, 0) == [
        ("part_created", {"id": part.id, "name": "Insert"}, "system"),
        ("article_created", {"id": a_id, "name": "Bracket"}, "system"),
    ]


def test_changed_fields_without_values(db):
    with db.begin() as s:
        a = make_article(s, name="Bracket", target_model="A1")
    last = mark(db)
    with db.begin() as s:
        s.info[capture.ACTOR] = "user"
        catalog.update_article(s, a.id, article_input(a, name="Bracket 2", buffer=3, target_model="P1S"))
    assert since(db, last) == [
        ("article_changed", {"id": a.id, "name": "Bracket 2", "fields": ["name", "buffer", "target"]}, "user")
    ]


def test_the_target_quantity_alone(db):
    with db.begin() as s:
        a = make_article(s, name="Bracket", min_stock=2, buffer=1)
    last = mark(db)
    with db.begin() as s:
        catalog.set_target_quantity(s, a.id, 6)
    assert since(db, last) == [("article_target", {"id": a.id, "name": "Bracket"}, "system")]


def test_the_automation_of_an_article(db):
    with db.begin() as s:
        a = make_article(s, name="Bracket", target_model="A1")
    last = mark(db)
    with db.begin() as s:
        catalog.set_auto_enabled(s, a.id, True)
    assert since(db, last) == [("article_auto", {"id": a.id, "name": "Bracket", "on": True}, "system")]


def test_archived_and_restored(db):
    with db.begin() as s:
        a = make_article(s, name="Bracket")
        p = make_part(s, name="Insert")
        product_set = make_set(s, "Kit", [(1, [a.id])])
    last = mark(db)
    with db.begin() as s:
        catalog.archive_set(s, product_set.id)
        catalog.archive_part(s, p.id)
        catalog.archive_article(s, a.id)
    with db.begin() as s:
        catalog.restore_article(s, a.id)
    kinds = [kind for kind, _details, _actor in since(db, last)]
    assert sorted(kinds[:3]) == ["article_archived", "part_archived", "set_archived"]
    assert kinds[3:] == ["article_restored"]


def test_parts_written_again_with_the_same_content_are_no_change(db):
    with db.begin() as s:
        part = make_part(s, name="Insert")
        a = make_article(s, name="Bracket")
        s.add(ArticlePart(article_id=a.id, part_id=part.id, qty_per_unit=2))
    last = mark(db)
    with db.begin() as s:
        catalog.update_article(s, a.id, article_input(a, parts={part.id: 2}))
    assert since(db, last) == []
    with db.begin() as s:
        catalog.update_article(s, a.id, article_input(a, parts={part.id: 3}))
    assert since(db, last) == [("article_changed", {"id": a.id, "name": "Bracket", "fields": ["parts"]}, "system")]


def test_set_positions_written_again_with_the_same_content_are_no_change(db):
    with db.begin() as s:
        a = make_article(s, name="A")
        b = make_article(s, name="B", plate_id=9)
        product_set = make_set(s, "Kit", [(1, [a.id]), (2, [a.id, b.id])])
    last = mark(db)
    same = SetInput("Kit", None, [PositionInput(1, [a.id]), PositionInput(2, [a.id, b.id])])
    with db.begin() as s:
        catalog.update_set(s, product_set.id, same)
    assert since(db, last) == []
    other = SetInput("Kit", None, [PositionInput(1, [a.id]), PositionInput(2, [b.id])])
    with db.begin() as s:
        catalog.update_set(s, product_set.id, other)
    assert since(db, last) == [
        ("set_changed", {"id": product_set.id, "name": "Kit", "fields": ["positions"]}, "system")
    ]


def test_a_part_is_changed(db):
    with db.begin() as s:
        p = make_part(s, name="Insert")
    last = mark(db)
    with db.begin() as s:
        catalog.update_part(s, p.id, PartInput(name="Insert", pack_size=50, supplier="Example Supplier"))
    assert since(db, last) == [
        ("part_changed", {"id": p.id, "name": "Insert", "fields": ["pack_size", "supplier"]}, "system")
    ]


def test_the_filament_assignment_is_a_field_of_the_article(db):
    with db.begin() as s:
        a = make_article(s, name="Bracket")
        sort = make_sort(s)
    last = mark(db)
    with db.begin() as s:
        tick_sorts(s, a.id, 1, sort)
    assert since(db, last) == [("article_changed", {"id": a.id, "name": "Bracket", "fields": ["filaments"]}, "system")]


def test_order_settings_of_a_spool_sort(db):
    with db.begin() as s:
        sort = make_sort(s)
        label = sort.label
    last = mark(db)
    with db.begin() as s:
        filament.save_settings(s, sort.id, FilamentSettingsInput(lead_time_days=5))
    with db.begin() as s:
        filament.save_settings(s, sort.id, FilamentSettingsInput(lead_time_days=5))  # unchanged
    assert since(db, last) == [("filament_settings_changed", {"id": sort.id, "name": label}, "system")]


def test_marked_as_ordered_and_as_arrived_by_hand(db):
    with db.begin() as s:
        p = make_part(s, name="Insert")
    last = mark(db)
    with db.begin() as s:
        s.info[capture.ACTOR] = "user"
        m = reorder.mark_ordered(s, "part", p.id, 2)
    with db.begin() as s:
        s.info[capture.ACTOR] = "user"
        reorder.mark_received(s, m.id)
    ref = {"type": "part", "id": p.id, "name": "Insert"}
    assert since(db, last) == [("reorder_ordered", ref, "user"), ("reorder_received", ref, "user")]


def test_an_order_closed_by_itself_is_the_automation_even_on_a_page(db):
    """A part receipt closes the mark (Spec 9.4); the page of the suggestions does that in a request."""
    with db.begin() as s:
        p = make_part(s, name="Insert")
        m = reorder.mark_ordered(s, "part", p.id, 1)
    with db.begin() as s:
        stock.receive_part(s, p.id, 10, actor="user")
    last = mark(db)
    with db.begin() as s:
        s.info[capture.ACTOR] = "user"  # a page request
        assert reorder.close_arrived_marks(s, None, datetime.now(UTC)) == 1
    assert since(db, last) == [("reorder_received", {"type": "part", "id": p.id, "name": "Insert"}, "auto")]
    assert m.id


async def test_a_new_print_file_is_the_journals_not_the_logs(db):
    fake = FakeBambuddy()
    folder = fake.add_folder("Templates")
    fake.add_file("Template A.gcode.3mf", folder.id, printer_model="A1", plates=1)
    new = fake.add_file("Template B.gcode.3mf", folder.id, printer_model="A1", plates=1)
    with db.begin() as s:
        a = make_article(s, name="Bracket", library_file_id=1, plate_id=1)
        tick_sorts(s, a.id, 1, make_sort(s))
    last = mark(db)
    file = await fake.get_file(new.id)
    with db.begin() as s:
        catalog.relink_article(s, a.id, file=file, plate=Plate(1, None), requirements=[], thumbnail=None, actor="user")
    assert since(db, last) == []


def test_a_converted_spool_sort_is_the_journals_not_the_logs(db):
    fake = FakeBambuddy()
    spools = [fake.add_spool("PLA", brand="Brand A", color_hex="#000000")]
    with db.begin() as s:
        old = make_legacy_sort(s)
        make_filament_settings(s, old.id, lead_time_days=4)
        s.add(reorder_mark := __import__("reprintbuddy.models", fromlist=["ReorderMark"]).ReorderMark(
            item_type="filament", item_id=old.id, qty=1
        ))
    last = mark(db)
    with db.begin() as s:
        filament.sync_sorts(s, spools)
    assert since(db, last) == []
    assert reorder_mark.id


def test_bookings_and_pauses_are_the_journals_not_the_logs(db):
    with db.begin() as s:
        a = make_article(s, name="Bracket")
    last = mark(db)
    with db.begin() as s:
        stock.count_article(s, a.id, "stock", 5, actor="user")
        pause_article(s, a.id, "file_missing", actor="auto")
    assert since(db, last) == []
