"""Activity log, live topics and push of articles made of parts (Spec 10.5-10.7, 0.4.0 p341)."""

from __future__ import annotations

from sqlalchemy import func, select

from factories import make_article
from reprintbuddy.changes import install_change_feed
from reprintbuddy.domain import assembled, catalog
from reprintbuddy.domain.assembled import AssembledInput, OptionInput
from reprintbuddy.domain.catalog import ArticleInput, BaseInput
from reprintbuddy.models import ActivityEntry
from reprintbuddy.notify import events


def since(db, last: int) -> list[tuple[str, dict | None]]:
    with db() as s:
        rows = s.scalars(select(ActivityEntry).where(ActivityEntry.id > last).order_by(ActivityEntry.id))
        return [(row.kind, row.details) for row in rows]


def mark(db) -> int:
    with db() as s:
        return s.scalar(select(func.max(ActivityEntry.id))) or 0


def hose_input(ids, *, option_ids=(None, None), targets=None):
    return AssembledInput(
        name="Hose", sku="EX-H1", mode="stock", base=[BaseInput(12, ids["Segment"])],
        fields=[[OptionInput(option_ids[0], "-L", [BaseInput(1, ids["Side part L"])]),
                 OptionInput(option_ids[1], "-R", [BaseInput(1, ids["Side part R"])])]],
        parts={}, variant_targets=targets or {},
    )


def parts(db):
    with db.begin() as s:
        return {n: make_article(s, name=n).id for n in ("Segment", "Side part L", "Side part R")}


def test_creating_assembled_logs_one_entry_not_one_per_variant(db):
    ids = parts(db)
    last = mark(db)
    with db.begin() as s:
        hose = assembled.create_assembled(s, hose_input(ids)).id
    assert since(db, last) == [("article_created", {"id": hose, "name": "Hose"})]


def test_changing_a_variant_target_logs_variant_targets(db):
    ids = parts(db)
    with db.begin() as s:
        hose = assembled.create_assembled(s, hose_input(ids))
        hose_id, option_ids = hose.id, [o.id for o in assembled.options(s, hose)[0]]
        first = assembled.variants(s, hose)[0].id
    last = mark(db)
    with db.begin() as s:
        assembled.update_assembled(s, hose_id, hose_input(ids, option_ids=option_ids, targets={first: (2, 1)}))
    assert since(db, last) == [("article_changed", {"id": hose_id, "name": "Hose", "fields": ["variant_targets"]})]


def test_saving_the_same_parts_list_again_is_no_change(db):
    ids = parts(db)
    with db.begin() as s:
        hose = assembled.create_assembled(s, hose_input(ids))
        hose_id, option_ids = hose.id, [o.id for o in assembled.options(s, hose)[0]]
    last = mark(db)
    with db.begin() as s:
        assembled.update_assembled(s, hose_id, hose_input(ids, option_ids=option_ids))
    assert since(db, last) == []


def test_sold_alone_change_is_logged(db):
    with db.begin() as s:
        article = make_article(s, name="Side part L")
        article_id = article.id
    last = mark(db)
    with db.begin() as s:
        catalog.update_article(s, article_id, ArticleInput(name="Side part L", sold_alone=False))
    assert since(db, last) == [("article_changed", {"id": article_id, "name": "Side part L", "fields": ["sold_alone"]})]


def test_variant_change_announces_its_parent(db):
    ids = parts(db)
    with db.begin() as s:
        hose = assembled.create_assembled(s, hose_input(ids))
        hose_id, variant = hose.id, assembled.variants(s, hose)[0].id
    sent: list[frozenset[str]] = []
    install_change_feed(db, sent.append)
    with db.begin() as s:
        s.get(type(hose), variant).stock = 2
    assert f"article:{hose_id}" in sent[0] and f"article:{variant}" in sent[0]


def test_below_minimum_push_for_variant_and_for_part_with_demand(db):  # Teil C 8
    ids = parts(db)
    with db.begin() as s:
        hose = assembled.create_assembled(s, hose_input(ids))
        variant = assembled.variants(s, hose)[0]
        variant.min_stock = 1
        s.flush()
        red = events.red_articles(s)
        assert red[variant.id] == "below_min"  # parts not covered: red, not yellow
        assert red[ids["Segment"]] == "below_min"  # minimum 0 + demand 12 > coverage 0
        assert assembled.variants(s, hose)[1].id not in red
