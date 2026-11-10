import pytest
from sqlalchemy import select

from reprintbuddy.domain import catalog
from reprintbuddy.domain.catalog import PartInput, SupplierInput
from reprintbuddy.domain.errors import NotFoundError, ValidationError
from reprintbuddy.models import ActivityEntry, JournalEntry, Part, PartSupplier

SUPPLIER_KEYS = {"pack_size", "min_order_packs", "lead_time_days", "supplier", "order_url"}


def first_supplier(**over):
    fields = dict(
        name="Example Supplier", order_url="https://shop.example/insert-m4", pack_size=50,
        min_order_packs=1, lead_time_days=5, is_default=True,
    )
    fields.update(over)
    return SupplierInput(**fields)


def second_supplier(**over):
    fields = dict(name="Example Supplier B", order_url=None, pack_size=10, min_order_packs=2, lead_time_days=1)
    fields.update(over)
    return SupplierInput(**fields)


def insert_m4(**over):
    """The part with one default supplier; `supplier=` sets its name, the other supplier keys its values."""
    own = {k: over.pop(k) for k in list(over) if k in SUPPLIER_KEYS}
    if "supplier" in own:
        own["name"] = own.pop("supplier")
    fields = dict(name="Insert M4", safety_value=10, safety_unit="qty", suppliers=[first_supplier(**own)])
    fields.update(over)
    return PartInput(**fields)


def rows_of(s, part_id):
    return [
        (r.position, r.name, r.order_url, r.pack_size, r.min_order_packs, r.lead_time_days, r.is_default)
        for r in catalog.part_suppliers(s, part_id)
    ]


def test_create_part_spec_fields(db):  # Spec 5.3
    with db.begin() as s:
        part_id = catalog.create_part(s, insert_m4(), 0, "user").id
    with db.begin() as s:
        p = s.get(Part, part_id)
        assert (p.name, p.unit, p.safety_value, p.safety_unit, p.stock) == ("Insert M4", "pcs", 10, "qty", 0)
        assert rows_of(s, part_id) == [(0, "Example Supplier", "https://shop.example/insert-m4", 50, 1, 5, True)]


def test_initial_stock_journaled(db):  # Spec 6.5
    with db.begin() as s:
        part_id = catalog.create_part(s, insert_m4(), 50, "user").id
    with db.begin() as s:
        assert s.get(Part, part_id).stock == 50
        rows = s.scalars(select(JournalEntry)).all()
        assert len(rows) == 1
        assert (rows[0].kind, rows[0].qty, rows[0].part_id) == ("inventory", 50, part_id)


def test_initial_zero_no_journal(db):
    with db.begin() as s:
        catalog.create_part(s, insert_m4(), 0, "user")
    with db.begin() as s:
        assert s.scalars(select(JournalEntry)).all() == []


def test_update_part_keeps_stock(db):
    with db.begin() as s:
        part_id = catalog.create_part(s, insert_m4(), 50, "user").id
    with db.begin() as s:
        catalog.update_part(s, part_id, insert_m4(name="Insert M4 long", pack_size=100))
    with db.begin() as s:
        p = s.get(Part, part_id)
        assert (p.name, catalog.default_supplier(s, part_id).pack_size, p.stock) == ("Insert M4 long", 100, 50)


def test_update_unknown_part(db):
    with pytest.raises(NotFoundError) as info:
        with db.begin() as s:
            catalog.update_part(s, 999, insert_m4())
    assert info.value.key == "parts.not_found"


@pytest.mark.parametrize(
    "over",
    [
        {"pack_size": 0}, {"order_url": "ftp://x"}, {"safety_unit": "weeks"}, {"name": "  "},
        {"unit": ""}, {"unit": "x" * 21}, {"min_stock": -1}, {"min_order_packs": 0},
        {"lead_time_days": 366}, {"safety_value": -1}, {"supplier": "x" * 201},
    ],
)
def test_part_validation(db, over):
    with pytest.raises(ValidationError):
        with db.begin() as s:
            catalog.create_part(s, insert_m4(**over), 0, "user")
    with db.begin() as s:
        assert s.scalars(select(Part)).all() == []


def test_part_text_cleanup(db):
    with db.begin() as s:
        p = catalog.create_part(s, insert_m4(supplier="  ", order_url=" "), 0, "user")
        assert rows_of(s, p.id)[0][1:3] == (None, None)


def test_negative_initial_stock_rejected(db):
    with pytest.raises(ValidationError):
        with db.begin() as s:
            catalog.create_part(s, insert_m4(), -1, "user")


def test_list_parts_sorted(db):
    with db.begin() as s:
        catalog.create_part(s, insert_m4(name="b part"), 0, "user")
        catalog.create_part(s, insert_m4(name="A part"), 0, "user")
    with db.begin() as s:
        assert [p.name for p in catalog.list_parts(s)] == ["A part", "b part"]


# ---- several suppliers (Spec 5.3) -----------------------------------------------------------


def test_a_part_without_suppliers_given_gets_one_default_supplier(db):
    with db.begin() as s:
        part_id = catalog.create_part(s, PartInput(name="Rubber foot"), 0, "user").id
    with db.begin() as s:
        assert rows_of(s, part_id) == [(0, None, None, 1, 1, 0, True)]


def test_create_with_two_suppliers_in_their_order(db):
    with db.begin() as s:
        part_id = catalog.create_part(s, insert_m4(suppliers=[first_supplier(), second_supplier()]), 0, "user").id
    with db.begin() as s:
        assert rows_of(s, part_id) == [
            (0, "Example Supplier", "https://shop.example/insert-m4", 50, 1, 5, True),
            (1, "Example Supplier B", None, 10, 2, 1, False),
        ]
        assert catalog.default_supplier(s, part_id).name == "Example Supplier"


def test_the_default_may_be_any_supplier(db):
    with db.begin() as s:
        part_id = catalog.create_part(
            s, insert_m4(suppliers=[first_supplier(is_default=False), second_supplier(is_default=True)]), 0, "user"
        ).id
    with db.begin() as s:
        assert catalog.default_supplier(s, part_id).position == 1


@pytest.mark.parametrize("suppliers, key", [
    ([], "parts.suppliers_count"),
    ([second_supplier(is_default=i == 0) for i in range(11)], "parts.suppliers_count"),
    ([first_supplier(is_default=False), second_supplier()], "parts.supplier_default"),
    ([first_supplier(), second_supplier(is_default=True)], "parts.supplier_default"),
])
def test_one_to_ten_suppliers_and_exactly_one_default(db, suppliers, key):
    with pytest.raises(ValidationError) as info:
        with db.begin() as s:
            catalog.create_part(s, insert_m4(suppliers=suppliers), 0, "user")
    assert info.value.key == key
    with db.begin() as s:
        assert s.scalars(select(Part)).all() == []


@pytest.mark.parametrize("over", [
    {"pack_size": 0}, {"order_url": "ftp://x"}, {"min_order_packs": 1001}, {"lead_time_days": -1},
    {"name": "x" * 201}, {"order_url": "https://" + "x" * 2000},
])
def test_each_supplier_is_checked_and_named_in_the_error(db, over):
    with pytest.raises(ValidationError) as info:
        with db.begin() as s:
            catalog.create_part(s, insert_m4(suppliers=[first_supplier(), second_supplier(**over)]), 0, "user")
    assert info.value.params["supplier"] == 2


def test_saving_compares_by_position(db):
    """Unchanged rows stay as they are (no log line, no "changed" on other devices); changed rows
    change, new ones are added, rows beyond the list are deleted."""
    with db.begin() as s:
        part_id = catalog.create_part(s, insert_m4(suppliers=[first_supplier(), second_supplier()]), 0, "user").id
    with db.begin() as s:
        first_id = catalog.part_suppliers(s, part_id)[0].id
    with db.begin() as s:
        catalog.update_part(s, part_id, insert_m4(suppliers=[first_supplier()]))
    with db.begin() as s:
        rows = catalog.part_suppliers(s, part_id)
        assert [r.id for r in rows] == [first_id]
        assert s.scalars(select(PartSupplier)).all() == rows
    with db.begin() as s:
        catalog.update_part(
            s, part_id, insert_m4(suppliers=[first_supplier(is_default=False), second_supplier(is_default=True)])
        )
    with db.begin() as s:
        rows = catalog.part_suppliers(s, part_id)
        assert rows[0].id == first_id
        assert [(r.position, r.is_default) for r in rows] == [(0, False), (1, True)]


def test_saving_the_same_suppliers_logs_nothing(db):
    with db.begin() as s:
        part_id = catalog.create_part(s, insert_m4(suppliers=[first_supplier(), second_supplier()]), 0, "user").id
    with db.begin() as s:
        before = len(s.scalars(select(ActivityEntry)).all())
    with db.begin() as s:
        catalog.update_part(s, part_id, insert_m4(suppliers=[first_supplier(), second_supplier()]))
    with db.begin() as s:
        assert len(s.scalars(select(ActivityEntry)).all()) == before


def test_names_in_use_for_the_suggestions_while_typing(db):
    with db.begin() as s:
        catalog.create_part(s, insert_m4(suppliers=[first_supplier(), second_supplier()]), 0, "user")
        catalog.create_part(s, insert_m4(name="Screw", suppliers=[first_supplier(name="Example Supplier B")]), 0, "user")
        catalog.create_part(s, PartInput(name="Rubber foot"), 0, "user")
    with db.begin() as s:
        assert catalog.supplier_names(s) == ["Example Supplier", "Example Supplier B"]


def test_suppliers_of_all_parts_at_once(db):
    with db.begin() as s:
        a = catalog.create_part(s, insert_m4(suppliers=[first_supplier(), second_supplier()]), 0, "user").id
        b = catalog.create_part(s, PartInput(name="Rubber foot"), 0, "user").id
    with db.begin() as s:
        found = catalog.suppliers_by_part(s)
        assert [r.position for r in found[a]] == [0, 1] and [r.position for r in found[b]] == [0]
