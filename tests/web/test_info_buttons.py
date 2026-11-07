"""Info buttons: a small button behind the label of a field opens an explanation, pinned under it.

The button is a <details>, so it works without JavaScript. It never sits inside a <label> that wraps
a field or a checkbox (a click on it must not focus the field or tick the box); the label and the
button are siblings in a wrapper instead.
"""

import html as htmllib
import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from conftest import sm_of
from factories import make_article, make_part
from reprintbuddy import __version__
from reprintbuddy import settings as st
from reprintbuddy.web import i18n
from reprintbuddy.web.i18n import translate

CSS = "/static/css/reprintbuddy.css"
FORM = "/articles/new/file/1/plate/1"
STATIC = Path(i18n.__file__).parent / "static"

# The agreed texts, key -> (en, de), word for word.
TEXTS = {
    "info.units_per_print": (
        "How many units one print of this plate yields, usually 1. Reprintbuddy uses it to work out how many prints a reprint needs.",
        "Wie viele Einheiten ein Druck dieser Platte liefert, meist 1. Danach rechnet Reprintbuddy aus, wie viele Drucke ein Nachdruck braucht.",
    ),
    "info.target": (
        "Where Bambuddy sends the print: to a printer model, so any free printer of that model prints it, or to one particular printer.",
        "Wohin Bambuddy den Druck schickt: an ein Druckermodell, dann druckt jeder freie Drucker dieses Modells, oder an einen bestimmten Drucker.",
    ),
    "info.min_stock": (
        "A reprint is due when the available units (stock minus reserved) plus everything already planned, printing, awaiting inspection or awaiting assembly fall below this number. Minimum stock 0 and buffer 0: never by itself.",
        "Nachgedruckt wird, wenn die verfügbaren Stück (Bestand minus Reserviertes) plus alles, was schon geplant, im Druck, zu prüfen oder zu montieren ist, unter diese Zahl fallen. Mindestbestand 0 und Puffer 0: nie von selbst.",
    ),
    "info.buffer": (
        "A reprint fills up not just to the minimum stock but to minimum stock plus buffer.",
        "Ein Nachdruck füllt nicht nur bis zum Mindestbestand auf, sondern bis Mindestbestand plus Puffer.",
    ),
    "info.needs_assembly": (
        "Good units from inspection first wait for assembly and only count as stock after “Book assembled”; the bought parts are deducted then.",
        "Gute Einheiten aus der Prüfung warten erst auf die Montage und zählen erst nach der Buchung „Montiert buchen“ zum Bestand; dabei werden die Zukaufteile abgezogen.",
    ),
    "info.auto_enabled": (
        "On: Reprintbuddy plans reprints of this article by itself as soon as it drops below the minimum stock, and passes them to Bambuddy bit by bit until minimum stock plus buffer is reached. Only works while the main automation switch is on too; off: Reprintbuddy only shows that a reprint is due.",
        "An: Reprintbuddy plant für diesen Artikel selbst Nachdrucke, sobald der Mindestbestand unterschritten ist, und gibt sie nach und nach an Bambuddy weiter, bis Mindestbestand plus Puffer erreicht sind. Wirkt nur, wenn auch der Hauptschalter Automatik an ist; aus: Reprintbuddy zeigt nur an, dass nachgedruckt werden sollte.",
    ),
    "info.has_parts": (
        "Enter which bought parts go into this article and how many per unit. They are taken off the parts stock when a unit becomes stock: at inspection “good”, with manual work only at assembly, and when booking in with “Deduct parts”.",
        "Hier trägst du ein, welche gekauften Teile in diesem Artikel stecken und wie viele je Einheit. Sie werden vom Bestand abgezogen, sobald eine Einheit Bestand wird: bei der Prüfung „gut“, bei Handarbeit erst bei der Montage, und beim Einbuchen mit „Zukaufteile abziehen“.",
    ),
    "info.part_min_stock": (
        "Below this stock the part shows as “below minimum”.",
        "Liegt der Bestand darunter, steht das Teil auf „unter Minimum“.",
    ),
    "info.pack_size": (
        "How many pieces come in one pack. Order suggestions always count whole packs.",
        "So viele Stück kommen in einer Packung. Bestellvorschläge rechnen immer in ganzen Packungen.",
    ),
    "info.min_order_packs": (
        "The fewest packs one order may have.",
        "So viele Packungen muss eine Bestellung mindestens haben.",
    ),
    "info.lead_time_days": (
        "Days from ordering to arrival. Order suggestions include what is used during this time.",
        "Tage von der Bestellung bis zum Eintreffen. Bestellvorschläge rechnen den Verbrauch in dieser Zeit mit ein.",
    ),
    "info.safety_value": (
        "Reserve that should still be left after the lead time: as a quantity or as so many days of usage (unit next to it). If it would get tighter, Reprintbuddy suggests an order.",
        "Reserve, die nach der Lieferzeit noch übrig sein soll: als Stückzahl oder als Verbrauch so vieler Tage (Einheit daneben). Würde es knapper, schlägt Reprintbuddy eine Bestellung vor.",
    ),
    "info.consume_parts": (
        "Takes the bought parts this article needs off the parts stock for every unit booked in. Untick it when the parts were already deducted.",
        "Zieht die Zukaufteile, die dieser Artikel braucht, für jede eingebuchte Einheit vom Zukaufteil-Bestand ab. Abwählen, wenn die Teile schon abgezogen sind.",
    ),
    "info.host": (
        "Network address Reprintbuddy listens on. 0.0.0.0 means: on all networks of the machine. Takes effect after a restart.",
        "Netzwerkadresse, auf der Reprintbuddy erreichbar ist. 0.0.0.0 heißt: auf allen Netzwerken des Rechners. Wird nach einem Neustart wirksam.",
    ),
}
ARTICLE_KEYS = {
    "info.units_per_print", "info.target", "info.min_stock", "info.buffer",
    "info.needs_assembly", "info.auto_enabled", "info.has_parts",
}
PART_KEYS = {
    "info.part_min_stock", "info.pack_size", "info.min_order_packs", "info.lead_time_days",
    "info.safety_value",
}


def en(key, **params):
    return translate("en", key, **params)


def render(client, source, **context):
    env = client.app.state.templates.env
    head = '{% import "_ui.html" as ui with context %}'
    values = {"lang": "en", "csrf_token": "CSRF", **context}
    return env.from_string(head + source).render(**values)


def set_language(client, language):
    with sm_of(client).begin() as s:
        st.set_setting(s, "language", language, commit=False)


def pops(page):
    """The explanation texts on a page, unescaped, in page order."""
    found = re.findall(r'<div class="info-pop" role="note">(.*?)</div>', page, re.S)
    return [htmllib.unescape(text) for text in found]


def expected(keys, index):
    return {TEXTS[key][index] for key in keys}


def check_page(page, keys, index):
    found = pops(page)
    assert len(found) == len(set(found)) == len(keys), found  # one per field, none twice
    assert set(found) == expected(keys, index)


def check_structure(page):
    """No info button inside a <label>; every <label for> points to a field of the page."""
    for label in re.findall(r"<label\b.*?</label>", page, re.S):
        assert 'class="info"' not in label, label
    for target in re.findall(r'<label for="([^"]+)"', page):
        assert re.search(rf'<(?:input|select|textarea)[^>]*\bid="{re.escape(target)}"', page), target
    for summary in re.findall(r'<details class="info">\s*<summary[^>]*>', page):
        assert re.search(r'aria-label="[^"]+"', summary)


def article_page(client, path):
    with sm_of(client).begin() as s:
        make_part(s, name="Insert M4")
    return client.get(path).text


# ---- locales --------------------------------------------------------------------------------


def test_all_texts_exist_in_both_locales_word_for_word():
    locales = i18n.load_locales()
    for key, (english, german) in TEXTS.items():
        assert locales["en"][key] == english, key
        assert locales["de"][key] == german, key
    assert locales["en"]["info.about"] == "Explanation of {label}"
    assert locales["de"]["info.about"] == "Erklärung zu {label}"


@pytest.mark.parametrize("name", ["en", "de"])
def test_the_new_block_has_no_duplicate_keys(name):
    path = Path(i18n.__file__).parent.parent / "locales" / f"{name}.json"

    def reject_duplicates(pairs):
        keys = [key for key, _ in pairs]
        assert len(keys) == len(set(keys)), sorted({k for k in keys if keys.count(k) > 1})
        return dict(pairs)

    json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates)


def test_the_quoted_button_label_is_the_real_label_of_the_booking_box():
    # The explanation of "has_parts" names the checkbox of the booking form; the quote must match.
    locales = i18n.load_locales()
    assert f"“{locales['en']['bookings.consume_parts']}”" in locales["en"]["info.has_parts"]
    assert f"„{locales['de']['bookings.consume_parts']}“" in locales["de"]["info.has_parts"]


# ---- the macro ------------------------------------------------------------------------------


def test_macro_renders_a_details_with_summary_and_a_note(ready_client):
    page = render(ready_client, '{{ ui.info("Some <b>text</b>", "Units per print") }}')
    assert re.search(r'<details class="info">\s*<summary', page)
    assert 'aria-label="Explanation of Units per print"' in page
    assert 'title="Explanation of Units per print"' in page
    assert '<div class="info-pop" role="note">Some &lt;b&gt;text&lt;/b&gt;</div>' in page
    assert "<b>" not in page
    assert '#info"' in page  # the symbol of the sprite


def test_macro_name_follows_the_language(ready_client):
    page = render(ready_client, '{{ ui.info("Text", "Stück je Druck") }}', lang="de")
    assert 'aria-label="Erklärung zu Stück je Druck"' in page


def test_macro_escapes_the_label(ready_client):
    page = render(ready_client, '{{ ui.info("Text", "A <i>b</i>") }}')
    assert "<i>" not in page
    assert "Explanation of A &lt;i&gt;b&lt;/i&gt;" in page


def test_field_macro_puts_label_and_button_side_by_side(ready_client):
    page = render(
        ready_client,
        '{% call ui.field("f-x", "Buffer", "Why") %}<input id="f-x" name="x">{% endcall %}',
    )
    assert '<div class="field">' in page
    assert re.search(r'<div class="label-row"><label for="f-x">Buffer</label>\s*<details class="info">', page)
    assert '<input id="f-x" name="x">' in page
    check_structure(page)


def test_check_row_macro_keeps_the_label_and_puts_the_button_after_it(ready_client):
    page = render(
        ready_client,
        '{% call ui.check_row("Why", "Automation") %}<label class="check"><input name="a" type="checkbox"><span>Automation</span></label>{% endcall %}',
    )
    assert re.search(r'<div class="check-row"><label class="check">.*?</label>\s*<details class="info">', page, re.S)
    check_structure(page)


def test_setting_row_with_info_puts_the_button_next_to_the_label_not_in_it(ready_client):
    page = render(
        ready_client,
        '{% call ui.setting_row("Port", "Hint", "port", "Why") %}<input id="port">{% endcall %}',
    )
    assert re.search(r'<div class="label-row"><label for="port">Port</label>\s*<details class="info">', page)
    assert "Explanation of Port" in page and ">Why</div>" in page
    check_structure(page)


def test_setting_row_without_info_is_unchanged(ready_client):
    page = render(ready_client, '{% call ui.setting_row("Port", "Hint", "port") %}<input id="port">{% endcall %}')
    assert '<div class="setting-text"><label for="port">Port</label><span>Hint</span></div>' in page
    assert "info" not in page


# ---- the pages ------------------------------------------------------------------------------


def test_new_article_form_has_its_info_buttons(bb_client):
    page = article_page(bb_client, FORM)
    check_page(page, ARTICLE_KEYS, 0)
    check_structure(page)


def test_new_article_form_in_german(bb_client):
    set_language(bb_client, "de")
    page = article_page(bb_client, FORM)
    check_page(page, ARTICLE_KEYS, 1)
    assert 'aria-label="Erklärung zu Stück je Druck"' in page
    assert 'aria-label="Erklärung zu Automatik"' in page


def test_edit_article_form_has_its_info_buttons(ready_client):
    with sm_of(ready_client).begin() as s:
        article_id = make_article(s, name="Template A").id
    page = article_page(ready_client, f"/articles/{article_id}/edit")
    check_page(page, ARTICLE_KEYS, 0)
    check_structure(page)


def test_edit_article_form_in_german(ready_client):
    set_language(ready_client, "de")
    with sm_of(ready_client).begin() as s:
        article_id = make_article(s, name="Template A").id
    check_page(article_page(ready_client, f"/articles/{article_id}/edit"), ARTICLE_KEYS, 1)


def test_article_form_without_parts_in_use_has_no_parts_info(bb_client):
    page = bb_client.get(FORM).text  # no part exists: the parts block is not shown
    check_page(page, ARTICLE_KEYS - {"info.has_parts"}, 0)


def test_article_fields_keep_their_names_and_ids_for_the_script(bb_client):
    page = article_page(bb_client, FORM)
    for name in ("name", "sku", "units_per_print", "min_stock", "buffer", "needs_assembly", "auto_enabled", "has_parts", "target"):
        assert re.search(rf'<(?:input|select)[^>]*\bname="{name}"', page), name
    assert 'id="article-parts"' in page and "data-parts-add" in page and "data-parts-rows" in page
    for name in ("units_per_print", "min_stock", "buffer", "target"):
        field = re.search(rf'<(?:input|select)[^>]*\bname="{name}"[^>]*>', page).group(0)
        assert re.search(r'\bid="[^"]+"', field), name


@pytest.mark.parametrize("language,index", [("en", 0), ("de", 1)])
def test_part_form_new_and_edit_have_their_info_buttons(ready_client, language, index):
    set_language(ready_client, language)
    with sm_of(ready_client).begin() as s:
        part_id = make_part(s, name="Insert M4").id
    for path in ("/parts/new", f"/parts/{part_id}/edit"):
        page = ready_client.get(path).text
        check_page(page, PART_KEYS, index)
        check_structure(page)
        for name in ("name", "unit", "min_stock", "pack_size", "min_order_packs", "lead_time_days", "safety_value", "safety_unit", "supplier", "order_url"):
            assert re.search(rf'<(?:input|select)[^>]*\bname="{name}"', page), (path, name)


@pytest.mark.parametrize("language,index", [("en", 0), ("de", 1)])
def test_bookings_has_the_info_button_of_the_deduct_parts_box(ready_client, language, index):
    set_language(ready_client, language)
    page = ready_client.get("/bookings").text
    check_page(page, {"info.consume_parts"}, index)
    check_structure(page)
    assert re.search(r'<input name="consume_parts" type="checkbox"', page)


@pytest.mark.parametrize("language,index", [("en", 0), ("de", 1)])
def test_settings_general_has_the_info_button_of_the_listen_address(ready_client, language, index):
    set_language(ready_client, language)
    page = ready_client.get("/settings").text
    check_page(page, {"info.host"}, index)
    check_structure(page)
    assert 'id="host" name="host"' in page


def test_other_settings_tabs_and_forms_have_no_info_buttons(ready_client):
    for path in ("/settings?tab=account", "/settings?tab=bambuddy", "/sets/new", "/orders/new", "/articles"):
        assert "info-pop" not in ready_client.get(path).text, path


# ---- script, symbol, style sheet ------------------------------------------------------------


def test_base_loads_the_script_deferred_with_the_version(ready_client):
    page = ready_client.get("/").text
    assert f'<script src="/static/js/info.js?v={__version__}" defer></script>' in page
    response = ready_client.get(f"/static/js/info.js?v={__version__}")
    assert response.status_code == 200


def test_script_closes_on_escape_and_outside_taps_and_keeps_one_open():
    source = (STATIC / "js" / "info.js").read_text(encoding="utf-8")
    assert "Escape" in source and "details.info" in source
    assert "toggle" in source  # opening one closes the others
    for forbidden in ("innerHTML", "eval(", "document.write"):
        assert forbidden not in source


def test_script_is_valid_javascript():
    try:
        done = subprocess.run(
            ["node", "--check", str(STATIC / "js" / "info.js")], capture_output=True, text=True, timeout=30
        )
    except FileNotFoundError:
        pytest.skip("node is not installed")
    assert done.returncode == 0, done.stderr


def test_sprite_has_the_info_symbol(ready_client):
    sprite = ready_client.get(f"/static/icons/icons.svg?v={__version__}").text
    assert re.search(r'<symbol id="info" viewBox="0 0 24 24">', sprite)


def rules_of(css, selector):
    return " ".join(re.findall(r"(?:^|\n)" + re.escape(selector) + r" \{([^}]*)\}", css))


def test_css_button_has_a_44px_target(ready_client):
    css = ready_client.get(CSS).text
    rule = rules_of(css, ".info summary")
    assert "min-width: 2.75rem" in rule and "min-height: 2.75rem" in rule


def test_css_popup_is_pinned_under_the_button_and_inside_the_screen(ready_client):
    css = ready_client.get(CSS).text
    pop = rules_of(css, ".info-pop")
    assert "position: absolute" in pop
    assert "18rem" in pop and "100vw" in pop  # width limit, never wider than the screen
    assert "z-index" in pop
    # The row of label and button is the frame of the popup (not the card): in a second column the
    # popup stays at its own field.
    assert "position: relative" in rules_of(css, ".label-row")
    assert "position: relative" in rules_of(css, ".check-row")
    assert "list-style: none" in rules_of(css, ".info summary")  # no disclosure triangle


def test_css_popup_width_rule_is_the_agreed_one(ready_client):
    pop = rules_of(ready_client.get(CSS).text, ".info-pop")
    assert "width: min(18rem, max(100%, 14rem))" in pop  # as wide as its field, but readable
    assert "max-width: calc(100vw - 2rem)" in pop
    # It starts at the field's own left edge unless the script moves it under the button; never at
    # the card's edge (that was the bug: a popup of the right column opened under the left column).
    assert "left: var(--info-x, 0px)" in pop
    assert "1.25rem" not in pop


def _rem(rule, prop):
    return float(re.search(rf"(?:^|[ ;]){prop}: (-?[0-9.]+)rem", rule).group(1))


def test_css_button_hit_area_does_not_reach_into_the_label(ready_client):
    css = ready_client.get(CSS).text
    summary = rules_of(css, ".info summary")
    reach = -float(re.search(r"margin: (-?[0-9.]+)rem (-?[0-9.]+)rem;", summary).group(2))  # sideways, in rem
    assert reach > 0
    # The gap between label and button is at least as wide as the hit area reaches to the label.
    for selector in (".label-row", ".check-row"):
        assert _rem(rules_of(css, selector), "gap") >= reach, selector


MACROS = re.compile(r"ui\.(?:info|field|check_row)\(|ui\.setting_row\([^%]*?,[^%]*?,[^%]*?,\s*t\('info\.")
CARD = re.compile(r'class="card[ "]')


def test_info_buttons_are_only_used_in_templates_that_sit_in_a_card():
    """The popup is framed by `.card`: every template that uses the macros has a card itself or is
    included only by templates that have one (a pragmatic check of the text, not of the DOM)."""
    templates = {p.name: p.read_text(encoding="utf-8") for p in (STATIC.parent / "templates").glob("*.html")}
    users = [name for name, text in templates.items() if name != "_ui.html" and MACROS.search(text)]
    assert {"_article_fields.html", "_article_parts.html", "part_form.html", "bookings.html", "settings.html"} <= set(users)
    for name in users:
        if CARD.search(templates[name]):
            continue
        includers = [
            other for other, text in templates.items()
            if re.search(rf'{{% include "{re.escape(name)}" %}}', text)
        ]
        assert includers, f"{name} uses info buttons outside of a card"
        for other in includers:
            if not CARD.search(templates[other]):
                # one more level: the partial is included by a partial that a card page includes
                outer = [o for o, t in templates.items() if re.search(rf'{{% include "{re.escape(other)}" %}}', t)]
                assert outer and all(CARD.search(templates[o]) for o in outer), (name, other)


def test_css_rows_keep_the_switch_look_and_the_label_rule(ready_client):
    css = ready_client.get(CSS).text
    assert "label.check" in css and 'label.check input[type="checkbox"]' in css
    assert "display: flex" in rules_of(css, ".check-row")
    assert "display: flex" in rules_of(css, ".label-row")


# ---- the popup opens under its own button, in either column --------------------------------

class _Ancestors(HTMLParser):
    """For every <details class="info">: the classes of the elements it sits in."""

    VOID = {"input", "br", "img", "meta", "link", "hr", "path", "use"}

    def __init__(self):
        super().__init__()
        self.stack, self.found = [], []

    def handle_starttag(self, tag, attrs):
        classes = (dict(attrs).get("class") or "").split()
        if tag == "details" and "info" in classes:
            self.found.append([c for _, cs in self.stack for c in cs])
        if tag not in self.VOID:
            self.stack.append((tag, classes))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


def _frames_of(page):
    parser = _Ancestors()
    parser.feed(page)
    return parser.found


@pytest.mark.parametrize(
    "path",
    ["/articles/{article}/edit", "/parts/{part}/edit", "/parts/new", "/bookings", "/settings"],
)
def test_every_info_button_sits_in_a_row_that_frames_its_popup(ready_client, path):
    """Sister paths of the bug: the field of a second column, the switch row, the settings row, the
    booking box and the part form all put the button in `.label-row` or `.check-row`."""
    with ready_client.app.state.sessionmaker() as session:
        article = make_article(session)
        part = make_part(session)
        session.commit()
        ids = {"article": article.id, "part": part.id}
    frames = _frames_of(ready_client.get(path.format(**ids)).text)
    assert frames, path
    for classes in frames:
        assert "label-row" in classes or "check-row" in classes, (path, classes)


NODE_CASES = [
    # (button left, frame left, frame width, popup width, viewport) -> expected x
    ((420, 400, 500, 288, 1280), 8),     # under the button (button left minus the 12 px edge)
    ((986, 700, 500, 288, 1280), 212),   # button at the far end of the field: right-aligned to the field
    ((400, 400, 500, 288, 1280), 0),     # never left of the field
    ((100, 50, 200, 288, 1280), 0),      # popup wider than the field: starts at the field
    ((300, 40, 295, 288, 375), 7),       # phone: the field is the card's content, nothing to move
    ((700, 300, 500, 288, 700), 104),    # the screen's edge wins over the field's edge
]


def test_script_places_the_popup_under_its_button_and_inside_the_field_and_the_screen():
    try:
        done = subprocess.run(
            [
                "node",
                "-e",
                "const p=require(process.argv[1]).place;"
                "console.log(JSON.stringify(JSON.parse(process.argv[2]).map(a=>p.apply(null,a))))",
                str(STATIC / "js" / "info.js"),
                json.dumps([list(args) for args, _ in NODE_CASES]),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        pytest.skip("node is not installed")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [expected for _, expected in NODE_CASES]


def test_script_sets_the_offset_when_an_explanation_opens_and_when_the_window_changes():
    source = (STATIC / "js" / "info.js").read_text(encoding="utf-8")
    assert "--info-x" in source
    assert '"resize"' in source
