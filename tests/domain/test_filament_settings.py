import pytest

from factories import make_legacy_sort, make_sort
from reprintbuddy.domain import filament
from reprintbuddy.domain.errors import NotFoundError, ValidationError
from reprintbuddy.domain.filament import FilamentSettingsInput


def test_save_creates_then_replaces(db):
    with db.begin() as s:
        sid = make_sort(s).id
        assert filament.get_settings(s, sid) is None
        filament.save_settings(s, sid, FilamentSettingsInput(
            lead_time_days=5, safety_value=300, safety_unit="grams", supplier="  Example Supplier ",
            order_url="https://shop.example/pla-black"))
        row = filament.get_settings(s, sid)
        assert (row.lead_time_days, row.safety_unit, row.supplier, row.pack_grams) == (5, "grams", "Example Supplier", 1000)
        filament.save_settings(s, sid, FilamentSettingsInput(lead_time_days=2, supplier=""))
        row = filament.get_settings(s, sid)
        assert (row.lead_time_days, row.supplier, row.order_url) == (2, None, None)


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
    "fields,key",
    [({"order_url": "ftp://x"}, "filament.invalid_url"),
     ({"safety_unit": "qty"}, "filament.invalid_safety_unit"),
     ({"pack_grams": 0}, "form.invalid_number"), ({"pack_grams": 100_001}, "form.invalid_number"),
     ({"lead_time_days": 366}, "form.invalid_number"), ({"safety_value": 1_000_001}, "form.invalid_number"),
     ({"min_order_packs": 0}, "form.invalid_number"), ({"supplier": "x" * 201}, "form.too_long")],
)
def test_invalid_settings_store_nothing(db, fields, key):
    with db.begin() as s:
        sid = make_sort(s).id
        with pytest.raises(ValidationError) as e:
            filament.save_settings(s, sid, FilamentSettingsInput(**fields))
        assert e.value.key == key
        assert filament.get_settings(s, sid) is None
