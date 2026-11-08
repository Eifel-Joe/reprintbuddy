import json
import re
import string
from pathlib import Path

import pytest

from reprintbuddy import i18n as core_i18n
from reprintbuddy.web import i18n

TEMPLATES = Path(i18n.__file__).parent / "templates"


def test_same_keys():
    locales = i18n.load_locales()
    assert set(locales["en"]) == set(locales["de"])


# p57: the old check of this kind (test_file_choice) looked only at the last two keys of each file, so a
# new text in the middle slipped through and every new key broke it. These tests read the raw files, so
# a key written twice (which `json.loads` silently collapses) is seen too, and look at every key.
LOCALE_DIR = Path(core_i18n.__file__).parent / "locales"
LANGUAGES = ("de", "en")


def _no_duplicates(pairs):
    keys = [key for key, _ in pairs]
    repeated = sorted({key for key in keys if keys.count(key) > 1})
    assert not repeated, f"keys written twice: {repeated}"
    return dict(pairs)


def _raw(lang):
    text = (LOCALE_DIR / f"{lang}.json").read_text(encoding="utf-8")
    return json.loads(text, object_pairs_hook=_no_duplicates)


def _placeholders(text):
    return sorted(name for _, name, _, _ in string.Formatter().parse(text) if name)


@pytest.mark.parametrize("lang", LANGUAGES)
def test_locale_file_has_no_key_twice(lang):
    _raw(lang)


def test_locale_files_have_the_same_keys():
    de, en = _raw("de"), _raw("en")
    assert not set(en) - set(de), f"only in en.json: {sorted(set(en) - set(de))}"
    assert not set(de) - set(en), f"only in de.json: {sorted(set(de) - set(en))}"


@pytest.mark.parametrize("lang", LANGUAGES)
def test_every_text_is_a_filled_string(lang):
    bad = [key for key, value in _raw(lang).items() if not isinstance(value, str) or not value.strip()]
    assert not bad, f"empty or not text in {lang}.json: {bad}"


def test_every_text_has_the_same_placeholders_in_both_languages():
    de, en = _raw("de"), _raw("en")
    differing = {key: (_placeholders(en[key]), _placeholders(de[key])) for key in en if key in de}
    differing = {key: pair for key, pair in differing.items() if pair[0] != pair[1]}
    assert not differing, f"placeholders differ (en, de): {differing}"


def test_translate_de():
    assert i18n.translate("de", "nav.overview") == "Übersicht"


def test_translate_en():
    assert i18n.translate("en", "nav.overview") == "Overview"


def test_fallback_to_en(monkeypatch):
    monkeypatch.setattr(
        core_i18n,
        "load_locales",
        lambda: {"en": {"only.en": "English"}, "de": {}},
    )
    assert i18n.translate("de", "only.en") == "English"


def test_missing_raises(monkeypatch):
    monkeypatch.setattr(core_i18n, "load_locales", lambda: {"en": {}, "de": {}})
    with pytest.raises(KeyError):
        i18n.translate("en", "does.not.exist")


def test_params(monkeypatch):
    monkeypatch.setattr(
        core_i18n, "load_locales", lambda: {"en": {"hello": "Hello {name}"}, "de": {}}
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


def test_dynamic_message_keys_exist():
    """Keys built at runtime (t(error), reason codes) are invisible to the template scan."""
    from typing import get_args

    from reprintbuddy.bambuddy.connection import Reason

    en = i18n.load_locales()["en"]
    de = i18n.load_locales()["de"]
    routes = Path(i18n.__file__).parent / "routes"
    literal = re.compile(r"""["']((?:setup|login|bambuddy|settings)\.[a-z_.]+)["']""")
    used = {
        m.group(1)
        for p in routes.glob("*.py")
        for m in literal.finditer(p.read_text(encoding="utf-8"))
        if not m.group(1).endswith(".html")  # template file names, not message keys
    }
    used |= {f"bambuddy.reason.{reason}" for reason in get_args(Reason) if reason != "ok"}
    from reprintbuddy.bambuddy.connection import REQUIRED_PERMISSIONS

    used |= {f"permission.{code}" for code in REQUIRED_PERMISSIONS}  # built in the templates
    used |= {f"permission.{code}" for code in ("api_keys:read", "api_keys:create", "settings:read")}
    used |= {"keyright.name", "keyright.status", "keyright.queue"}
    used |= {
        f"error.{kind}.{part}"
        for kind in ("forbidden", "not_found", "host", "generic")
        for part in ("title", "message")
    } | {"error.back"}
    assert used, "scan found nothing: the pattern is wrong"
    assert used <= set(en), f"missing in en: {sorted(used - set(en))}"
    assert used <= set(de), f"missing in de: {sorted(used - set(de))}"


def test_german_restart_hint_is_in_german():
    de = i18n.load_locales()["de"]["settings.restart_needed"]
    assert "Neustart" in de
    assert "restart" not in de.lower()
