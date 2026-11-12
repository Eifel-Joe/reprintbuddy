import pytest

from reprintbuddy.domain.errors import ValidationError
from reprintbuddy.domain.stock import MAX_QTY
from reprintbuddy.web.forms import error_text, parse_bool, parse_int, parse_text


@pytest.mark.parametrize("raw", ["", "abc", "1.5", "1e9", "-1", "1000001", "9" * 20, "9" * 5000, " ", "١٢"])
def test_parse_int_rejects(raw):
    with pytest.raises(ValidationError):
        parse_int(raw, field="bookings.field.qty", min=0, max=MAX_QTY) if raw != "" else parse_int(
            raw, field="bookings.field.qty", min=0, max=MAX_QTY
        )


def test_parse_int_none_rejected():
    with pytest.raises(ValidationError) as info:
        parse_int(None, field="bookings.field.qty", min=0, max=10)
    assert info.value.key == "form.required"


def test_parse_int_range_error_carries_bounds():
    with pytest.raises(ValidationError) as info:
        parse_int("11", field="bookings.field.qty", min=1, max=10)
    assert info.value.key == "form.invalid_number"
    assert info.value.params == {"field": "bookings.field.qty", "min": 1, "max": 10}


def test_parse_int_accepts():
    assert parse_int(" 7 ", field="f", min=0, max=10) == 7
    assert parse_int("", field="f", min=0, max=10, default=0) == 0
    assert parse_int("-3", field="f", min=-5, max=10) == -3


def test_parse_text():
    assert parse_text("  x  ", field="f", max_len=5) == "x"
    assert parse_text("   ", field="f", max_len=5) is None
    assert parse_text(None, field="f", max_len=5) is None
    with pytest.raises(ValidationError) as info:
        parse_text("", field="f", max_len=5, required=True)
    assert info.value.key == "form.required"
    with pytest.raises(ValidationError) as info:
        parse_text("123456", field="f", max_len=5)
    assert info.value.key == "form.too_long"


def test_parse_bool():
    assert parse_bool("on") is True
    assert parse_bool("1") is True
    assert parse_bool(None) is False
    assert parse_bool("") is False
    assert parse_bool("off") is False


def test_error_text_translates_field():
    err = ValidationError("form.invalid_number", field="parts.field.pack_size", min=1, max=1000000)
    assert "Bestelleinheit" in error_text("de", err)
    assert "ganze Zahl" in error_text("de", err)
    assert "whole number" in error_text("en", err)


def test_error_text_lists_names_and_marks_archived_ones():  # plan approval v0.4.3, condition A1
    from reprintbuddy.domain.errors import ConflictError
    err = ConflictError("sets.breaks_parent", names=[("Complete kit", True), ("Top", False)], name="Plate")
    assert error_text("de", err) == "Geht nicht: In Complete kit (archiviert), Top stünde Plate dann doppelt."
    assert error_text("en", err) == "Not possible: Plate would then be in Complete kit (archived), Top twice."
