import pytest
from sqlalchemy import select

from factories import make_filament_settings, make_legacy_sort, make_sort
from reprintbuddy.domain import filament
from reprintbuddy.domain.errors import NotFoundError, ValidationError
from reprintbuddy.domain.filament import FilamentSettingsInput
from reprintbuddy.domain.suppliers import SupplierInput
from reprintbuddy.models import FilamentSupplier


def _rows(s, sid):
    return [
        (r.position, r.name, r.order_url, r.pack_size, r.min_order_packs, r.lead_time_days, r.is_default)
        for r in filament.sort_suppliers(s, sid)
    ]


def test_save_creates_then_replaces(db):
    """Spec 9.3: the safety margin at the sort, the rest per supplier; saved once, one at least."""
    with db.begin() as s:
        sid = make_sort(s).id
        assert filament.get_settings(s, sid) is None
        assert filament.sort_suppliers(s, sid) == []
        filament.save_settings(s, sid, FilamentSettingsInput(
            safety_value=300, safety_unit="grams",
            suppliers=[SupplierInput(name="  Example Supplier ", order_url="https://shop.example/pla-black",
                                     pack_size=1000, lead_time_days=5, is_default=True)]))
        row = filament.get_settings(s, sid)
        assert (row.safety_value, row.safety_unit) == (300, "grams")
        assert _rows(s, sid) == [(0, "Example Supplier", "https://shop.example/pla-black", 1000, 1, 5, True)]
        filament.save_settings(s, sid, FilamentSettingsInput(suppliers=[
            SupplierInput(name="", pack_size=1000, lead_time_days=2),
            SupplierInput(name="Roll Shop", pack_size=500, min_order_packs=2, lead_time_days=1, is_default=True),
        ]))
        assert _rows(s, sid) == [(0, None, None, 1000, 1, 2, False), (1, "Roll Shop", None, 500, 2, 1, True)]
        filament.save_settings(s, sid, FilamentSettingsInput(suppliers=[
            SupplierInput(name="Roll Shop", pack_size=500, min_order_packs=2, lead_time_days=1, is_default=True),
        ]))
        assert _rows(s, sid) == [(0, "Roll Shop", None, 500, 2, 1, True)]


def test_the_default_input_is_one_default_supplier_of_one_kilo(db):
    with db.begin() as s:
        sid = make_sort(s).id
        filament.save_settings(s, sid, FilamentSettingsInput())
        assert _rows(s, sid) == [(0, None, None, 1000, 1, 0, True)]


def test_saving_the_same_values_writes_nothing(db):
    """Unchanged rows are not touched: no log line, no live message (Spec 10.5, 10.7)."""
    with db.begin() as s:
        sid = make_filament_settings(s, make_sort(s).id, supplier="Shop", pack_grams=750).spool_filament_id
    with db.begin() as s:
        filament.save_settings(s, sid, FilamentSettingsInput(
            suppliers=[SupplierInput(name="Shop", pack_size=750, is_default=True)]))
        assert not s.dirty and not s.new and not s.deleted


def test_unknown_sort(db):
    with db.begin() as s:
        with pytest.raises(NotFoundError) as e:
            filament.save_settings(s, 99, FilamentSettingsInput())
        assert e.value.key == "filament.sort_not_found"


def test_a_legacy_sort_takes_no_settings(db):  # it waits for the conversion and shows nowhere
    with db.begin() as s:
        old = make_legacy_sort(s).id
        with pytest.raises(NotFoundError) as e:
            filament.save_settings(s, old, FilamentSettingsInput())
        assert e.value.key == "filament.sort_not_found"
        assert filament.get_settings(s, old) is None


@pytest.mark.parametrize(
    "fields,supplier,key",
    [({}, {"order_url": "ftp://x"}, "filament.invalid_url"),
     ({"safety_unit": "qty"}, {}, "filament.invalid_safety_unit"),
     ({}, {"pack_size": 0}, "form.invalid_number"), ({}, {"pack_size": 100_001}, "form.invalid_number"),
     ({}, {"lead_time_days": 366}, "form.invalid_number"), ({"safety_value": 1_000_001}, {}, "form.invalid_number"),
     ({}, {"min_order_packs": 0}, "form.invalid_number"), ({}, {"name": "x" * 201}, "form.too_long"),
     ({}, {"is_default": False}, "filament.supplier_default")],
)
def test_invalid_settings_store_nothing(db, fields, supplier, key):
    with db.begin() as s:
        sid = make_sort(s).id
        one = SupplierInput(**{"pack_size": 1000, "is_default": True, **supplier})
        with pytest.raises(ValidationError) as e:
            filament.save_settings(s, sid, FilamentSettingsInput(**fields, suppliers=[one]))
        assert e.value.key == key
        assert filament.get_settings(s, sid) is None
        assert s.scalars(select(FilamentSupplier)).all() == []


def test_eleven_suppliers_are_refused(db):
    with db.begin() as s:
        sid = make_sort(s).id
        many = [SupplierInput(pack_size=1000, is_default=i == 0) for i in range(11)]
        with pytest.raises(ValidationError) as e:
            filament.save_settings(s, sid, FilamentSettingsInput(suppliers=many))
        assert (e.value.key, e.value.params["max"]) == ("filament.suppliers_count", 10)
