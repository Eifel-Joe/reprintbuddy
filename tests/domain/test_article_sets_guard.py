"""Which articles a query sees (0.4.0, p341, condition 1 of the plan's approval).

Since 0.4.0 the table `articles` holds print articles, articles made of parts and the variant rows of
the latter. Every query that means "print articles" (rule 7.1, the print check, filament, the queue,
reorder) must leave the others out, lists and the overview never show a variant row as an article of
its own, and nothing shows an archived row. The articles made of parts below carry values that would
make an unfiltered query pick them (automation on, paused, below the minimum), so a query that
forgets the kind fails here, not in a user's database.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from factories import make_article, make_assembled
from reprintbuddy import settings
from reprintbuddy.domain import articlesets, catalog, engine, filament, inspection, reorder, workqueue
from reprintbuddy.domain.tracking import check_articles
from reprintbuddy.models import Article
from reprintbuddy.notify import events
from reprintbuddy.ops.export import export_data

ARCHIVED = datetime(2026, 11, 10, 8, 0)


@pytest.fixture
def world(db):
    """Print articles; a box (on stock, no choices); a hose (choices, on order, variant rows); an
    archived lamp with variant rows. Returns the ids by group."""
    with db() as s:
        settings.set_setting(s, "automation", True)  # commits on its own
    with db.begin() as s:
        prints = {
            name: make_article(s, name=name, auto_enabled=True, min_stock=1, target_model="A1").id
            for name in ("Panel A", "Lid", "Segment", "Side part L", "Socket L32", "Socket L40", "Clip")
        }
        box = make_assembled(
            s, name="Storage box", sku="EX-B1", base=[(prints["Panel A"], 1), (prints["Lid"], 1)],
            min_stock=3, auto_enabled=True, awaiting_assembly=1,
        )
        hose = make_assembled(
            s, name="Hose", sku="EX-H1", mode="order", base=[(prints["Segment"], 12)],
            fields=[[("-L32", [(prints["Side part L"], 1), (prints["Socket L32"], 1)]),
                     ("-L40", [(prints["Side part L"], 1), (prints["Socket L40"], 1)])]],
            paused=True, pause_reason="file_missing",
        )
        lamp = make_assembled(
            s, name="Lamp", sku="EX-L1", base=[(prints["Clip"], 2)],
            fields=[[("-A", [(prints["Lid"], 1)]), ("-B", [(prints["Panel A"], 1)])]],
            archived_at=ARCHIVED,
        )
        for row in s.scalars(select(Article).where(Article.kind == "variant")):
            # values an unfiltered query would pick up
            row.auto_enabled, row.min_stock, row.paused, row.pause_reason = True, 2, True, "no_target"
            if row.parent_id == lamp.id:
                row.archived_at = ARCHIVED
        hose_variants = set(s.scalars(select(Article.id).where(Article.parent_id == hose.id)))
        lamp_variants = set(s.scalars(select(Article.id).where(Article.parent_id == lamp.id)))
    return {
        "print": set(prints.values()), "box": box.id, "hose": hose.id, "lamp": lamp.id,
        "hose_variants": hose_variants, "lamp_variants": lamp_variants,
    }


def not_print(world) -> set[int]:
    return {world["box"], world["hose"], world["lamp"]} | world["hose_variants"] | world["lamp_variants"]


def archived(world) -> set[int]:
    return {world["lamp"]} | world["lamp_variants"]


def test_the_helpers_give_the_named_sets(db, world):
    with db() as s:
        ids = lambda query: set(s.scalars(query.with_only_columns(Article.id)))  # noqa: E731
        assert ids(articlesets.print_articles()) == world["print"]
        assert ids(articlesets.catalog_articles()) == world["print"] | {world["box"], world["hose"]}
        assert ids(articlesets.catalog_articles(archived=True)) == {world["lamp"]}
        assert ids(articlesets.assembled_articles()) == {world["box"], world["hose"]}
        assert ids(articlesets.stock_holders()) == {world["box"]}
        assert ids(articlesets.orderable_articles()) == world["print"] | {world["box"]}
        assert ids(articlesets.variant_rows(world["hose"])) == world["hose_variants"]
        assert ids(articlesets.variant_rows(world["lamp"], archived=True)) == world["lamp_variants"]


async def test_only_print_articles_where_only_print_articles_belong(db, world, fake_bambuddy):
    others = not_print(world)
    with db.begin() as s:
        queued = workqueue.apply_rule(s, actor="auto")
        assert {e.article_id for e in queued} == world["print"]  # each below its minimum
        assert set(workqueue.coverage_all(s)) == world["print"]
        warnings = engine.collect_warnings(s, engine.EngineStatus(configured=True, reachable=True))
        paused = {w.params["article_id"] for w in warnings if w.key == "warning.article_paused"}
        assert paused.isdisjoint(others)
        missing_articles, _missing = reorder._missing_by_article(s)
        assert set(missing_articles) == world["print"]
        assert {a for a, _name, _slot in filament.unmapped_slots(s)}.isdisjoint(others)
        assert {r.article_id for r in inspection.list_awaiting_assembly(s)}.isdisjoint(others)
        assert set(events.red_articles(s)).isdisjoint(others - {world["box"]})
        assert {int(ref) for ref in events._paused_refs(s)}.isdisjoint(others)
    result = await check_articles(fake_bambuddy, db)  # must not stumble over rows without a plate
    assert result.error is None
    with db() as s:
        for article_id in others:
            row = s.get(Article, article_id)
            assert row.pause_reason in (None, "file_missing", "no_target")  # untouched by the check


def test_lists_show_no_variant_row_and_nothing_archived(db, world):
    with db() as s:
        listed = {row.id for row in catalog.list_articles(s)}
        assert listed.isdisjoint(world["hose_variants"] | archived(world))
        # Until 0.4.0 task 5 the list holds print articles only; task 5 adds the box and the hose.
        assert world["print"] <= listed


def test_export_keeps_every_row(db, world):
    engine_ = db.kw["bind"]
    with engine_.connect() as conn:
        exported = {r["id"] for r in export_data(conn, now=datetime(2026, 11, 11, tzinfo=UTC))["tables"]["articles"]}
    assert exported == world["print"] | not_print(world)
