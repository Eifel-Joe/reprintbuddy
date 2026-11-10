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


# v0.3.14 p175, widened in v0.3.15 p201: the German texts speak with "du". One message of the update card
# said "Suchen Sie ...". Capital "Sie" is the polite form wherever a word or a comma comes before it on the
# same line ("Prüfen Sie", "sind Sie", "Wie Sie sehen", ", Sie können"): in the middle of a sentence the
# third person is written "sie". The third person is capital only as the first word of a sentence, and then
# a full stop, a colon, a quotation mark, a list or heading mark or the start of the text comes before it
# ("Sie enthält keinen G-Code."): that is not found. "Ihr...", "Ihnen" are found in every place; a
# sentence that starts with a third-person "Ihr..." (her/their) would trip the check and goes into
# `_NOT_POLITE` (a longer snippet of the text, so the exception holds for that place only). The pattern does not look across a line break (a wrapped sentence start has a full stop
# before it, a wrapped middle is not caught).
# v0.3.16 p251: opening brackets, quotation marks and asterisks between the word and "Sie" do not hide it
# ("(Sie können", "„Sie können", "**Sie können**"): the rule looks at what stands before them. A "Sie" that is
# only named, closed again right behind it ("(Sie)", "„Sie“", "**Sie**"), is not an address. "Ihrerseits",
# "Ihretwegen", "Ihretwillen" and "Ihrethalben" belong to the "Ihr..." words.
_POLITE_SIE = re.compile(
    r"(?<=[A-Za-zäöüÄÖÜß,;]) Sie\b(?!-)"  # one word or a comma before it, on the same line
    # behind opening marks only a "Sie" that is closed again right behind it is a mention (review of p251)
    r"|(?<=[A-Za-zäöüÄÖÜß,;]) [(\[{\"'„“‚‘»«*_]+Sie\b(?![-\"'“”‘’«»)\]}*_])"
    r"|\bIhr(?:e|en|em|er|es|erseits|etwegen|etwillen|ethalben)?\b|\bIhnen\b"
)
_NOT_POLITE: set[str] = set()  # none today; add the snippet of the text (with its surroundings) here when a third-person text trips the check


def _polite_hits(text):
    """The hits of the pattern; one that lies inside a snippet of `_NOT_POLITE` is a third person."""
    hits = []
    for m in _POLITE_SIE.finditer(text):
        around = text[max(0, m.start() - 40):m.end() + 40]
        if not any(snippet in around for snippet in _NOT_POLITE):
            hits.append(m.group(0).strip())
    return hits


def test_german_texts_have_no_polite_sie_form():
    bad = {key: _polite_hits(value) for key, value in _raw("de").items() if _polite_hits(value)}
    assert not bad, f"Sie-Form in de.json (use du): {bad}"


def test_german_docs_and_readme_have_no_polite_sie_form():
    root = Path(__file__).resolve().parents[2]
    files = [root / "README.de.md", root / "CONTRIBUTING.de.md", *(root / "docs").rglob("*.de.md")]
    assert len(files) > 10
    bad = {path.name: _polite_hits(path.read_text(encoding="utf-8")) for path in files}
    bad = {name: hits for name, hits in bad.items() if hits}
    assert not bad, f"Sie-Form in German docs (use du): {bad}"


# v0.3.15 p201: the check must know more forms of the polite address than the first four it was built on.
@pytest.mark.parametrize(
    "text",
    [
        "Ihre Daten bleiben auf dem Server.",  # "Ihr..." at the start of a sentence
        "Alles gespeichert. Ihr Konto ist angelegt.",
        "Das geht nicht. Ihren Wunsch merken wir uns.",
        "Das Ergebnis liegt Ihnen vor.",
        "Hier sind Sie richtig.",  # irregular verb forms: "sind", "waren", "wurden"
        "Wo sind Sie gerade?",
        "Gestern waren Sie hier.",
        "Wie Sie sehen, ist es fertig.",  # a lower-case word before "Sie" is never the start of a sentence
        "Das Update ist da, Sie können es starten.",
        "Das ist, wie Sie wissen, nicht neu.",
        "Prüfen Sie die Datei.",  # the forms the first version knew keep being found
        "Wenn Sie fertig sind, speichern.",
        "Bitte wählen Sie einen Drucker.",
        # v0.3.16 p251: "Sie" right behind a bracket, a quotation mark or an asterisk, and the "Ihr..." words
        # that the first list of endings missed.
        "Das Update (Sie können es starten) ist da.",
        "Es heißt dann „Sie können jetzt speichern“.",
        'Es heißt dann "Sie können jetzt speichern".',
        "Das ist wichtig, **Sie können** es ändern.",
        "Das liegt Ihrerseits vor.",
        "Ihretwegen ist es fertig.",
        "Um Ihretwillen bleibt es so.",
        # review of p251: a closer behind the address ("klicken Sie)") is no mention; only a "Sie" that
        # an opener leads in and a closer follows at once is one.
        "(dann klicken Sie) weiter",
        "Dann klicken Sie)",
        "Bitte speichern Sie“",
        "**Speichern Sie**",
        "'Speichern Sie'",
        "»Speichern Sie«",
        "Klicken Sie]",
    ],
)
def test_the_polite_form_is_found(text):
    assert _polite_hits(text), text


@pytest.mark.parametrize(
    "text",
    [
        "Sie enthält keinen G-Code.",  # "Sie" as the subject at the start of a sentence: she/it/they
        "Die Datei ist leer. Sie wird nicht gedruckt.",
        "Hinweis: Sie liegt im Ordner.",
        "- Sie wird geprüft.",
        "## Sie\n\nDie Datei sagt es selbst.",
        "Die Teile sind da. Sie sind sortiert.",  # "sind Sie" would be polite, "Sie sind" is not
        "Teile: Sie sind sortiert und ihre Farbe stimmt.",  # lower-case "ihre" is third person
        "Die Datei und ihr Name werden angezeigt.",
        "Wir fragen die Teile, ob sie passen; sie passen.",
        "Das Projekt „Sie\" gibt es nicht.",
        # v0.3.16 p251: the new places must not raise false alarms. A bracket, a quote or an asterisk that
        # opens a new sentence has a full stop, a colon or the line start in front; a word that is only
        # named ("Sie") is closed again right behind it.
        "Das Wort (Sie) steht in Klammern.",
        "Das Wort **Sie** steht fett.",
        "Das Wort „Sie“ steht in Anführungszeichen.",
        "Die Datei ist leer. (Sie wird nicht gedruckt.)",
        "Die Datei ist leer. **Sie wird nicht gedruckt.**",
        "Hinweis: „Sie enthält keinen G-Code.“",
        "„Sie enthält keinen G-Code“, steht dort.",
        "(Sie wird geprüft.)",
        "- *Sie* wird geprüft.",
        "Das geschah ihrerseits und ihretwegen.",  # lower case is third person
    ],
)
def test_the_third_person_is_not_taken_for_the_polite_form(text):
    assert not _polite_hits(text), text


def test_a_snippet_in_not_polite_excuses_only_its_own_place(monkeypatch):
    monkeypatch.setitem(globals(), "_NOT_POLITE", {"Ihre Zukaufteile werden"})
    assert not _polite_hits("Artikel ohne Handarbeit. Ihre Zukaufteile werden abgezogen.")
    assert _polite_hits("Ihre Daten bleiben hier.")
