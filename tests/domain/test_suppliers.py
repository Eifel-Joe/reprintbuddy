"""The suppliers of parts and spool sorts share one set of rules (Spec 5.3, 9.3)."""

import pytest

from reprintbuddy.domain import suppliers
from reprintbuddy.domain.errors import ValidationError
from reprintbuddy.domain.suppliers import FILAMENT_LABELS, PART_LABELS, SupplierInput

from factories import make_filament_settings, make_part, make_sort


def test_names_in_use_come_from_parts_and_spool_sorts(db):
    """Spec 9.3: names already used at filament and parts are offered while typing."""
    with db.begin() as s:
        make_part(s, supplier="Shop B")
        make_part(s, name="Rubber foot", supplier="shop a")
        make_filament_settings(s, make_sort(s, brand="Brand A").id, supplier="Shop B")
        make_filament_settings(s, make_sort(s, brand="Brand B").id, supplier="Roll Shop")
        make_filament_settings(s, make_sort(s, brand="Brand C").id)  # no name
        assert suppliers.names_in_use(s) == ["Roll Shop", "shop a", "Shop B"]


@pytest.mark.parametrize("labels", [PART_LABELS, FILAMENT_LABELS], ids=["part", "filament"])
def test_one_to_ten_with_exactly_one_default(labels):
    with pytest.raises(ValidationError) as none:
        suppliers.check([], labels)
    assert none.value.key == labels.count
    with pytest.raises(ValidationError) as eleven:
        suppliers.check([SupplierInput(is_default=i == 0) for i in range(11)], labels)
    assert eleven.value.key == labels.count
    with pytest.raises(ValidationError) as two:
        suppliers.check([SupplierInput(is_default=True), SupplierInput(is_default=True)], labels)
    assert two.value.key == labels.default
    assert len(suppliers.check([SupplierInput(is_default=True)] + [SupplierInput()] * 9, labels)) == 10


def test_an_error_names_the_field_of_its_kind_and_the_supplier_number():
    items = [SupplierInput(is_default=True, pack_size=1000), SupplierInput(pack_size=0)]
    with pytest.raises(ValidationError) as error:
        suppliers.check(items, FILAMENT_LABELS)
    assert error.value.key == "form.invalid_number"
    assert error.value.params["field"] == "filament.field.pack_grams"
    assert error.value.params["supplier"] == 2
    with pytest.raises(ValidationError) as url:
        suppliers.check([SupplierInput(is_default=True, order_url="shop.example")], FILAMENT_LABELS)
    assert url.value.key == "filament.invalid_url"
    assert "supplier" not in url.value.params  # one supplier: no number


def test_a_roll_has_at_most_100_kg():
    with pytest.raises(ValidationError):
        suppliers.check([SupplierInput(is_default=True, pack_size=100_001)], FILAMENT_LABELS)
    assert suppliers.check([SupplierInput(is_default=True, pack_size=100_000)], FILAMENT_LABELS)[0].pack_size == 100_000
