"""Turns journal entries into rows a template can show (names instead of ids)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy.models import Article, JournalEntry, Order, Part
from reprintbuddy.web.formatting import to_local

CHANGE_LABELS = {
    "stock": "articles.col.stock",
    "reserved": "articles.col.reserved",
    "provisional": "articles.col.provisional",
    "awaiting_assembly": "articles.col.awaiting_assembly",
}


def _into(entry: JournalEntry) -> int | None:
    """The article a part of a row "built in" went into, if the row says."""
    into = (entry.details or {}).get("into") if entry.kind == "built_in" else None
    return into if isinstance(into, int) and not isinstance(into, bool) else None


def journal_rows(db: Session, entries: Sequence[JournalEntry]) -> list[dict[str, Any]]:
    article_ids = {e.article_id for e in entries if e.article_id is not None}
    # p361: a part built in names the article it went into (the variant row on stock, the article made
    # of parts on order) with the variant number as booked
    article_ids |= {into for e in entries if (into := _into(e)) is not None}
    part_ids = {e.part_id for e in entries if e.part_id is not None}
    order_ids = {e.order_id for e in entries if e.order_id is not None}
    articles = dict(db.execute(select(Article.id, Article.name).where(Article.id.in_(article_ids))).all()) if article_ids else {}
    parts = dict(db.execute(select(Part.id, Part.name).where(Part.id.in_(part_ids))).all()) if part_ids else {}
    orders = dict(db.execute(select(Order.id, Order.reference).where(Order.id.in_(order_ids))).all()) if order_ids else {}
    rows = []
    for entry in entries:
        changes = []
        for field, pair in ((entry.details or {}).get("changes") or {}).items():
            if field in CHANGE_LABELS and isinstance(pair, list) and len(pair) == 2:
                changes.append({"label": CHANGE_LABELS[field], "before": pair[0], "after": pair[1]})
        # an inventory dated back: the value of the booking day, before and after the count (p210)
        then = (entry.details or {}).get("then") if entry.kind == "inventory" else None
        rows.append(
            {
                "at": entry.booked_at,  # when it counts (p172)
                # dated back: the row shows the day it counts and, below, when it was saved
                "day": to_local(entry.booked_at).date() if entry.booked_at != entry.at else None,
                "saved_at": entry.at if entry.booked_at != entry.at else None,
                "actor_key": f"journal.actor.{entry.actor}",
                "kind_key": f"journal.kind.{entry.kind}",
                "qty": entry.qty,
                "note": entry.note,
                "article": (entry.article_id, articles.get(entry.article_id)) if entry.article_id else None,
                "part": (entry.part_id, parts.get(entry.part_id)) if entry.part_id else None,
                "order": (entry.order_id, orders.get(entry.order_id)) if entry.order_id else None,
                "changes": changes,
                "then": then if isinstance(then, list) and len(then) == 2 else None,
                "into": (
                    (into, articles.get(into), (entry.details or {}).get("variant_sku"))
                    if (into := _into(entry)) is not None else None
                ),
            }
        )
    return rows
