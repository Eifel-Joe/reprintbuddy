"""Planned demand for filament and purchased parts (Spec 9.2). Reads only, never writes."""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy.domain import rules, workqueue
from reprintbuddy.models import Article, ArticleFilament, ArticlePart


def missing_to_target(article: Article, coverage_total: int) -> int:
    """Units still missing up to the target quantity (never negative)."""
    return max(0, rules.target_quantity(article.min_stock, article.buffer) - coverage_total)


def _missing_by_article(session: Session) -> tuple[dict[int, Article], dict[int, int]]:
    articles = {
        a.id: a for a in session.scalars(select(Article).where(Article.archived_at.is_(None)))
    }
    coverage = workqueue.coverage_all(session)
    return articles, {i: missing_to_target(a, coverage[i].total) for i, a in articles.items()}


def filament_demand(session: Session) -> dict[int, float]:
    """Grams per filament sort: grams x (queued + in-progress prints + prints up to the target).

    Counts every non-archived article (also with automation off); slots without a sort are
    skipped; sorts without demand may be missing from the result.
    """
    articles, missing = _missing_by_article(session)
    queued = workqueue.queued_prints(session)
    running = workqueue.in_progress_prints(session)
    demand: dict[int, float] = defaultdict(float)
    for slot in session.scalars(
        select(ArticleFilament).where(ArticleFilament.spool_filament_id.is_not(None))
    ):
        article = articles.get(slot.article_id)
        if article is None:
            continue
        prints = (
            queued.get(article.id, 0)
            + running.get(article.id, 0)
            + rules.prints_for_units(missing[article.id], article.units_per_print)
        )
        demand[slot.spool_filament_id] += slot.grams * prints
    return dict(demand)


def part_demand(session: Session) -> dict[int, int]:
    """Units per purchased part: qty_per_unit x (awaiting assembly + queued units + units in
    progress + units up to the target). "Vorläufig" is deliberately not a summand (Spec 9.2)."""
    articles, missing = _missing_by_article(session)
    queued = workqueue.queued_prints(session)
    units = workqueue.in_progress_units(session)
    demand: dict[int, int] = defaultdict(int)
    for link in session.scalars(select(ArticlePart)):
        article = articles.get(link.article_id)
        if article is None:
            continue
        per_unit = (
            article.awaiting_assembly
            + queued.get(article.id, 0) * article.units_per_print
            + units.get(article.id, 0)
            + missing[article.id]
        )
        demand[link.part_id] += link.qty_per_unit * per_unit
    return dict(demand)
