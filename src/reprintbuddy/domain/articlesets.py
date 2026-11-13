"""Which articles a query sees (0.4.0, p341; Spec 5.6).

Since 0.4.0 the table `articles` holds three kinds of rows: print articles (their own plate), articles
made of parts (a bill of materials) and the variant rows of the latter. A query that forgets the kind
lets an article made of parts into rule 7.1, the print check or the filament pages, or shows a variant
row as an article of its own. So every query over many articles starts from one of these named sets,
and only these decide what "print article", "stock holder" or "orderable" means.
Tests: tests/domain/test_article_sets_guard.py.

Each set comes twice: as a condition (`where_*`, for queries over a few columns or with joins) and as
a select of whole articles that the caller narrows further. Archived rows are left out unless asked
for; a variant row is archived together with its article made of parts (Spec 5.5), so the archive
filter needs no join on the parent.
"""

from __future__ import annotations

from sqlalchemy import ColumnElement, Select, and_, exists, or_, select
from sqlalchemy.orm import aliased

from reprintbuddy.models import Article, ArticleOption

PRINT = "print"
ASSEMBLED = "assembled"
VARIANT = "variant"


def _archive(archived: bool) -> ColumnElement[bool]:
    return Article.archived_at.is_not(None) if archived else Article.archived_at.is_(None)


def where_print(*, archived: bool = False) -> ColumnElement[bool]:
    """Print articles: printed, checked, reprinted, with a plate and filament (Spec 5.1)."""
    return and_(Article.kind == PRINT, _archive(archived))


def where_catalog(*, archived: bool = False) -> ColumnElement[bool]:
    """What the article list, the archive and the search show: print articles and articles made of
    parts, never a variant row (it is shown under its article)."""
    return and_(Article.kind.in_((PRINT, ASSEMBLED)), _archive(archived))


def where_assembled(*, archived: bool = False) -> ColumnElement[bool]:
    return and_(Article.kind == ASSEMBLED, _archive(archived))


def _has_options() -> ColumnElement[bool]:
    return exists(select(ArticleOption.id).where(ArticleOption.article_id == Article.id))


def where_stock_holder() -> ColumnElement[bool]:
    """Active rows that hold finished stock of an article made of parts (Spec 5.6, 6.1): one built on
    stock without choices, or an active variant row of one built on stock."""
    parent = aliased(Article)
    on_stock_parent = exists(
        select(parent.id).where(
            parent.id == Article.parent_id, parent.kind == ASSEMBLED, parent.assembly_mode == "stock"
        )
    )
    return and_(
        Article.archived_at.is_(None),
        or_(
            and_(Article.kind == ASSEMBLED, Article.assembly_mode == "stock", ~_has_options()),
            and_(Article.kind == VARIANT, on_stock_parent),
        ),
    )


def where_orderable() -> ColumnElement[bool]:
    """Articles an order line or a sale may name directly (Spec 6.7, 5.2): active print articles sold
    alone, and stock holders. An article made of parts built on order is ordered by its variant
    (orders.AssembledItem), its parts then reserved one by one."""
    return or_(and_(where_print(), Article.sold_alone.is_(True)), where_stock_holder())


def print_articles(*, archived: bool = False) -> Select[tuple[Article]]:
    return select(Article).where(where_print(archived=archived))


def catalog_articles(*, archived: bool = False) -> Select[tuple[Article]]:
    return select(Article).where(where_catalog(archived=archived))


def assembled_articles(*, archived: bool = False) -> Select[tuple[Article]]:
    return select(Article).where(where_assembled(archived=archived))


def stock_holders() -> Select[tuple[Article]]:
    return select(Article).where(where_stock_holder())


def variant_rows(parent_id: int, *, archived: bool = False) -> Select[tuple[Article]]:
    """The variant rows of one article made of parts, active or archived."""
    return select(Article).where(
        Article.kind == VARIANT, Article.parent_id == parent_id, _archive(archived)
    ).order_by(Article.id)
