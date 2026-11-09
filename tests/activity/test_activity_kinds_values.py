"""The fixed list of events and the cleaning of values of the activity log (Spec 10.7).

Every setting is either shown with a label or internal; every event and every field has its text in
both languages; an address keeps only scheme, host and port.
"""

from __future__ import annotations

import pytest

from reprintbuddy import settings as st
from reprintbuddy.activity import kinds, values
from reprintbuddy.i18n import load_locales


def test_every_setting_is_shown_or_internal():
    """A new setting fails here until it is put into one of the two lists (Spec 10.7: "interne
    Werte, die keine Einstellungsseite zeigt, nicht")."""
    known = set(st.DEFAULTS) | {"session_secret"}
    shown, internal = set(values.SETTINGS), set(values.INTERNAL)
    assert not shown & internal
    assert known == shown | internal


def test_the_sensitive_settings_are_shown_as_secret_only():
    for key in st.SENSITIVE:
        assert values.SETTINGS[key] == "secret", key
    assert "session_secret" in values.INTERNAL


def test_every_area_kind_and_actor_has_a_text_in_both_languages():
    locales = load_locales()
    needed = (
        [f"activity.area.{area}" for area in kinds.AREAS]
        + [f"activity.kind.{kind}" for kind in kinds.KINDS]
        + [f"activity.actor.{actor}" for actor in kinds.ACTORS]
        + [f"activity.setting.{name}" for name in values.NAMES.values()]
        + [f"activity.field.{field}" for field in set(values.FIELDS.values())]
    )
    for lang in ("en", "de"):
        missing = [key for key in needed if key not in locales[lang]]
        assert not missing, (lang, missing)


def test_every_kind_belongs_to_a_known_area():
    assert set(kinds.KINDS.values()) <= set(kinds.AREAS)


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("http://bambuddy.local:8000", "http://bambuddy.local:8000"),
        ("https://user:secret@bambuddy.local:8000/api?key=abc#x", "https://bambuddy.local:8000"),
        ("http://10.0.0.5", "http://10.0.0.5"),
        ("http://[fd00::1]:8123/ha", "http://[fd00::1]:8123"),
        ("not an address", None),
        (None, None),
    ],
)
def test_an_address_keeps_scheme_host_and_port(raw, clean):
    assert values.clean_address(raw) == clean


def test_shown_change_by_kind_of_value():
    show = values.shown_change
    assert show("poll_minutes", 15, 10) == {"setting": "poll_minutes", "old": 15, "new": 10}
    assert show("automation", True, False) == {"setting": "automation", "old": True, "new": False}
    # The page asks "stop the queue", the database stores "queue running": shown as on the page.
    assert show("queue_running", True, False) == {"setting": "queue_running", "old": False, "new": True}
    assert show("language", "en", "de") == {"setting": "language", "old": "en", "new": "de"}
    assert show("bambuddy_url", None, "http://u:p@b.local:8000/x?k=1") == {
        "setting": "bambuddy_url", "old": None, "new": "http://b.local:8000"
    }
    assert show("bambuddy_api_key", "old-key", "new-key") == {"setting": "bambuddy_key", "change": "changed"}
    assert show("ha_token", "tok", None) == {"setting": "ha_access_token", "change": "removed"}
    assert show("tls_cert", None, "-----BEGIN CERTIFICATE-----") == {"setting": "https_certificate", "change": "uploaded"}
    assert show("tls_cert", "pem", None) == {"setting": "https_certificate", "change": "removed"}
    assert show("ha_events", ["a"], ["a", "b"]) == {"setting": "ha_events", "change": "changed"}
    assert show("label_roll", "62x29", "62x100") == {"setting": "label_roll", "change": "changed"}
    assert show("label_printer_host", None, "printer.local") == {
        "setting": "label_printer_host", "old": None, "new": "printer.local"
    }


def test_no_secret_value_ever_leaves_shown_change():
    marker = "MARKER-SECRET-123"
    for key in values.SETTINGS:
        for old, new in ((None, marker), (marker, None), (marker, marker + "x")):
            shown = values.shown_change(key, old, new)
            if values.SETTINGS[key] in ("secret", "certificate", "list", "text"):
                assert marker not in repr(shown), key
