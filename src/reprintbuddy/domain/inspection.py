"""Inspection (Spec 8.2) and assembly (Spec 8.3).

Functions never commit: the caller opens the transaction. All counter changes go
through `reprintbuddy.domain.stock`, the only writer.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy.domain import stock
from reprintbuddy.domain.stock import Actor, PartShortage
from reprintbuddy.models import Article, ArticlePart, Part

# ---- assembly (Spec 8.3) -------------------------------------------------------------


@dataclass(frozen=True)
class PartNeed:
    part_id: int
    name: str
    qty_per_unit: int
    stock: int


@dataclass(frozen=True)
class AssemblyRow:
    article_id: int
    name: str
    awaiting: int
    parts: list[PartNeed]


def list_awaiting_assembly(session: Session) -> list[AssemblyRow]:
    """Active articles with units waiting for assembly, by name, with their parts.

    Not filtered by `needs_assembly`: units already waiting must stay bookable
    after the flag is switched off (decision 4).
    """
    articles = session.scalars(
        select(Article)
        .where(Article.archived_at.is_(None), Article.awaiting_assembly > 0)
        .order_by(Article.name, Article.id)
    ).all()
    rows: list[AssemblyRow] = []
    for article in articles:
        needs = session.execute(
            select(Part, ArticlePart.qty_per_unit)
            .join(ArticlePart, ArticlePart.part_id == Part.id)
            .where(ArticlePart.article_id == article.id)
            .order_by(Part.name, Part.id)
        ).all()
        rows.append(
            AssemblyRow(
                article_id=article.id,
                name=article.name,
                awaiting=article.awaiting_assembly,
                parts=[PartNeed(p.id, p.name, qty, p.stock) for p, qty in needs],
            )
        )
    return rows


def assemble(
    session: Session, article_id: int, n: int, actor: Actor, note: str | None = None
) -> list[PartShortage]:
    """Books an assembly through the stock module; it checks the limits (decision 4)."""
    return stock.assemble(session, article_id, n, actor, note)
