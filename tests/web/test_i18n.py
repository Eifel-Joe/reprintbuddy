import re
from pathlib import Path

import pytest

from lagerbuddy.web import i18n

TEMPLATES = Path(i18n.__file__).parent / "templates"


def test_same_keys():
    locales = i18n.load_locales()
    assert set(locales["en"]) == set(locales["de"])


def test_translate_de():
    assert i18n.translate("de", "nav.overview") == "Übersicht"


def test_translate_en():
    assert i18n.translate("en", "nav.overview") == "Overview"


def test_fallback_to_en(monkeypatch):
    monkeypatch.setattr(
        i18n,
        "load_locales",
        lambda: {"en": {"only.en": "English"}, "de": {}},
    )
    assert i18n.translate("de", "only.en") == "English"


def test_missing_raises(monkeypatch):
    monkeypatch.setattr(i18n, "load_locales", lambda: {"en": {}, "de": {}})
    with pytest.raises(KeyError):
        i18n.translate("en", "does.not.exist")


def test_params(monkeypatch):
    monkeypatch.setattr(
        i18n, "load_locales", lambda: {"en": {"hello": "Hello {name}"}, "de": {}}
    )
    assert i18n.translate("en", "hello", name="Anna") == "Hello Anna"


def test_navigation_texts():
    expected = {
        "nav.overview": ("Overview", "Übersicht"),
        "nav.articles": ("Articles", "Artikel"),
        "nav.sets": ("Sets", "Sets"),
        "nav.orders": ("Orders", "Bestellungen"),
        "nav.bookings": ("Bookings", "Buchen"),
        "nav.inspection": ("To check", "Zu prüfen"),
        "nav.assembly": ("Assembly", "Montage"),
        "nav.queue": ("Queue", "Warteschlange"),
        "nav.filament": ("Filament", "Filament"),
        "nav.parts": ("Parts", "Zukaufteile"),
        "nav.reorder": ("Reorder", "Bestellvorschläge"),
        "nav.settings": ("Settings", "Einstellungen"),
    }
    for key, (en, de) in expected.items():
        assert i18n.translate("en", key) == en
        assert i18n.translate("de", key) == de
    assert "nav.open_bambuddy" in i18n.load_locales()["en"]
    assert "nav.logout" in i18n.load_locales()["en"]


def test_templates_use_known_keys():
    keys = set(i18n.load_locales()["en"])
    pattern = re.compile(r"""\bt\(\s*(['"])([^'"]+)\1""")
    used = set()
    for path in TEMPLATES.rglob("*.html"):
        used |= {m.group(2) for m in pattern.finditer(path.read_text(encoding="utf-8"))}
    assert used <= keys, f"unknown keys: {sorted(used - keys)}"
