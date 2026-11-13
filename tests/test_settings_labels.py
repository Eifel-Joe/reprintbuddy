"""The settings of the label printing (Spec 8.5): printer address, roll, and the chosen details."""

import pytest

from reprintbuddy import settings as st
from reprintbuddy.labels import fields


def test_defaults_no_printer_the_one_roll_and_the_usual_details(session):
    assert st.get_setting(session, "label_printer_host") is None
    assert st.get_setting(session, "label_roll") == "DK-11209"
    assert tuple(st.get_setting(session, "label_fields")) == fields.DEFAULT_FIELDS == ("name", "sku", "label_text")


@pytest.mark.parametrize(
    "typed,stored",
    [
        ("198.51.100.20", "198.51.100.20"),
        ("  198.51.100.20  ", "198.51.100.20"),
        ("printer-01", "printer-01"),
        ("printer-01.example", "printer-01.example"),
        ("Printer.Example.", "Printer.Example"),
        ("2001:db8::7", "2001:db8::7"),
        (None, None),
        ("", None),
        ("   ", None),
    ],
)
def test_a_printer_address_is_an_address_or_a_name(typed, stored):
    assert st.validate("label_printer_host", typed) == stored


@pytest.mark.parametrize(
    "typed",
    [
        "127.0.0.1", "127.1.2.3", "0.0.0.0", "::1", "::", "localhost", "LOCALHOST", "shelf.localhost", "224.0.0.1",
        "http://198.51.100.20", "198.51.100.20:9100", "198.51.100.20/x", "user@198.51.100.20", "a b", "a_b",
        "-bad", "bad-", "a..b", "x" * 254, "[2001:db8::7]", "198.51.100.20%eth0", "üñï ", 5, True, ["a"],
        "a\x00b", "fe80::1%eth0", "169.254.169.254", "169.254.1.1", "fe80::1", "0.1.2.3", "240.0.0.1",
        "255.255.255.255", "::ffff:169.254.1.1",
    ],
)
def test_this_machine_urls_ports_and_junk_are_refused(typed):
    with pytest.raises(ValueError):
        st.validate("label_printer_host", typed)


def test_an_error_does_not_repeat_the_value():
    with pytest.raises(ValueError) as caught:
        st.validate("label_printer_host", "http://secret.example")
    assert "secret" not in str(caught.value)


def test_the_roll_must_be_a_known_one():
    assert st.validate("label_roll", "DK-11209") == "DK-11209"
    for bad in ("DK-0000", "", None, 5, "dk-11209"):
        with pytest.raises(ValueError):
            st.validate("label_roll", bad)


def test_the_details_are_stored_once_each_in_the_fixed_order():
    assert st.validate("label_fields", ["date", "name", "name", "sku"]) == ["name", "sku", "date"]
    assert st.validate("label_fields", []) == []


@pytest.mark.parametrize("bad", ["name", None, 5, ["bogus"], ["name", 5], [["name"]], {"name": True}])
def test_unknown_or_malformed_details_are_refused(bad):
    with pytest.raises(ValueError):
        st.validate("label_fields", bad)


def test_the_values_survive_a_round_trip(session):
    st.set_setting(session, "label_printer_host", "198.51.100.20")
    st.set_setting(session, "label_fields", ["buffer", "name"])
    assert st.get_setting(session, "label_printer_host") == "198.51.100.20"
    assert st.get_setting(session, "label_fields") == ["name", "buffer"]


def test_nothing_of_it_is_sensitive():
    assert not {"label_printer_host", "label_roll", "label_fields"} & st.SENSITIVE
