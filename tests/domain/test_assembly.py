import pytest
from sqlalchemy import func, select

from factories import make_article, make_part
from reprintbuddy.domain import inspection
from reprintbuddy.domain.errors import StockLimitError
from reprintbuddy.domain.inspection import AssemblyRow, PartNeed
from reprintbuddy.domain.stock import PartShortage
from reprintbuddy.models import Article, ArticlePart, JournalEntry, Part, utcnow


def awaiting_article(s, awaiting=2, insert_stock=50, hex_stock=10, **fields):
    article = make_article(s, awaiting_assembly=awaiting, needs_assembly=True, **fields)
    insert = make_part(s, name="Insert M4", stock=insert_stock)
    hex_key = make_part(s, name="Hex key 2", stock=hex_stock)
    s.add(ArticlePart(article_id=article.id, part_id=insert.id, qty_per_unit=2))
    s.add(ArticlePart(article_id=article.id, part_id=hex_key.id, qty_per_unit=1))
    s.flush()
    return article, insert, hex_key


def test_list_awaiting(db):
    with db.begin() as s:
        article, insert, hex_key = awaiting_article(s)
        make_article(s, name="Template B", awaiting_assembly=0, needs_assembly=True)
        make_article(s, name="Template C", awaiting_assembly=3, archived_at=utcnow())
    with db.begin() as s:
        rows = inspection.list_awaiting_assembly(s)
    assert rows == [
        AssemblyRow(
            article_id=article.id,
            name="Template A",
            awaiting=2,
            parts=[
                PartNeed(part_id=hex_key.id, name="Hex key 2", qty_per_unit=1, stock=10),
                PartNeed(part_id=insert.id, name="Insert M4", qty_per_unit=2, stock=50),
            ],
        )
    ]


def test_assemble_books(db):
    with db.begin() as s:
        article, insert, hex_key = awaiting_article(s)
        ids = (article.id, insert.id, hex_key.id)
    with db.begin() as s:
        assert inspection.assemble(s, ids[0], 2, "user") == []
    with db.begin() as s:
        art = s.get(Article, ids[0])
        assert (art.awaiting_assembly, art.stock) == (0, 2)
        assert s.get(Part, ids[1]).stock == 46
        assert s.get(Part, ids[2]).stock == 8


def test_assemble_short_parts_warns(db):
    with db.begin() as s:
        article, insert, hex_key = awaiting_article(s, hex_stock=1)
        ids = (article.id, insert.id, hex_key.id)
    with db.begin() as s:
        shortages = inspection.assemble(s, ids[0], 2, "user")
    assert shortages == [PartShortage(ids[2], 2, 1)]
    with db.begin() as s:
        assert s.get(Part, ids[2]).stock == 0
        assert s.get(Article, ids[0]).stock == 2


def test_assemble_over_awaiting_rejected(db):
    with db.begin() as s:
        article, insert, hex_key = awaiting_article(s)
        ids = (article.id, insert.id, hex_key.id)
    with pytest.raises(StockLimitError) as info:
        with db.begin() as s:
            inspection.assemble(s, ids[0], 3, "user")
    assert info.value.key == "stock.limit.awaiting_assembly"
    with db.begin() as s:
        art = s.get(Article, ids[0])
        assert (art.awaiting_assembly, art.stock) == (2, 0)
        assert (s.get(Part, ids[1]).stock, s.get(Part, ids[2]).stock) == (50, 10)
        assert s.scalar(select(func.count()).select_from(JournalEntry)) == 0


def test_assemble_after_flag_removed(db):
    with db.begin() as s:
        article, *_ = awaiting_article(s, awaiting=1)
        article.needs_assembly = False
        aid = article.id
    with db.begin() as s:
        assert [r.article_id for r in inspection.list_awaiting_assembly(s)] == [aid]
        inspection.assemble(s, aid, 1, "user")
    with db.begin() as s:
        art = s.get(Article, aid)
        assert (art.awaiting_assembly, art.stock) == (0, 1)


def test_list_awaiting_of_one_article_and_thumbnail_flag(db):
    with db.begin() as s:
        a = make_article(s, name="Template A", awaiting_assembly=2, thumbnail=b"png")
        b = make_article(s, name="Template B", awaiting_assembly=1)
        ids = (a.id, b.id)
    with db.begin() as s:
        rows = inspection.list_awaiting_assembly(s, article_id=ids[0])
        assert [(r.article_id, r.has_thumbnail) for r in rows] == [(ids[0], True)]
        assert [r.article_id for r in inspection.list_awaiting_assembly(s)] == list(ids)
        assert inspection.list_awaiting_assembly(s, article_id=999) == []
