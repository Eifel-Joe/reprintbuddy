"""The change feed: every committed change is reported as topics (kind and id, no content)."""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select, update

from reprintbuddy import auth
from reprintbuddy.changes import CLASSIFIED, install_change_feed
from reprintbuddy.domain import catalog, filament, orders, stock
from reprintbuddy.domain.catalog import ArticleInput, BaseInput, SetInput
from reprintbuddy.domain.errors import StockLimitError
from reprintbuddy.models import (
    Article,
    ArticleFilament,
    ArticleFilamentSort,
    Base,
    FilamentSettings,
    Inspection,
    NotifyMark,
    Part,
    ReorderMark,
)
from reprintbuddy.web.formtoken import consume_form_token, issue_form_token
from factories import (
    make_article,
    make_dispatch,
    make_entry,
    make_order,
    make_part,
    make_set,
    make_sort,
)

SRC = Path(__file__).resolve().parents[1] / "src" / "reprintbuddy"


@pytest.fixture
def published(db):
    """What the feed publishes; the data of each test is created before it is cleared."""
    sent: list[frozenset[str]] = []
    install_change_feed(db, sent.append)
    return sent


def test_inventory_booking_reports_article_and_journal(db, published):
    with db.begin() as s:
        a = make_article(s).id
    published.clear()
    with db.begin() as s:
        stock.count_article(s, a, "stock", 3, "user")
    assert published == [frozenset({"article", f"article:{a}", "journal"})]


def test_nothing_is_reported_before_the_commit(db, published):
    with db.begin() as s:
        a = make_article(s).id
    published.clear()
    with db.begin() as s:
        stock.count_article(s, a, "stock", 3, "user")
        s.flush()
        assert published == []
    assert len(published) == 1


def test_a_rolled_back_change_reports_nothing(db, published):
    with db.begin() as s:
        a = make_article(s).id
    published.clear()
    with pytest.raises(RuntimeError):
        with db.begin() as s:
            stock.count_article(s, a, "stock", 3, "user")
            s.flush()
            raise RuntimeError("boom")
    assert published == []
    with db.begin() as s:  # the next commit does not carry the discarded topics along
        stock.receive_part(s, make_part(s).id, 1, "user")
    assert all(f"article:{a}" not in topics for topics in published)


def test_a_refused_booking_reports_nothing(db, published):
    with db.begin() as s:
        a = make_article(s, stock=1).id
        order = make_order(s).id
    published.clear()
    with pytest.raises(StockLimitError):
        with db.begin() as s:
            stock.ship_for_order(s, order, {a: 5}, "user")
    assert published == []


def test_editing_settings_reports_the_settings_topic(db, published):
    with db.begin() as s:
        a = make_article(s).id
    published.clear()
    with db.begin() as s:
        catalog.update_article(s, a, ArticleInput(name="Template A", min_stock=4))
    assert f"article-settings:{a}" in published[0]


def test_removing_the_parts_of_an_article_reports_its_settings(db, published):
    with db.begin() as s:
        p = make_part(s).id
        a = catalog.update_article(s, make_article(s).id, ArticleInput(name="Template A", parts={p: 2})).id
    published.clear()
    with db.begin() as s:
        catalog.update_article(s, a, ArticleInput(name="Template A", parts={}))
    # "activity": the change is a line of the activity log too (Spec 10.7), which follows live.
    assert published == [frozenset({"article", f"article:{a}", f"article-settings:{a}", "activity"})]


def test_order_creation_reports_order_articles_and_journal(db, published):
    with db.begin() as s:
        a = make_article(s, stock=5).id
    published.clear()
    with db.begin() as s:
        order = orders.create_order(
            s,
            reference="Order 1001",
            order_date=date(2026, 1, 1),
            items=[orders.ArticleItem(a, 2)],
            actor="user",
        ).id
    assert len(published) == 1
    assert {"order", f"order:{order}", f"article:{a}", "journal"} <= published[0]


def test_set_update_reports_the_set(db, published):
    with db.begin() as s:
        a = make_article(s).id
        b = make_article(s, name="Template B").id
        set_id = make_set(s, "Set A", [(1, a)], [(2, [(b, "-B")])], sku="S-A").id
    published.clear()
    with db.begin() as s:
        catalog.update_set(s, set_id, SetInput("Set A", None, [BaseInput(3, b), BaseInput(1, a)], []))
    assert len(published) == 1
    assert {"set", f"set:{set_id}"} <= published[0]


def test_part_receipt_reports_part_and_journal(db, published):
    with db.begin() as s:
        p = make_part(s).id
    published.clear()
    with db.begin() as s:
        stock.receive_part(s, p, 10, "user")
    assert published == [frozenset({"part", f"part:{p}", "journal"})]


def test_signing_in_and_form_tokens_report_nothing(db, published):
    with db() as s:
        auth.create_session(s)
    with db.begin() as s:
        consume_form_token(s, "secret", issue_form_token("secret"))
    assert published == []


def test_one_report_per_commit(db, published):
    with db.begin() as s:
        a = make_article(s).id
        b = make_article(s, name="Template B").id
    published.clear()
    with db.begin() as s:
        stock.count_article(s, a, "stock", 3, "user")
        s.flush()
        stock.count_article(s, b, "stock", 4, "user")
    assert published == [
        frozenset({"article", f"article:{a}", f"article:{b}", "journal"})
    ]


def test_a_failing_publish_does_not_undo_the_commit(db, caplog):
    def fail(_topics):
        raise RuntimeError("broker down")

    install_change_feed(db, fail)
    with db.begin() as s:
        a = make_article(s).id
    with caplog.at_level(logging.WARNING, logger="reprintbuddy.changes"):
        with db.begin() as s:
            stock.count_article(s, a, "stock", 3, "user")
    with db() as s:
        assert s.get(Article, a).stock == 3
    assert any(r.levelno == logging.WARNING and r.name == "reprintbuddy.changes" for r in caplog.records)


def test_bulk_statements_report_the_kind(db, published):
    with db.begin() as s:
        make_part(s)
    published.clear()
    with db.begin() as s:
        s.execute(update(Part).values(unit="x"))
    assert published == [frozenset({"part"})]


def test_a_savepoint_neither_reports_nor_discards(db, published):
    with db.begin() as s:
        a = make_article(s).id
        p = make_part(s).id
    published.clear()
    with db.begin() as s:
        stock.count_article(s, a, "stock", 3, "user")
        with s.begin_nested():
            stock.receive_part(s, p, 1, "user")
        assert published == []  # released, not committed
        with pytest.raises(RuntimeError):
            with s.begin_nested():
                stock.receive_part(s, p, 1, "user")
                s.flush()
                raise RuntimeError("boom")
    assert len(published) == 1
    assert {f"article:{a}", f"part:{p}", "journal"} <= published[0]


def test_queue_entry_reports_queue_and_article(db, published):
    with db.begin() as s:
        a = make_article(s).id
    published.clear()
    with db.begin() as s:
        make_entry(s, a)
    assert published == [frozenset({"queue", "article", f"article:{a}"})]


def test_dispatch_reports_queue_and_article(db, published):
    with db.begin() as s:
        a = make_article(s).id
    published.clear()
    with db.begin() as s:
        make_dispatch(s, a)
    assert published == [frozenset({"queue", "article", f"article:{a}"})]


def test_inspection_reports_article(db, published):
    with db.begin() as s:
        a = make_article(s).id
        d = make_dispatch(s, a).id
    published.clear()
    with db.begin() as s:
        s.add(Inspection(dispatch_id=d, article_id=a, units=1))
    assert published == [frozenset({"inspection", "article", f"article:{a}"})]


def test_every_table_is_classified():
    unclassified = {m.class_.__name__ for m in Base.registry.mappers} - {c.__name__ for c in CLASSIFIED}
    assert not unclassified, f"give these tables topics in changes.py or ignore them: {unclassified}"


def _sources(root: Path, skip: tuple[str, ...] = ()) -> list[Path]:
    files = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        if not any(relative == s or relative.startswith(s) for s in skip):
            files.append(path)
    return files


_TEXT_CALL = re.compile(r"""\btext\(\s*[rRbBuU]?("{3}|'{3}|"|')(.*?)\1""", re.S)
_WRITE_WORD = re.compile(r"\b(insert|update|delete)\b", re.I)


def test_no_raw_sql_writes_outside_migrations():
    offenders = []
    for path in _sources(SRC, skip=("migrations/", "db.py", "ops/")):
        source = path.read_text(encoding="utf-8")
        if "exec_driver_sql" in source:
            offenders.append(f"{path.name}: exec_driver_sql")
        for match in _TEXT_CALL.finditer(source):
            if _WRITE_WORD.search(match.group(2)):
                offenders.append(f"{path.name}: {match.group(0)[:60]}")
    assert not offenders, f"raw SQL writes bypass the change feed: {offenders}"


def test_domain_writes_row_by_row():
    bulk = re.compile(r"execute\(\s*(delete|update|insert)\(")
    offenders = [
        path.name
        for path in _sources(SRC / "domain")
        if bulk.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"row by row, so the change feed can name the row: {offenders}"


def test_the_rule_checks_find_what_they_look_for():
    """The source checks above would pass on anything if their patterns were wrong."""
    assert _TEXT_CALL.search('conn.execute(text("DELETE FROM parts"))')
    assert _WRITE_WORD.search(_TEXT_CALL.search("text('update parts set x=1')").group(2))
    assert not _TEXT_CALL.search('_text("DELETE")')
    assert re.search(r"execute\(\s*(delete|update|insert)\(", "session.execute(delete(Part))")


def test_feed_is_scoped_to_its_sessionmaker(db, published):
    """Another sessionmaker on the same engine does not report through this feed."""
    from reprintbuddy.db import make_sessionmaker

    other = make_sessionmaker(db.kw["bind"])
    with other.begin() as s:
        make_part(s)
    assert published == []
    with db() as s:
        assert s.scalar(select(Part.id)) is not None


def test_filament_sorts_and_reorder_marks_report_their_kind(db, published):
    with db.begin() as s:
        make_sort(s)
    assert published == [frozenset({"filament"})]
    published.clear()
    with db.begin() as s:
        s.add(ReorderMark(item_type="filament", item_id=1, qty=1))
    assert published == [frozenset({"reorder", "activity"})]  # "marked as ordered" in the activity log


def test_filament_settings_report_their_own_topic(db, published):
    with db.begin() as s:
        sort_id = make_sort(s).id
    published.clear()
    with db.begin() as s:
        s.add(FilamentSettings(spool_filament_id=sort_id))
    assert published == [frozenset({"filament", f"filament-settings:{sort_id}"})]
    published.clear()
    with db.begin() as s:
        make_sort(s, color_hex="#ffffff")  # another sort or a snapshot does not touch this form
    assert all(f"filament-settings:{sort_id}" not in topics for topics in published)


def test_saving_filament_settings_reports_only_a_real_change(db, published):
    with db.begin() as s:
        sort_id = make_sort(s).id
        s.add(FilamentSettings(spool_filament_id=sort_id))
    published.clear()
    with db.begin() as s:
        s.get(FilamentSettings, sort_id).safety_value = 300
    assert published == [frozenset({"filament", f"filament-settings:{sort_id}", "activity"})]
    published.clear()
    with db.begin() as s:
        s.get(FilamentSettings, sort_id).safety_value = 300  # same value
    assert published == []


def test_a_supplier_of_a_spool_sort_reports_its_settings_form(db, published):
    """Spec 9.3 (p281): the suppliers are part of the sort's order settings; an open form hears of
    their change; the reorder page follows "filament" for its lines per supplier."""
    from reprintbuddy.domain.filament import FilamentSettingsInput
    from reprintbuddy.domain.suppliers import SupplierInput

    with db.begin() as s:
        sort_id = make_sort(s).id
        filament.save_settings(s, sort_id, FilamentSettingsInput())
    published.clear()
    with db.begin() as s:
        filament.save_settings(s, sort_id, FilamentSettingsInput(suppliers=[
            SupplierInput(pack_size=1000, is_default=True), SupplierInput(name="Roll Shop", pack_size=500),
        ]))
    assert published == [frozenset({"filament", f"filament-settings:{sort_id}", "activity"})]


def test_a_ticked_spool_sort_reports_the_article_and_the_filament_page(db, published):
    with db.begin() as s:
        a = make_article(s).id
        s.add(ArticleFilament(article_id=a, slot=1, grams=10.0, material="PLA", color_hex="#000000"))
        sort_id = make_sort(s).id
    published.clear()
    with db.begin() as s:
        s.add(ArticleFilamentSort(article_id=a, slot=1, sort_id=sort_id, reordered=True))
    expected = frozenset({"article", f"article:{a}", f"article-settings:{a}", "filament", "activity"})
    assert published == [expected]
    published.clear()
    with db.begin() as s:
        s.delete(s.get(ArticleFilamentSort, (a, 1, sort_id)))
    assert published == [expected]


def test_notify_marks_report_nothing(db, published):
    """The marks of the push messages are bookkeeping that no page shows (Spec 10.6): writing them
    at every tick must not make open pages fetch their regions again."""
    with db.begin() as s:
        s.add(NotifyMark(kind="paused", ref="1", state="due"))
    with db.begin() as s:
        s.scalars(select(NotifyMark)).one().state = "sent"
    assert published == []


def test_the_activity_log_reports_its_topic_and_its_marks_nothing(db, published):
    """The page of the activity log follows live (Spec 10.7, 10.5); the marks of the logged states
    are bookkeeping that no page shows."""
    from reprintbuddy.models import ActivityEntry, ActivityMark

    with db.begin() as s:
        s.add(ActivityEntry(area="login", kind="login", actor="user"))
    with db.begin() as s:
        s.add(ActivityMark(key="bambuddy.connection", state="ok"))
    assert published == [frozenset({"activity"})]


def test_a_supplier_of_a_part_reports_the_part_and_its_settings(db, published):
    """Spec 5.3: the suppliers are part of the part's form; an open form hears of their change."""
    from reprintbuddy.domain.catalog import PartInput, SupplierInput

    with db.begin() as s:
        p = make_part(s).id
    published.clear()
    with db.begin() as s:
        catalog.update_part(s, p, PartInput(
            name="Insert M4", suppliers=[SupplierInput(is_default=True), SupplierInput(name="Example Supplier B")]
        ))
    assert published == [frozenset({"part", f"part:{p}", f"part-settings:{p}", "activity"})]
