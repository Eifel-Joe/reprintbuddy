import pytest
from sqlalchemy import select

from reprintbuddy.domain import catalog
from reprintbuddy.domain.catalog import PartInput
from reprintbuddy.domain.errors import NotFoundError, ValidationError
from reprintbuddy.models import JournalEntry, Part


def insert_m4(**over):
    fields = dict(
        name="Insert M4", pack_size=50, min_order_packs=1, lead_time_days=5, safety_value=10,
        safety_unit="qty", supplier="Example Supplier", order_url="https://shop.example/insert-m4",
    )
    fields.update(over)
    return PartInput(**fields)


def test_create_part_spec_fields(db):  # Spec 5.3
    with db.begin() as s:
        part_id = catalog.create_part(s, insert_m4(), 0, "user").id
    with db.begin() as s:
        p = s.get(Part, part_id)
        assert (p.name, p.unit, p.pack_size, p.min_order_packs) == ("Insert M4", "pcs", 50, 1)
        assert (p.lead_time_days, p.safety_value, p.safety_unit) == (5, 10, "qty")
        assert (p.supplier, p.order_url) == ("Example Supplier", "https://shop.example/insert-m4")
        assert p.stock == 0


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
        assert (p.name, p.pack_size, p.stock) == ("Insert M4 long", 100, 50)


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
        assert p.supplier is None and p.order_url is None


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
