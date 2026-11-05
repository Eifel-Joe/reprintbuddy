import pytest
from sqlalchemy import func, select

from factories import make_article, make_order, make_part
from reprintbuddy.domain import stock
from reprintbuddy.domain.errors import NotFoundError, StockLimitError, ValidationError
from reprintbuddy.domain.rules import available
from reprintbuddy.domain.stock import PartShortage
from reprintbuddy.models import Article, ArticlePart, JournalEntry, Part, utcnow


def journal(s, kind=None):
    q = select(JournalEntry).order_by(JournalEntry.id)
    if kind:
        q = q.where(JournalEntry.kind == kind)
    return list(s.scalars(q))


def count_journal(s):
    return s.scalar(select(func.count()).select_from(JournalEntry))


def with_parts(s, article, insert_stock=50, hex_stock=10):
    insert = make_part(s, name="Insert M4", stock=insert_stock)
    hex_key = make_part(s, name="Hex key 2", stock=hex_stock)
    s.add(ArticlePart(article_id=article.id, part_id=insert.id, qty_per_unit=2))
    s.add(ArticlePart(article_id=article.id, part_id=hex_key.id, qty_per_unit=1))
    s.flush()
    return insert, hex_key


def test_reserve_and_release(db):
    with db.begin() as s:
        a = make_article(s).id
        order = make_order(s).id
        stock.reserve_for_order(s, order, {a: 2}, "user")
    with db.begin() as s:
        assert s.get(Article, a).reserved == 2
        stock.release_for_order(s, order, {a: 2}, "user")
    with db.begin() as s:
        assert s.get(Article, a).reserved == 0
        created, cancelled = journal(s, "order_created")[0], journal(s, "order_cancelled")[0]
        assert (created.qty, created.order_id, created.actor) == (2, order, "user")
        assert cancelled.qty == 2


def test_ship_reduces_stock_and_reserved(db):
    with db.begin() as s:
        a = make_article(s, stock=3, reserved=2).id
        stock.ship_for_order(s, make_order(s).id, {a: 2}, "user")
    with db.begin() as s:
        art = s.get(Article, a)
        assert (art.stock, art.reserved) == (1, 0)
        rows = journal(s)
        assert len(rows) == 1
        assert rows[0].details["changes"] == {"stock": [3, 1], "reserved": [2, 0]}


def test_ship_without_stock_rejected(db):
    with db.begin() as s:
        a = make_article(s, stock=1, reserved=2).id
    with pytest.raises(StockLimitError) as info:
        with db.begin() as s:
            stock.ship_for_order(s, 1, {a: 2}, "user")
    assert info.value.key == "stock.limit.stock"
    with db.begin() as s:
        art = s.get(Article, a)
        assert (art.stock, art.reserved) == (1, 2)
        assert count_journal(s) == 0


def test_ship_rejects_atomically_across_articles(db):
    with db.begin() as s:
        a = make_article(s, name="Template A", stock=5, reserved=1).id
        b = make_article(s, name="Template B", stock=0, reserved=1).id
    with pytest.raises(StockLimitError):
        with db.begin() as s:
            stock.ship_for_order(s, 1, {a: 1, b: 1}, "user")
    with db.begin() as s:
        assert s.get(Article, a).stock == 5
        assert count_journal(s) == 0


def test_sale_may_make_available_negative(db):  # Spec 6.4
    with db.begin() as s:
        a = make_article(s, stock=3, reserved=2).id
        stock.sell_unreserved(s, a, 2, "user")
    with db.begin() as s:
        art = s.get(Article, a)
        assert art.stock == 1
        assert available(art.stock, art.reserved) == -1


def test_sale_over_stock_rejected(db):
    with db.begin() as s:
        a = make_article(s, stock=1).id
    with pytest.raises(StockLimitError) as info:
        with db.begin() as s:
            stock.sell_unreserved(s, a, 2, "user")
    assert info.value.key == "stock.limit.stock"
    with db.begin() as s:
        assert s.get(Article, a).stock == 1
        assert count_journal(s) == 0


def test_release_more_than_reserved_rejected(db):
    with db.begin() as s:
        a = make_article(s, reserved=1).id
    with pytest.raises(StockLimitError) as info:
        with db.begin() as s:
            stock.release_for_order(s, 1, {a: 2}, "user")
    assert info.value.key == "stock.limit.reserved"


def test_stock_in_without_parts(db):
    with db.begin() as s:
        a = make_article(s)
        insert, _ = with_parts(s, a)
        a_id, insert_id = a.id, insert.id
        result = stock.stock_in(s, a_id, 4, "user", consume_parts=False)
    assert result == []
    with db.begin() as s:
        assert s.get(Article, a_id).stock == 4
        assert s.get(Part, insert_id).stock == 50
        assert [r.kind for r in journal(s)] == ["stock_in"]


def test_stock_in_consumes_parts(db):  # Spec 6.2, 6.6
    with db.begin() as s:
        a = make_article(s)
        insert, hex_key = with_parts(s, a)
        ids = (a.id, insert.id, hex_key.id)
        assert stock.stock_in(s, ids[0], 3, "user", consume_parts=True) == []
    with db.begin() as s:
        assert s.get(Article, ids[0]).stock == 3
        assert s.get(Part, ids[1]).stock == 44
        assert s.get(Part, ids[2]).stock == 7
        consumed = journal(s, "part_consumed")
        assert len(consumed) == 2
        assert {r.details["cause"] for r in consumed} == {"stock_in"}


def test_stock_in_short_parts_clamped(db):  # Spec 8.3 rule
    with db.begin() as s:
        a = make_article(s)
        insert, hex_key = with_parts(s, a, hex_stock=1)
        ids = (a.id, hex_key.id)
        result = stock.stock_in(s, ids[0], 3, "user", consume_parts=True)
        assert result == [PartShortage(ids[1], needed=3, had=1)]
    with db.begin() as s:
        assert s.get(Article, ids[0]).stock == 3
        assert s.get(Part, ids[1]).stock == 0


def test_inventory_sets_count_and_books_difference(db):  # Spec 6.2, 6.5
    with db.begin() as s:
        a = make_article(s, stock=5).id
        assert stock.count_article(s, a, "stock", 3, "user") == -2
    with db.begin() as s:
        assert s.get(Article, a).stock == 3
        row = journal(s)[0]
        assert row.kind == "inventory" and row.qty == -2
        assert row.details["counted"] == 3 and row.details["field"] == "stock"
        assert stock.count_article(s, a, "stock", 3, "user") == 0
    with db.begin() as s:
        assert journal(s)[-1].qty == 0


def test_inventory_awaiting_assembly(db):
    with db.begin() as s:
        a = make_article(s, awaiting_assembly=2).id
        assert stock.count_article(s, a, "awaiting_assembly", 0, "user") == -2
    with db.begin() as s:
        assert s.get(Article, a).awaiting_assembly == 0


@pytest.mark.parametrize("field", ["reserved", "provisional"])
def test_inventory_rejects_other_fields(db, field):
    with db.begin() as s:
        a = make_article(s).id
    with pytest.raises(ValidationError):
        with db.begin() as s:
            stock.count_article(s, a, field, 1, "user")


@pytest.mark.parametrize("n", [0, -1, 1_000_001])
def test_invalid_qty_rejected(db, n):
    with db.begin() as s:
        a = make_article(s, stock=5, reserved=5, provisional=5).id
        p = make_part(s).id
    calls = [
        lambda s: stock.reserve_for_order(s, 1, {a: n}, "user"),
        lambda s: stock.sell_unreserved(s, a, n, "user"),
        lambda s: stock.stock_in(s, a, n, "user", consume_parts=False),
        lambda s: stock.receive_part(s, p, n, "user"),
        lambda s: stock.inspect_scrap(s, a, n, "user"),
    ]
    for call in calls:
        with pytest.raises(ValidationError):
            with db.begin() as s:
                call(s)
    with db.begin() as s:
        assert count_journal(s) == 0


def test_invalid_counted_rejected(db):
    with db.begin() as s:
        a = make_article(s, stock=5).id
        p = make_part(s).id
    with pytest.raises(ValidationError):
        with db.begin() as s:
            stock.count_article(s, a, "stock", -1, "user")
    with pytest.raises(ValidationError):
        with db.begin() as s:
            stock.count_part(s, p, -1, "user")
    with pytest.raises(ValidationError):
        with db.begin() as s:
            stock.count_article(s, a, "stock", 1_000_001, "user")
    with db.begin() as s:
        assert count_journal(s) == 0 and s.get(Article, a).stock == 5


def test_unknown_article_and_part(db):
    with pytest.raises(NotFoundError) as info:
        with db.begin() as s:
            stock.sell_unreserved(s, 999, 1, "user")
    assert info.value.key == "articles.not_found"
    with pytest.raises(NotFoundError) as info:
        with db.begin() as s:
            stock.receive_part(s, 999, 1, "user")
    assert info.value.key == "parts.not_found"


def test_part_receipt_and_count(db):
    with db.begin() as s:
        p = make_part(s, pack_size=50).id
        stock.receive_part(s, p, 50, "user")
    with db.begin() as s:
        assert s.get(Part, p).stock == 50
        assert stock.count_part(s, p, 48, "user") == -2
    with db.begin() as s:
        assert s.get(Part, p).stock == 48
        rows = journal(s)
        assert [r.kind for r in rows] == ["part_receipt", "inventory"]
        assert rows[0].part_id == p and rows[1].qty == -2


def test_print_finished_provisional(db):
    with db.begin() as s:
        a = make_article(s).id
        stock.add_provisional(s, a, 1, "auto")
    with db.begin() as s:
        assert s.get(Article, a).provisional == 1
        row = journal(s)[0]
        assert row.kind == "print_finished" and row.actor == "auto"


def test_inspect_good_without_assembly_consumes_parts(db):
    with db.begin() as s:
        a = make_article(s, provisional=3)
        insert, hex_key = with_parts(s, a)
        ids = (a.id, insert.id, hex_key.id)
        assert stock.inspect_good(s, ids[0], 3, "user") == []
    with db.begin() as s:
        art = s.get(Article, ids[0])
        assert (art.provisional, art.stock) == (0, 3)
        assert s.get(Part, ids[1]).stock == 44 and s.get(Part, ids[2]).stock == 7
        kinds = [r.kind for r in journal(s)]
        assert kinds.count("inspection_good") == 1 and kinds.count("part_consumed") == 2


def test_inspect_good_with_assembly(db):
    with db.begin() as s:
        a = make_article(s, provisional=3, needs_assembly=True)
        insert, _ = with_parts(s, a)
        ids = (a.id, insert.id)
        stock.inspect_good(s, ids[0], 3, "user")
    with db.begin() as s:
        art = s.get(Article, ids[0])
        assert (art.awaiting_assembly, art.stock, art.provisional) == (3, 0, 0)
        assert s.get(Part, ids[1]).stock == 50


def test_inspect_scrap(db):
    with db.begin() as s:
        a = make_article(s, provisional=3).id
        stock.inspect_scrap(s, a, 1, "user")
    with db.begin() as s:
        assert s.get(Article, a).provisional == 2
        assert journal(s)[0].kind == "inspection_scrap"


def test_inspect_scrap_over_provisional_rejected(db):
    with db.begin() as s:
        a = make_article(s, provisional=3).id
    with pytest.raises(StockLimitError) as info:
        with db.begin() as s:
            stock.inspect_scrap(s, a, 5, "user")
    assert info.value.key == "stock.limit.provisional"
    with db.begin() as s:
        assert s.get(Article, a).provisional == 3
        assert count_journal(s) == 0


def test_assemble_consumes_parts(db):  # Spec 8.3
    with db.begin() as s:
        a = make_article(s, awaiting_assembly=3, needs_assembly=True)
        insert, _ = with_parts(s, a)
        ids = (a.id, insert.id)
        stock.assemble(s, ids[0], 3, "user")
    with db.begin() as s:
        art = s.get(Article, ids[0])
        assert (art.awaiting_assembly, art.stock) == (0, 3)
        assert s.get(Part, ids[1]).stock == 44


def test_assemble_short_parts_clamped(db):  # Spec 8.3
    with db.begin() as s:
        a = make_article(s, awaiting_assembly=3, needs_assembly=True)
        _, hex_key = with_parts(s, a, hex_stock=1)
        ids = (a.id, hex_key.id)
        result = stock.assemble(s, ids[0], 3, "user")
        assert result == [PartShortage(ids[1], needed=3, had=1)]
    with db.begin() as s:
        assert s.get(Part, ids[1]).stock == 0
        assert s.get(Article, ids[0]).stock == 3
        row = [r for r in journal(s, "part_consumed") if r.part_id == ids[1]][0]
        assert row.details["shortage"] == 2 and row.details["cause"] == "assembly"


def test_inspect_good_short_parts_clamped(db):  # decision 5
    with db.begin() as s:
        a = make_article(s, provisional=3)
        _, hex_key = with_parts(s, a, hex_stock=1)
        ids = (a.id, hex_key.id)
        result = stock.inspect_good(s, ids[0], 3, "user")
        assert result == [PartShortage(ids[1], needed=3, had=1)]
    with db.begin() as s:
        assert s.get(Part, ids[1]).stock == 0 and s.get(Article, ids[0]).stock == 3


def test_assemble_over_awaiting_rejected(db):
    with db.begin() as s:
        a = make_article(s, awaiting_assembly=1).id
    with pytest.raises(StockLimitError) as info:
        with db.begin() as s:
            stock.assemble(s, a, 2, "user")
    assert info.value.key == "stock.limit.awaiting_assembly"


def test_write_journal_without_stock_change(db, monkeypatch):
    monkeypatch.setattr(stock, "KINDS", stock.KINDS | {"queue_entry_created"})
    with db.begin() as s:
        a = make_article(s, stock=2).id
        entry = stock.write_journal(s, kind="queue_entry_created", actor="auto", article_id=a)
        assert entry.kind == "queue_entry_created"
    with db.begin() as s:
        assert s.get(Article, a).stock == 2 and count_journal(s) == 1
        with pytest.raises(ValueError):
            stock.write_journal(s, kind="no_such_kind", actor="auto")


def test_journal_note_trimmed_and_limited(db):
    with db.begin() as s:
        a = make_article(s, stock=2).id
        stock.sell_unreserved(s, a, 1, "user", note="  by hand  ")
        stock.sell_unreserved(s, a, 1, "user", note="   ")
    with db.begin() as s:
        assert [r.note for r in journal(s)] == ["by hand", None]
    with pytest.raises(ValidationError):
        with db.begin() as s:
            stock.write_journal(s, kind="sale", actor="user", note="x" * 501)


def test_journal_time_is_utc_naive(db):
    before = utcnow()
    with db.begin() as s:
        a = make_article(s, stock=2).id
        stock.sell_unreserved(s, a, 1, "user")
    with db.begin() as s:
        at = journal(s)[0].at
        assert at.tzinfo is None and abs((at - before).total_seconds()) < 60


def test_journal_page_newest_first_and_filtered(db):
    with db.begin() as s:
        a = make_article(s, name="Template A", stock=5).id
        b = make_article(s, name="Template B", stock=5).id
        p = make_part(s).id
        stock.sell_unreserved(s, a, 1, "user")
        stock.sell_unreserved(s, b, 1, "user")
        stock.receive_part(s, p, 5, "user")
        stock.sell_unreserved(s, a, 2, "user")
    with db.begin() as s:
        allrows = stock.journal_page(s)
        assert [r.qty for r in allrows] == [2, 5, 1, 1]
        assert [r.qty for r in stock.journal_page(s, article_id=a)] == [2, 1]
        assert [r.kind for r in stock.journal_page(s, part_id=p)] == ["part_receipt"]
        assert len(stock.journal_page(s, limit=2)) == 2
        assert [r.qty for r in stock.journal_page(s, limit=2, offset=2)] == [1, 1]
