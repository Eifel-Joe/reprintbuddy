"""The journal: every booking, newest first (Spec 6.3)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from reprintbuddy.domain import assembled, stock
from reprintbuddy.models import Article
from reprintbuddy.web.deps import get_db, render
from reprintbuddy.web.forms import bounded_int
from reprintbuddy.web.journal_view import journal_rows

router = APIRouter()

PAGE_SIZE = 100


def _opt_int(raw: str | None, *, low: int = 1) -> int | None:
    """A positive whole number from the query string; anything else counts as absent."""
    value = bounded_int(raw.strip()) if raw is not None else None
    return value if value is not None and low <= value < 2**31 else None


@router.get("/journal")
async def journal_page(
    request: Request,
    article: str | None = None,
    part: str | None = None,
    page: str | None = None,
    db: Session = Depends(get_db),
):
    article_id, part_id = _opt_int(article), _opt_int(part)
    page_no = _opt_int(page) or 1
    # An article made of parts takes the rows of its variants too, as its page does (p364).
    chosen = db.get(Article, article_id) if article_id is not None else None
    article_ids = assembled.journal_ids(db, chosen) if chosen is not None and chosen.kind == "assembled" else None
    entries = stock.journal_page(
        db, article_id=None if article_ids else article_id, article_ids=article_ids, part_id=part_id,
        limit=PAGE_SIZE + 1, offset=(page_no - 1) * PAGE_SIZE,
    )
    query = "".join(
        f"&{name}={value}" for name, value in (("article", article_id), ("part", part_id)) if value
    )
    return render(
        request,
        "journal.html",
        active_nav="articles",
        rows=journal_rows(db, entries[:PAGE_SIZE]),
        has_next=len(entries) > PAGE_SIZE,
        has_prev=page_no > 1,
        next_url=f"/journal?page={page_no + 1}{query}",
        prev_url=f"/journal?page={page_no - 1}{query}",
    )
