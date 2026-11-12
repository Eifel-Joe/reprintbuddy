"""Articles made of parts: create, check, keep the variant rows (Spec 5.6, 0.4.0 p341)."""

import pytest
from sqlalchemy import select

from factories import make_article, make_part
from reprintbuddy.domain import assembled, catalog, stock
from reprintbuddy.domain.assembled import AssembledInput, OptionInput
from reprintbuddy.domain.catalog import ArticleInput, BaseInput, ChoiceInput, ChoiceOptionInput, SetInput
from reprintbuddy.domain.errors import ConflictError, ValidationError
from reprintbuddy.models import Article, JournalEntry, Order, OrderLine


def B(qty, article_id):
    return BaseInput(qty, article_id)


def opt(suffix, *rows, option_id=None):
    return OptionInput(option_id, suffix, [B(q, a) for q, a in rows])


@pytest.fixture
def parts(db):
    """Print articles of the examples: panels, segment, side parts and sockets of the hose."""
    with db.begin() as s:
        names = ["Panel " + c for c in "ABCDEFGHIJ"] + [
            "Segment", "Side part L", "Side part R", "Socket L32", "Socket L40", "Socket R32", "Socket R40", "Holder",
        ]
        ids = {n: make_article(s, name=n).id for n in names}
        ids["insert"] = make_part(s, name="Thread insert").id
    return ids


def hose_input(p, **changes):
    data = AssembledInput(
        name="Hose", sku="EX-H1", mode="order", base=[B(12, p["Segment"])],
        fields=[[
            opt("-L32", (1, p["Side part L"]), (1, p["Socket L32"])),
            opt("-L40", (1, p["Side part L"]), (1, p["Socket L40"])),
            opt("-R32", (1, p["Side part R"]), (1, p["Socket R32"])),
            opt("-R40", (1, p["Side part R"]), (1, p["Socket R40"])),
        ]],
        parts={},
    )
    for key, value in changes.items():
        setattr(data, key, value)
    return data


def box_input(p, **changes):
    data = AssembledInput(
        name="Storage box", sku="EX-B1", mode="stock",
        base=[B(1, p["Panel " + c]) for c in "ABCDEFGHIJ"], fields=[], parts={}, min_stock=2, buffer=1,
    )
    for key, value in changes.items():
        setattr(data, key, value)
    return data


def variants(s, composite_id):
    return [(r.sku, r.variant_key is not None) for r in assembled.variants(s, s.get(Article, composite_id))]


def test_box_with_ten_base_parts_on_stock(db, parts):
    with db.begin() as s:
        box = assembled.create_assembled(s, box_input(parts))
        assert (box.kind, box.assembly_mode, box.sold_alone) == ("assembled", "stock", True)
        assert (box.min_stock, box.buffer, box.library_file_id) == (2, 1, None)
        assert assembled.holders(s, box) == [box]
        assert s.scalars(select(Article).where(Article.parent_id == box.id)).all() == []
        assert assembled.needs_of(s, box) == {parts["Panel " + c]: 1 for c in "ABCDEFGHIJ"}


def test_hose_has_four_variants_with_composed_numbers(db, parts):
    with db.begin() as s:
        hose = assembled.create_assembled(s, hose_input(parts))
        rows = assembled.variants(s, hose)
        assert [r.sku for r in rows] == ["EX-H1-L32", "EX-H1-L40", "EX-H1-R32", "EX-H1-R40"]
        assert {(r.kind, r.parent_id, r.name) for r in rows} == {("variant", hose.id, "Hose")}
        assert assembled.holders(s, hose) == []  # built on order: no stock of its own
        assert assembled.needs_of(s, rows[1]) == {parts["Segment"]: 12, parts["Side part L"]: 1, parts["Socket L40"]: 1}


def test_stock_mode_with_choices_holds_stock_per_variant(db, parts):
    with db.begin() as s:
        hose = assembled.create_assembled(s, hose_input(parts, mode="stock"))
        assert assembled.holders(s, hose) == assembled.variants(s, hose)
        assert len(assembled.holders(s, hose)) == 4


def test_article_may_repeat_across_options_of_one_field(db, parts):
    with db.begin() as s:
        assembled.create_assembled(s, hose_input(parts))  # side part L in -L32 and -L40


def test_article_twice_in_one_option_is_refused(db, parts):
    data = hose_input(parts)
    data.fields[0][0] = opt("-L32", (1, parts["Side part L"]), (2, parts["Side part L"]))
    with pytest.raises(ValidationError) as err, db.begin() as s:
        assembled.create_assembled(s, data)
    assert err.value.key == "assembled.article_twice" and err.value.params["name"] == "Side part L"


def test_base_part_in_an_option_is_refused(db, parts):
    data = hose_input(parts)
    data.fields[0][0] = opt("-L32", (1, parts["Segment"]), (1, parts["Socket L32"]))
    with pytest.raises(ValidationError) as err, db.begin() as s:
        assembled.create_assembled(s, data)
    assert err.value.key == "assembled.article_twice" and err.value.params["name"] == "Segment"


def test_article_in_both_fields_is_refused(db, parts):
    data = hose_input(parts)
    data.fields.append([opt("-X", (1, parts["Socket L32"])), opt("-Y", (1, parts["Holder"]))])
    with pytest.raises(ValidationError) as err, db.begin() as s:
        assembled.create_assembled(s, data)
    assert err.value.key == "assembled.article_twice" and err.value.params["name"] == "Socket L32"


def test_only_print_articles_can_be_parts(db, parts):
    with db.begin() as s:
        box = assembled.create_assembled(s, box_input(parts)).id
        hose = assembled.create_assembled(s, hose_input(parts))
        variant = assembled.variants(s, hose)[0].id
    for article_id in (box, variant):
        with pytest.raises(ValidationError) as err, db.begin() as s:
            assembled.create_assembled(s, AssembledInput(
                name="Lamp", sku=None, mode="stock", base=[B(1, article_id), B(1, parts["Holder"])], fields=[], parts={},
            ))
        assert err.value.key == "assembled.part_not_print"


def test_archived_part_is_refused(db, parts):
    with db.begin() as s:
        s.get(Article, parts["Holder"]).archived_at = catalog_now()
    with pytest.raises(ValidationError) as err, db.begin() as s:
        assembled.create_assembled(s, AssembledInput(
            name="Lamp", sku=None, mode="stock", base=[B(2, parts["Holder"])], fields=[], parts={},
        ))
    assert err.value.key == "assembled.part_archived"


def catalog_now():
    from reprintbuddy.models import utcnow

    return utcnow()


@pytest.mark.parametrize("base, purchased, ok", [
    ([(12, "Segment")], {}, True),
    ([(1, "Holder")], {"insert": 2}, True),
    ([(1, "Holder")], {}, False),
    ([], {"insert": 2}, False),
])
def test_size_counts_units_and_purchased_parts(db, parts, base, purchased, ok):  # Spec 5.6 "Umfang"
    data = AssembledInput(
        name="Holder mounted", sku=None, mode="stock", base=[B(q, parts[n]) for q, n in base], fields=[],
        parts={parts[k]: q for k, q in purchased.items()},
    )
    if ok:
        with db.begin() as s:
            assembled.create_assembled(s, data)
    else:
        with pytest.raises(ValidationError) as err, db.begin() as s:
            assembled.create_assembled(s, data)
        assert err.value.key == "assembled.too_small"


def test_number_and_suffixes_are_required_with_choices(db, parts):
    with pytest.raises(ValidationError) as err, db.begin() as s:
        assembled.create_assembled(s, hose_input(parts, sku=None))
    assert err.value.key == "assembled.sku_required"
    data = hose_input(parts)
    data.fields[0][1].suffix = " "
    with pytest.raises(ValidationError) as err, db.begin() as s:
        assembled.create_assembled(s, data)
    assert err.value.key == "assembled.suffix_required"
    data = hose_input(parts)
    data.fields[0][1].suffix = "-L32"
    with pytest.raises(ValidationError) as err, db.begin() as s:
        assembled.create_assembled(s, data)
    assert (err.value.key, err.value.params["suffix"]) == ("assembled.suffix_twice", "-L32")


def test_choices_2_need_choices_1_and_options_need_rows(db, parts):
    data = hose_input(parts)
    data.fields = [[], [opt("-A", (1, parts["Holder"]))]]
    with pytest.raises(ValidationError) as err, db.begin() as s:
        assembled.create_assembled(s, data)
    assert err.value.key == "assembled.choices2_without_1"
    data = hose_input(parts)
    data.fields[0].append(opt("-X"))
    with pytest.raises(ValidationError) as err, db.begin() as s:
        assembled.create_assembled(s, data)
    assert err.value.key == "assembled.option_empty"


def test_variant_number_too_long(db, parts):
    data = hose_input(parts, sku="E" * 62)
    with pytest.raises(ValidationError) as err, db.begin() as s:
        assembled.create_assembled(s, data)
    assert err.value.key == "assembled.variant_too_long"


def test_variant_number_must_be_free(db, parts):
    with db.begin() as s:
        make_article(s, name="Other", sku="EX-H1-R40")
    with pytest.raises(ConflictError) as err, db.begin() as s:
        assembled.create_assembled(s, hose_input(parts))
    assert (err.value.key, err.value.params["sku"]) == ("catalog.sku_taken", "EX-H1-R40")


def test_variant_number_of_a_set_variant_is_taken(db, parts):
    with db.begin() as s:  # the set's variants: EX-H1-L40 and EX-H1-LZZ
        catalog.create_set(s, SetInput("Kit", "EX-H1-L", [BaseInput(1, parts["Holder"])],
                                       [ChoiceInput(1, [ChoiceOptionInput(parts["Panel A"], "40"),
                                                        ChoiceOptionInput(parts["Panel B"], "ZZ")])]))
    with pytest.raises(ConflictError) as err, db.begin() as s:
        assembled.create_assembled(s, hose_input(parts))
    assert (err.value.key, err.value.params["sku"]) == ("catalog.sku_taken", "EX-H1-L40")


def test_set_cannot_take_a_variant_number_of_an_assembled_article(db, parts):
    with db.begin() as s:
        assembled.create_assembled(s, hose_input(parts))
    with pytest.raises(ConflictError) as err, db.begin() as s:
        catalog.create_set(s, SetInput("Kit", "EX-H1-L32", [BaseInput(1, parts["Holder"]), BaseInput(1, parts["Panel A"])], []))
    assert err.value.params["sku"] == "EX-H1-L32"
    with pytest.raises(ConflictError), db.begin() as s:
        catalog.create_set(s, SetInput("Kit", "EX-H1-L", [BaseInput(1, parts["Holder"])],
                                       [ChoiceInput(1, [ChoiceOptionInput(parts["Panel A"], "32"),
                                                        ChoiceOptionInput(parts["Panel B"], "99")])]))


def _hose_on_stock(db, parts):
    with db.begin() as s:
        hose = assembled.create_assembled(s, hose_input(parts, mode="stock"))
        rows = {r.sku: r.id for r in assembled.variants(s, hose)}
        option_ids = [o.id for o in assembled.options(s, hose)[0]]
    return hose.id, rows, option_ids


def _edit_input(parts, option_ids, suffixes=("-L32", "-L40", "-R32", "-R40"), keep=(0, 1, 2, 3)):
    rows = [
        ((1, parts["Side part L"]), (1, parts["Socket L32"])),
        ((1, parts["Side part L"]), (1, parts["Socket L40"])),
        ((1, parts["Side part R"]), (1, parts["Socket R32"])),
        ((1, parts["Side part R"]), (1, parts["Socket R40"])),
    ]
    return hose_input(parts, mode="stock", fields=[[
        opt(suffixes[i], *rows[i], option_id=option_ids[i]) for i in keep
    ]])


def test_stock_stays_with_variant_when_suffix_changes(db, parts):  # review focus 1
    hose, rows, option_ids = _hose_on_stock(db, parts)
    with db.begin() as s:
        stock.count_article(s, rows["EX-H1-L32"], "stock", 3, "user")
    with db.begin() as s:
        assembled.update_assembled(s, hose, _edit_input(parts, option_ids, suffixes=("-L33", "-L40", "-R32", "-R40")))
    with db() as s:
        row = s.get(Article, rows["EX-H1-L32"])
        assert (row.sku, row.stock, row.archived_at) == ("EX-H1-L33", 3, None)
        assert s.scalars(select(JournalEntry.article_id).where(JournalEntry.kind == "inventory")).all() == [row.id]


def test_suffixes_may_swap(db, parts):
    hose, rows, option_ids = _hose_on_stock(db, parts)
    with db.begin() as s:
        assembled.update_assembled(s, hose, _edit_input(parts, option_ids, suffixes=("-L40", "-L32", "-R32", "-R40")))
    with db() as s:
        assert s.get(Article, rows["EX-H1-L32"]).sku == "EX-H1-L40"
        assert s.get(Article, rows["EX-H1-L40"]).sku == "EX-H1-L32"


def test_variant_with_stock_cannot_be_removed(db, parts):
    hose, rows, option_ids = _hose_on_stock(db, parts)
    with db.begin() as s:
        stock.count_article(s, rows["EX-H1-R40"], "stock", 1, "user")
    with pytest.raises(ConflictError) as err, db.begin() as s:
        assembled.update_assembled(s, hose, _edit_input(parts, option_ids, keep=(0, 1, 2)))
    assert (err.value.key, err.value.params["sku"]) == ("assembled.variant_in_use", "EX-H1-R40")


def test_variant_in_an_open_order_cannot_be_removed(db, parts):
    hose, rows, option_ids = _hose_on_stock(db, parts)
    with db.begin() as s:
        order = Order(reference="R-1", order_date=catalog_now().date())
        s.add(order)
        s.flush()
        s.add(OrderLine(order_id=order.id, article_id=rows["EX-H1-R40"], qty=1))
    with pytest.raises(ConflictError) as err, db.begin() as s:
        assembled.update_assembled(s, hose, _edit_input(parts, option_ids, keep=(0, 1, 2)))
    assert err.value.key == "assembled.variant_in_use"


def test_removed_variant_without_stock_is_archived_and_revived(db, parts):
    hose, rows, option_ids = _hose_on_stock(db, parts)
    with db.begin() as s:
        assembled.update_assembled(s, hose, _edit_input(parts, option_ids, keep=(0, 1, 2)))
    with db() as s:
        assert s.get(Article, rows["EX-H1-R40"]).archived_at is not None
        assert [r.sku for r in assembled.variants(s, s.get(Article, hose))] == ["EX-H1-L32", "EX-H1-L40", "EX-H1-R32"]
    data = _edit_input(parts, option_ids, keep=(0, 1, 2))
    data.fields[0].append(opt("-R40", (1, parts["Side part R"]), (1, parts["Socket R40"])))  # a new option
    with db.begin() as s:
        assembled.update_assembled(s, hose, data)
    with db() as s:
        revived = s.get(Article, rows["EX-H1-R40"])
        assert revived.archived_at is None and revived.sku == "EX-H1-R40"  # the number is not locked for good
        assert len(assembled.variants(s, s.get(Article, hose))) == 4


def test_adding_choices_to_a_box_with_stock_is_refused(db, parts):
    with db.begin() as s:
        box = assembled.create_assembled(s, box_input(parts)).id
        stock.count_article(s, box, "stock", 2, "user")
    data = box_input(parts, fields=[[opt("-A", (1, parts["Holder"])), opt("-B", (1, parts["Segment"]))]])
    with pytest.raises(ConflictError) as err, db.begin() as s:
        assembled.update_assembled(s, box, data)
    assert (err.value.key, err.value.params["sku"]) == ("assembled.variant_in_use", "EX-B1")


def test_variant_targets_on_stock_and_zero_on_order(db, parts):
    hose, rows, option_ids = _hose_on_stock(db, parts)
    data = _edit_input(parts, option_ids)
    data.variant_targets = {rows["EX-H1-L32"]: (2, 1)}
    with db.begin() as s:
        assembled.update_assembled(s, hose, data)
    with db() as s:
        assert [(r.min_stock, r.buffer) for r in assembled.variants(s, s.get(Article, hose))] == [(2, 1), (0, 0), (0, 0), (0, 0)]


def test_order_mode_variants_carry_only_their_number(db, parts):
    data = hose_input(parts, min_stock=3, buffer=2)
    with db.begin() as s:
        hose = assembled.create_assembled(s, data)
        assert (hose.min_stock, hose.buffer) == (0, 0)
        assert {(r.min_stock, r.buffer, r.stock) for r in assembled.variants(s, hose)} == {(0, 0, 0)}


def test_mode_switch_refused_with_stock(db, parts):
    hose, rows, option_ids = _hose_on_stock(db, parts)
    with db.begin() as s:
        stock.count_article(s, rows["EX-H1-L32"], "stock", 1, "user")
    data = _edit_input(parts, option_ids)
    data.mode = "order"
    with pytest.raises(ConflictError) as err, db.begin() as s:
        assembled.update_assembled(s, hose, data)
    assert err.value.key == "assembled.mode_switch_busy"


def test_sold_alone_defaults_to_on_for_print_articles(db, parts):
    with db.begin() as s:
        assert s.get(Article, parts["Holder"]).sold_alone is True
        assert ArticleInput(name="x").sold_alone is True
        catalog.update_article(s, parts["Holder"], ArticleInput(name="Holder", sold_alone=False))
        assert s.get(Article, parts["Holder"]).sold_alone is False


def _edit_on_order(parts, option_ids, **kw):
    data = _edit_input(parts, option_ids, **kw)
    return AssembledInput(name=data.name, sku=data.sku, mode="order", base=data.base, fields=data.fields, parts={})


def test_renamed_variant_in_an_open_order_cannot_be_removed(db, parts):  # p360 (Spec 5.6)
    """The order keeps the number as ordered; a later suffix change must not hide it from the check."""
    from datetime import date

    from reprintbuddy.domain import orders
    from reprintbuddy.domain.orders import AssembledItem

    with db.begin() as s:
        hose = assembled.create_assembled(s, hose_input(parts))
        keys = {r.sku: r.variant_key for r in assembled.variants(s, hose)}
        option_ids = [o.id for o in assembled.options(s, hose)[0]]
        orders.create_order(
            s, reference="R-1", order_date=date(2026, 11, 12),
            items=[AssembledItem(hose.id, 1, keys["EX-H1-R40"])], actor="user",
        )
        hose_id = hose.id
    with db.begin() as s:  # the number changes; the order keeps EX-H1-R40
        assembled.update_assembled(s, hose_id, _edit_on_order(parts, option_ids, suffixes=("-L32", "-L40", "-R32", "-R41")))
    with pytest.raises(ConflictError) as err, db.begin() as s:
        assembled.update_assembled(s, hose_id, _edit_on_order(parts, option_ids, suffixes=("-L32", "-L40", "-R32", "-R41"), keep=(0, 1, 2)))
    assert (err.value.key, err.value.params["sku"]) == ("assembled.variant_in_use", "EX-H1-R41")


def test_renamed_base_number_keeps_an_ordered_variant(db, parts):  # p360, sister path: the base number
    from datetime import date

    from reprintbuddy.domain import orders
    from reprintbuddy.domain.orders import AssembledItem

    with db.begin() as s:
        hose = assembled.create_assembled(s, hose_input(parts))
        keys = {r.sku: r.variant_key for r in assembled.variants(s, hose)}
        rows = {r.sku: r.id for r in assembled.variants(s, hose)}
        option_ids = [o.id for o in assembled.options(s, hose)[0]]
        placed = orders.create_order(
            s, reference="R-1", order_date=date(2026, 11, 12),
            items=[AssembledItem(hose.id, 1, keys["EX-H1-R40"])], actor="user",
        )
        assert {line.variant_id for line in s.scalars(select(OrderLine).where(OrderLine.order_id == placed.id))} == {rows["EX-H1-R40"]}
        hose_id = hose.id
    renamed = _edit_on_order(parts, option_ids)
    renamed = AssembledInput(name="Hose", sku="EX-H2", mode="order", base=renamed.base, fields=renamed.fields, parts={})
    with db.begin() as s:
        assembled.update_assembled(s, hose_id, renamed)
    dropped = _edit_on_order(parts, option_ids, keep=(0, 1, 2))
    dropped = AssembledInput(name="Hose", sku="EX-H2", mode="order", base=dropped.base, fields=dropped.fields, parts={})
    with pytest.raises(ConflictError) as err, db.begin() as s:
        assembled.update_assembled(s, hose_id, dropped)
    assert (err.value.key, err.value.params["sku"]) == ("assembled.variant_in_use", "EX-H2-R40")
