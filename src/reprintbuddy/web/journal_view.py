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


def journal_rows(db: Session, entries: Sequence[JournalEntry]) -> list[dict[str, Any]]:
    article_ids = {e.article_id for e in entries if e.article_id is not None}
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
            }
        )
    return rows
