"""Rows, availability and status of articles made of parts (Spec 5.6, 10.1; 0.4.0 p341)."""

from factories import make_article, make_assembled, make_entry
from reprintbuddy.domain import assembled, catalog
from reprintbuddy.domain.status import assembled_status


def test_assembled_status():  # Spec 10.1, "knapp" = parts covered, only assembly is missing
    assert assembled_status(available=-1, min_stock=0, parts_covered=True) == "overbooked"
    assert assembled_status(available=1, min_stock=2, parts_covered=False) == "below_min"
    assert assembled_status(available=1, min_stock=2, parts_covered=True) == "low"
    assert assembled_status(available=2, min_stock=2, parts_covered=False) == "ok"


def hose(s, *, mode="order"):
    """Spec 5.6 example: segment 60, side part L 3, R 1, sockets L32 2, L40 2, R32 0, R40 1."""
    ids = {}
    for name, stock in (("Segment", 60), ("Side part L", 3), ("Side part R", 1), ("Socket L32", 2),
                        ("Socket L40", 2), ("Socket R32", 0), ("Socket R40", 1)):
        ids[name] = make_article(s, name=name, stock=stock).id
    composite = make_assembled(s, name="Hose", sku="EX-H1", mode=mode, base=[(ids["Segment"], 12)], fields=[[
        ("-L32", [(ids["Side part L"], 1), (ids["Socket L32"], 1)]),
        ("-L40", [(ids["Side part L"], 1), (ids["Socket L40"], 1)]),
        ("-R32", [(ids["Side part R"], 1), (ids["Socket R32"], 1)]),
        ("-R40", [(ids["Side part R"], 1), (ids["Socket R40"], 1)]),
    ]])
    return composite, ids


def test_hose_row_shows_variants_and_together(db):  # Spec 5.6 example
    with db.begin() as s:
        composite, _ids = hose(s)
        row = assembled.assembled_row(s, composite.id)
        assert [(h.sku, h.buildable) for h in row.holders] == [
            ("EX-H1-L32", 2), ("EX-H1-L40", 2), ("EX-H1-R32", 0), ("EX-H1-R40", 1),
        ]
        assert row.available == 4 and row.status == "deliverable"
        assert row.mode == "order"


def test_hose_not_deliverable_without_segments(db):
    with db.begin() as s:
        composite, ids = hose(s)
        s.get(type(composite), ids["Segment"]).stock = 11
        s.flush()
        assert assembled.assembled_row(s, composite.id).status == "not_deliverable"


def test_box_row_buildable_from_available_parts(db):
    with db.begin() as s:
        panel = make_article(s, name="Panel A", stock=5, reserved=3)
        lid = make_article(s, name="Lid", stock=9)
        box = make_assembled(s, name="Storage box", sku="EX-B1", base=[(panel.id, 1), (lid.id, 1)],
                             min_stock=2, buffer=1, stock=1)
        row = assembled.assembled_row(s, box.id)
        (holder,) = row.holders
        assert (holder.article_id, holder.stock, holder.available, holder.buildable) == (box.id, 1, 1, 2)
        assert (holder.missing, row.available) == (2, 1)


def test_variant_low_when_parts_covered_else_below_min(db):
    with db.begin() as s:
        panel = make_article(s, name="Panel A", stock=0, units_per_print=1)
        lid = make_article(s, name="Lid", stock=5)
        box = make_assembled(s, name="Storage box", base=[(panel.id, 1), (lid.id, 1)], min_stock=2)
        demand = assembled.assembly_demand(s)
        assert assembled.assembled_row(s, box.id, demand=demand).status == "below_min"  # panel not covered
        make_entry(s, panel.id, 2)  # two panels queued: every part covered, only assembly is missing
        assert assembled.assembled_row(s, box.id, demand=demand).status == "low"


def test_print_article_status_counts_demand(db):  # Spec 10.1 / 7.1
    with db.begin() as s:
        panel = make_article(s, name="Panel A", stock=1)  # minimum 0 of its own
        lid = make_article(s, name="Lid", stock=5)
        make_assembled(s, name="Storage box", base=[(panel.id, 1), (lid.id, 1)], min_stock=2, buffer=1)
        rows = {r.id: r for r in catalog.list_articles(s)}
        assert rows[panel.id].demand == 3
        assert rows[panel.id].status == "below_min"  # coverage 1 < 0 + 3
        assert rows[lid.id].status == "ok"  # coverage 5 >= 3


def test_list_articles_never_lists_variant_rows(db):
    with db.begin() as s:
        composite, ids = hose(s)
        variant_ids = {v.id for v in assembled.variants(s, composite)}
        rows = catalog.list_articles(s)
        listed = {r.id for r in rows}
        assert composite.id in listed and listed.isdisjoint(variant_ids)
        row = next(r for r in rows if r.id == composite.id)
        assert (row.kind, row.available, row.status) == ("assembled", 4, "deliverable")
        assert [h.sku for h in row.assembled.holders] == ["EX-H1-L32", "EX-H1-L40", "EX-H1-R32", "EX-H1-R40"]
        assert next(r for r in rows if r.id == ids["Segment"]).kind == "print"


def test_thumbnail_comes_from_first_part(db):  # Spec 5.6 "Bild"
    with db.begin() as s:
        panel = make_article(s, name="Panel A", thumbnail=b"png")
        lid = make_article(s, name="Lid")
        box = make_assembled(s, name="Storage box", base=[(lid.id, 1), (panel.id, 1)])
        assert assembled.assembled_row(s, box.id).thumbnail_article_id == lid.id
        side = make_article(s, name="Side part L")
        no_base = make_assembled(s, name="Lamp", sku="EX-L1", fields=[[("-A", [(side.id, 2)]), ("-B", [(lid.id, 2)])]])
        assert assembled.assembled_row(s, no_base.id).thumbnail_article_id == side.id


def test_used_in_names_the_assembled_articles(db):  # Spec 10.3
    with db.begin() as s:
        composite, ids = hose(s)
        make_assembled(s, name="Bracket", base=[(ids["Side part L"], 2)])
        assert [name for _id, name in assembled.used_in(s, ids["Side part L"])] == ["Bracket", "Hose"]
        assert assembled.used_in(s, ids["Socket R40"]) == [(composite.id, "Hose")]
