"""The checks of single form values that the catalog, the suppliers and the filament settings share.

They lived as private copies in `catalog.py` and `suppliers.py`, and `assembled.py` and
`filament.py` reached into the catalog for them (0.4.7, R4). Tests: tests/test_small_helpers.py.
"""

from __future__ import annotations

from reprintbuddy.domain.errors import ValidationError


def int_in(value: object, field_key: str, low: int, high: int) -> int:
    """`value` when it is a whole number from `low` to `high`; a bool or anything else is refused."""
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValidationError("form.invalid_number", field=field_key, min=low, max=high)
    return value


def text(value: str | None, field_key: str, max_len: int, *, required: bool = False) -> str | None:
    """Trimmed text; empty becomes None (or an error when required)."""
    value = (value or "").strip()
    if not value:
        if required:
            raise ValidationError("form.required", field=field_key)
        return None
    if len(value) > max_len:
        raise ValidationError("form.too_long", field=field_key, max=max_len)
    return value
