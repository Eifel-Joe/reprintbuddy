"""Labels in the web interface (Spec 8.5): the settings tab "Labels" with preview and trial print, and
the card "Label" on the article's page with preview, number and print. The printer is never reached:
`label_support.Recorder` stands in for the network."""

import io
import re

import pytest
import zxingcpp
from conftest import csrf_of, form_token_of, sm_of
from factories import make_article
from label_support import Recorder, block
from PIL import Image

from reprintbuddy import settings as st
from reprintbuddy.labels import fields
from reprintbuddy.labels.errors import PrinterUnreachable, SendAborted
from reprintbuddy.models import Article
from reprintbuddy.web import netaddr
from reprintbuddy.web.i18n import translate

HOST = "198.51.100.20"
SAME_PAGE = "/settings?tab=labels"


def en(key, **params):
    return translate("en", key, **params)


def de(key, **params):
    return translate("de", key, **params)


@pytest.fixture
def net(monkeypatch):
    return Recorder(monkeypatch)


@pytest.fixture
def configured(ready_client):
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "label_printer_host", HOST, commit=False)
    return ready_client


@pytest.fixture
def article(ready_client):
    with sm_of(ready_client).begin() as s:
        return make_article(s, name="Template A", sku="TA-100", min_stock=4, buffer=2, target_model="A1").id


def stored(client, key):
    with sm_of(client)() as s:
        return st.get_setting(s, key)


def png_of(response):
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    return Image.open(io.BytesIO(response.content))


def scanned(image):
    return [c.text for c in zxingcpp.read_barcodes(image) if c.format == zxingcpp.BarcodeFormat.QRCode]


def message_on_next_page(client, path="/"):
    return client.get(path).text


def post_settings(client, **over):
    data = {"csrf_token": csrf_of(client, SAME_PAGE), "label_printer_host": HOST, "label_roll": "DK-11209",
            "label_field": ["name", "sku"]}
    data.update(over)
    return client.post("/settings/labels", data=data)


# ============================== the settings tab ==============================


def test_the_tab_exists_and_shows_printer_roll_and_every_detail(ready_client):
    page = ready_client.get(SAME_PAGE)
    assert page.status_code == 200
    assert f'href="{SAME_PAGE}"' in ready_client.get("/settings").text  # in the row of tabs
    html = page.text
    assert 'name="label_printer_host"' in html
    assert re.search(r'<select[^>]*name="label_roll"', html) and "DK-11209" in html
    for key in fields.FIELDS:
        assert re.search(rf'<input[^>]*name="label_field"[^>]*value="{key}"', html), key
    assert "Raster" in html  # the one thing to set at the printer
    assert f'src="/settings/labels/preview.png' in html
    assert 'formaction="/settings/labels/test"' in html


def test_the_saved_choice_is_ticked_and_the_others_are_not(ready_client):
    html = ready_client.get(SAME_PAGE).text
    for key in fields.DEFAULT_FIELDS:
        assert re.search(rf'<input[^>]*name="label_field"[^>]*value="{key}"[^>]*checked', html), key
    assert not re.search(r'<input[^>]*name="label_field"[^>]*value="date"[^>]*checked', html)


def test_saving_stores_the_three_settings_in_the_fixed_order(ready_client):
    done = post_settings(ready_client, label_field=["date", "name"])
    assert done.status_code == 303 and done.headers["location"] == SAME_PAGE
    assert stored(ready_client, "label_printer_host") == HOST
    assert stored(ready_client, "label_roll") == "DK-11209"
    assert stored(ready_client, "label_fields") == ["name", "date"]
    assert 'value="198.51.100.20"' in ready_client.get(SAME_PAGE).text


def test_nothing_ticked_means_only_the_code(ready_client):
    data = {"csrf_token": csrf_of(ready_client, SAME_PAGE), "label_printer_host": HOST, "label_roll": "DK-11209"}
    assert ready_client.post("/settings/labels", data=data).status_code == 303
    assert stored(ready_client, "label_fields") == []


def test_an_empty_address_removes_the_printer(configured):
    post_settings(configured, label_printer_host="")
    assert stored(configured, "label_printer_host") is None


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "http://198.51.100.20", "198.51.100.20:9100", "a b"])
def test_a_wrong_address_is_refused_and_what_was_typed_stays(configured, host):
    response = post_settings(configured, label_printer_host=host, label_field=["buffer"])
    assert response.status_code == 200
    assert en("settings.invalid_label_host") in response.text
    assert stored(configured, "label_printer_host") == HOST  # nothing was saved
    assert stored(configured, "label_fields") == ["name", "sku", "label_text"]
    assert re.search(r'name="label_field"[^>]*value="buffer"[^>]*checked', response.text)


def test_an_unknown_roll_or_detail_is_refused(ready_client):
    assert post_settings(ready_client, label_roll="DK-0").status_code == 200
    assert post_settings(ready_client, label_field=["bogus"]).status_code == 200
    assert stored(ready_client, "label_printer_host") is None


def test_saving_needs_the_csrf_token(ready_client):
    assert ready_client.post("/settings/labels", data={"label_printer_host": HOST}).status_code == 403
    assert ready_client.post("/settings/labels/test", data={"label_printer_host": HOST}).status_code == 403


# ---- the preview picture of the settings ----


def test_the_preview_is_the_picture_of_a_label_with_a_code_that_scans(ready_client, monkeypatch):
    monkeypatch.setattr(netaddr, "machine_address", lambda: "203.0.113.64")
    response = ready_client.get("/settings/labels/preview.png")
    image = png_of(response)
    assert image.size == (696, 271) and image.mode == "1"
    assert response.headers["cache-control"] == "no-store"
    assert [t.startswith("http://203.0.113.64:") and t.endswith("/a/1") for t in scanned(image)] == [True]


def test_the_preview_follows_the_ticked_details_and_without_any_it_has_only_the_code(ready_client):
    saved = ready_client.get("/settings/labels/preview.png").content
    one = ready_client.get("/settings/labels/preview.png?fields=name").content
    none = ready_client.get("/settings/labels/preview.png?fields=").content
    assert len({saved, one, none}) == 3
    assert png_of(ready_client.get("/settings/labels/preview.png?fields=bogus,name")).size == (696, 271)
    assert ready_client.get("/settings/labels/preview.png?fields=bogus,name").content == one


def test_the_preview_says_which_lines_did_not_fit(ready_client):
    every = ready_client.get("/settings/labels/preview.png?fields=" + ",".join(fields.FIELDS))
    assert "x-label-cut" in every.headers  # present, empty or a list of indexes


def test_the_preview_needs_a_signed_in_user(ready_client):
    ready_client.cookies.clear()
    assert ready_client.get("/settings/labels/preview.png").status_code == 303


# ---- the trial print ----


def trial(client, **over):
    """The trial print as the form sends it: with the one-time token of the settings form."""
    data = {"csrf_token": csrf_of(client, SAME_PAGE), "form_token": form_token_of(client, SAME_PAGE, "/settings/labels"),
            "label_printer_host": HOST, "label_roll": "DK-11209", "label_field": ["name"]}
    data.update(over)
    return client.post("/settings/labels/test", data=data), data


def test_the_trial_print_sends_one_job_with_what_is_ticked_and_saves_nothing(configured, net):
    done, _ = trial(configured)
    assert done.status_code == 200  # the page stays, with what was typed and the message
    assert len(net.sent) == 1 and len(net.sent[0][1]) == 25642
    assert net.sent[0][0] == HOST
    assert stored(configured, "label_fields") == ["name", "sku", "label_text"]  # not saved
    assert en("labels.sent_one") in done.text


def test_what_was_typed_and_ticked_stays_on_the_page_after_a_trial_print(ready_client, net):
    done, _ = trial(ready_client, label_printer_host="198.51.100.77", label_field=["buffer", "date"])
    assert 'value="198.51.100.77"' in done.text
    assert re.search(r'name="label_field"[^>]*value="buffer"[^>]*checked', done.text)
    assert not re.search(r'name="label_field"[^>]*value="name"[^>]*checked', done.text)
    assert stored(ready_client, "label_printer_host") is None


def test_the_same_trial_print_twice_prints_once(configured, net):
    done, data = trial(configured)
    again = configured.post("/settings/labels/test", data=data)
    assert len(net.sent) == 1
    assert en("form.duplicate") in again.text


def test_a_trial_print_without_a_valid_token_prints_nothing(configured, net):
    done, _ = trial(configured, form_token="forged")
    assert net.sent == []
    assert en("form.expired") in done.text


def test_the_trial_print_goes_to_the_typed_address_even_when_not_yet_saved(ready_client, net):
    trial(ready_client, label_printer_host="198.51.100.77")
    assert net.sent[0][0] == "198.51.100.77"
    assert stored(ready_client, "label_printer_host") is None


def test_the_trial_print_without_an_address_says_so(ready_client, net):
    done, _ = trial(ready_client, label_printer_host="")
    assert net.sent == [] and net.asked == []
    assert en("labels.not_configured") in done.text


def test_the_trial_print_with_a_wrong_address_says_so(ready_client, net):
    done, _ = trial(ready_client, label_printer_host="127.0.0.1")
    assert done.status_code == 200 and en("settings.invalid_label_host") in done.text
    assert net.sent == []


def test_the_trial_print_tells_why_it_did_not_print(configured, monkeypatch):
    net = Recorder(monkeypatch, answer=block(b9=0x10))
    done, _ = trial(configured)
    assert net.sent == [] and en("labels.problem.cover_open") in done.text


def test_the_drawing_of_a_label_never_runs_on_the_event_loop(configured, article, monkeypatch):
    """Drawing takes tens to hundreds of milliseconds: every route that draws does it in a worker."""
    import asyncio

    from reprintbuddy.labels import render

    seen = []
    real = render.render_label

    def spy(*args, **kwargs):
        try:
            asyncio.get_running_loop()
            seen.append("on the loop")
        except RuntimeError:
            seen.append("worker")
        return real(*args, **kwargs)

    Recorder(monkeypatch)
    # the pages that carry the forms are fetched first: a settings page draws its small sample itself
    article_form = {"csrf_token": csrf_of(configured, f"/articles/{article}"),
                    "form_token": form_token_of(configured, f"/articles/{article}", f"/articles/{article}/label"),
                    "copies": "1"}
    trial_form = {"csrf_token": csrf_of(configured, SAME_PAGE),
                  "form_token": form_token_of(configured, SAME_PAGE, "/settings/labels"),
                  "label_printer_host": HOST, "label_roll": "DK-11209", "label_field": ["name"]}
    monkeypatch.setattr(render, "render_label", spy)
    configured.get(f"/articles/{article}/label.png")
    configured.get("/settings/labels/preview.png")
    configured.post(f"/articles/{article}/label", data=article_form)
    configured.post("/settings/labels/test", data=trial_form)
    assert len(seen) == 4 and set(seen) == {"worker"}, seen


def test_the_article_page_itself_draws_no_label(configured, article, monkeypatch):
    """Only the picture's own request draws it; the page is built without drawing."""
    from reprintbuddy.labels import render

    calls = []
    real = render.render_label
    monkeypatch.setattr(render, "render_label", lambda *a, **k: calls.append(1) or real(*a, **k))
    assert configured.get(f"/articles/{article}").status_code == 200
    assert calls == []


# ============================== the article's page ==============================


def test_the_card_shows_preview_number_and_the_print_button_with_a_printer(configured, article):
    html = configured.get(f"/articles/{article}").text
    assert en("labels.title") in html
    assert f'src="/articles/{article}/label.png"' in html
    assert re.search(r'<input[^>]*name="copies"[^>]*value="1"', html)
    assert f'action="/articles/{article}/label"' in html and en("labels.print") in html
    assert en("labels.keep_address") in html  # p115: a printed code keeps its address


def test_without_a_printer_the_card_points_to_the_settings_instead_of_a_button(ready_client, article):
    html = ready_client.get(f"/articles/{article}").text
    assert f'action="/articles/{article}/label"' not in html
    assert en("labels.not_configured") in html and 'href="/settings?tab=labels"' in html


def test_the_card_is_in_german_too(configured, article):
    with sm_of(configured).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    html = configured.get(f"/articles/{article}").text
    assert de("labels.title") in html and de("labels.print") in html and de("labels.keep_address") in html


def test_the_article_preview_carries_the_articles_own_address(configured, article, monkeypatch):
    monkeypatch.setattr(netaddr, "machine_address", lambda: "203.0.113.64")
    image = png_of(configured.get(f"/articles/{article}/label.png"))
    assert image.size == (696, 271)
    assert [t.endswith(f"/a/{article}") and "203.0.113.64" in t for t in scanned(image)] == [True]


def test_the_article_preview_shows_the_chosen_details(configured, article):
    only_name = configured.get(f"/articles/{article}/label.png").content
    with sm_of(configured).begin() as s:
        st.set_setting(s, "label_fields", ["name", "min_stock", "buffer", "target_qty"], commit=False)
    assert configured.get(f"/articles/{article}/label.png").content != only_name


def test_the_article_preview_of_a_missing_article_is_404(configured):
    assert configured.get("/articles/999/label.png").status_code == 404


def test_label_text_and_the_target_quantity_come_from_the_article(configured, article):
    with sm_of(configured).begin() as s:
        s.get(Article, article).label_text = "Shelf B3"
    a = configured.get(f"/articles/{article}/label.png").content
    with sm_of(configured).begin() as s:
        s.get(Article, article).label_text = "Shelf C9"
    assert configured.get(f"/articles/{article}/label.png").content != a


# ---- printing ----


def print_form(client, article, **over):
    path = f"/articles/{article}"
    data = {"csrf_token": csrf_of(client, path), "form_token": form_token_of(client, path, f"/articles/{article}/label"),
            "copies": "1"}
    data.update(over)
    return client.post(f"/articles/{article}/label", data=data)


def test_one_label_is_sent_after_the_status_question(configured, article, net):
    done = print_form(configured, article)
    assert done.status_code == 303 and done.headers["location"] == f"/articles/{article}"
    assert len(net.asked) == 1 and len(net.sent) == 1 and net.sent[0][0] == HOST
    assert en("labels.sent_one") in configured.get(f"/articles/{article}").text


def test_several_labels_are_one_job_each_after_one_question(configured, article, net):
    print_form(configured, article, copies="3")
    assert len(net.sent) == 3 and len(net.asked) == 1
    assert en("labels.sent_many", count=3) in configured.get(f"/articles/{article}").text


def test_the_job_is_the_picture_of_the_articles_label(configured, article, net):
    from reprintbuddy.labels import brother
    from reprintbuddy.labels.rolls import DK_11209

    print_form(configured, article)
    shown = png_of(configured.get(f"/articles/{article}/label.png"))
    assert net.sent[0][1] == brother.build_job(shown, DK_11209)


@pytest.mark.parametrize("copies", ["0", "100", "-1", "abc", "", "1.5", "2 3"])
def test_a_wrong_number_prints_nothing_and_says_so(configured, article, net, copies):
    print_form(configured, article, copies=copies)
    assert net.sent == [] and net.asked == []
    assert en("labels.copies_invalid", max=99) in configured.get(f"/articles/{article}").text


def test_the_same_form_twice_prints_once(configured, article, net):
    path = f"/articles/{article}"
    data = {"csrf_token": csrf_of(configured, path), "form_token": form_token_of(configured, path, f"{path}/label"),
            "copies": "2"}
    configured.post(f"{path}/label", data=data)
    configured.post(f"{path}/label", data=data)
    assert len(net.sent) == 2
    assert en("form.duplicate") in configured.get(path).text


def test_without_a_printer_nothing_is_sent(ready_client, article, net):
    data = {"csrf_token": csrf_of(ready_client, f"/articles/{article}"), "copies": "1"}  # no form is shown: no token
    ready_client.post(f"/articles/{article}/label", data=data)
    assert net.sent == [] and net.asked == []
    assert en("labels.not_configured") in ready_client.get(f"/articles/{article}").text


def test_an_unknown_article_is_404_and_nothing_is_sent(configured, net):
    data = {"csrf_token": csrf_of(configured, "/settings"), "copies": "1"}
    assert configured.post("/articles/999/label", data=data).status_code == 404
    assert net.sent == []


def test_printing_needs_the_csrf_token(configured, article, net):
    assert configured.post(f"/articles/{article}/label", data={"copies": "1"}).status_code == 403
    assert net.sent == []


def test_an_archived_article_can_still_have_its_label_printed(configured, article, net):
    from datetime import UTC, datetime

    with sm_of(configured).begin() as s:
        s.get(Article, article).archived_at = datetime.now(UTC)
    print_form(configured, article)
    assert len(net.sent) == 1


@pytest.mark.parametrize(
    "answer,text",
    [
        (block(b9=0x10), "labels.problem.cover_open"),
        (block(b8=0x02), "labels.problem.roll_empty"),
        (block(b10=0, b11=0, b17=0, b8=0x01), "labels.problem.no_roll"),
        (block(b8=0x04), "labels.problem.cutter"),
        (block(b9=0x80), "labels.problem.error"),
    ],
)
def test_a_printer_that_is_not_ready_gets_nothing_and_the_reason_is_shown(configured, article, monkeypatch, answer, text):
    net = Recorder(monkeypatch, answer=answer)
    print_form(configured, article, copies="2")
    assert net.sent == []
    page = configured.get(f"/articles/{article}").text
    assert en(text) in page and 'class="alert"' in page  # shown as trouble


def test_another_roll_names_both_rolls(configured, article, monkeypatch):
    net = Recorder(monkeypatch, answer=block(b17=100))
    print_form(configured, article)
    page = configured.get(f"/articles/{article}").text
    assert net.sent == []
    assert en("labels.problem.wrong_roll", inserted="62 × 100 mm", expected="62 × 29 mm") in page.replace("&times;", "×")


def test_a_continuous_roll_is_named_as_such(configured, article, monkeypatch):
    Recorder(monkeypatch, answer=block(b11=0x4A, b17=0))
    print_form(configured, article)
    assert "62 mm" in configured.get(f"/articles/{article}").text


def test_no_answer_from_the_printer_prints_anyway(configured, article, monkeypatch):
    net = Recorder(monkeypatch, answer=None)
    print_form(configured, article)
    assert len(net.sent) == 1


def test_a_failed_series_says_how_far_it_got_and_stops(configured, article, monkeypatch):
    net = Recorder(monkeypatch, outcomes=[None, SendAborted()])
    print_form(configured, article, copies="5")
    assert len(net.sent) == 2
    page = configured.get(f"/articles/{article}").text
    assert en("labels.partial", printed=1, requested=5, reason=en("labels.err.aborted")) in page


def test_an_unreachable_printer_is_reported(configured, article, monkeypatch):
    Recorder(monkeypatch, outcomes=[PrinterUnreachable()])
    print_form(configured, article)
    assert en("labels.failed", reason=en("labels.err.unreachable")) in configured.get(f"/articles/{article}").text


def test_the_database_is_not_held_while_the_printer_works(configured, article, net):
    """The long part (the network) must not hold a write lock: another request can write meanwhile."""
    def another_writer():
        with sm_of(configured).begin() as s:
            s.get(Article, article).label_text = "written meanwhile"

    net.during_send = another_writer
    print_form(configured, article)
    with sm_of(configured)() as s:
        assert s.get(Article, article).label_text == "written meanwhile"


def test_the_printer_address_comes_from_the_settings_never_from_the_request(configured, article, net):
    print_form(configured, article, label_printer_host="203.0.113.9", host="203.0.113.9", printer="203.0.113.9")
    assert [host for host, *_ in net.sent] == [HOST]


def test_the_printing_runs_off_the_event_loop(configured, article, monkeypatch):
    import asyncio
    import threading

    seen = []
    from reprintbuddy.labels import service

    real = service.print_labels

    def spy(*args, **kwargs):
        try:
            asyncio.get_running_loop()
            seen.append("on the loop")
        except RuntimeError:
            seen.append("worker thread")
        return real(*args, **kwargs)

    monkeypatch.setattr(service, "print_labels", spy)
    Recorder(monkeypatch)
    print_form(configured, article)
    assert seen == ["worker thread"] and threading.current_thread() is threading.main_thread()


# ---- p115: the hint that printed codes keep their address ----


def test_the_hint_stands_at_the_setting_and_at_the_printing_in_both_languages(configured, article):
    for lang, tr in (("en", en), ("de", de)):
        with sm_of(configured).begin() as s:
            st.set_setting(s, "language", lang, commit=False)
        assert tr("labels.keep_address") in configured.get(f"/articles/{article}").text
        general = configured.get("/settings?tab=general").text
        assert tr("settings.qr_keep_address") in general
        assert tr("labels.keep_address") in configured.get(SAME_PAGE).text


# ---- the printer is another device: not this server either (Spec 8.5) ----


@pytest.fixture
def own_address(monkeypatch):
    monkeypatch.setattr(netaddr, "machine_address", lambda: "203.0.113.64")
    return "203.0.113.64"


def test_the_servers_own_address_is_refused_in_the_settings(ready_client, own_address):
    response = post_settings(ready_client, label_printer_host=own_address)
    assert response.status_code == 200 and en("settings.invalid_label_host") in response.text
    assert stored(ready_client, "label_printer_host") is None


def test_the_servers_own_address_is_refused_by_the_trial_print(ready_client, own_address, net):
    done, _ = trial(ready_client, label_printer_host=own_address)
    assert done.status_code == 200 and en("settings.invalid_label_host") in done.text
    assert net.sent == [] and net.asked == []


def test_link_local_is_refused_in_the_settings(ready_client):
    for host in ("169.254.169.254", "fe80::1"):
        assert post_settings(ready_client, label_printer_host=host).status_code == 200
    assert stored(ready_client, "label_printer_host") is None


def test_an_own_address_stored_earlier_is_still_not_printed_to(ready_client, article, own_address, net):
    """The settings may hold it from before the server moved: the print checks again."""
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "label_printer_host", own_address, commit=False)
    print_form(ready_client, article)
    assert net.sent == [] and net.asked == []
    page = ready_client.get(f"/articles/{article}").text
    assert en("labels.failed", reason=en("labels.err.address")) in page


# ============================== notes under the preview (p118) ==============================

LONG_BASE = "https://" + "a" * 60 + ".example.org:8443"  # an address that makes the code too fine


def notes_of(response):
    return [token for token in response.headers["x-label-notes"].split(",") if token]


def test_an_ordinary_preview_has_no_note(configured, article):
    assert notes_of(configured.get(f"/articles/{article}/label.png")) == []
    assert notes_of(configured.get("/settings/labels/preview.png")) == []


def test_a_character_the_font_lacks_is_a_note_on_the_article_preview(configured, ready_client):
    with sm_of(ready_client).begin() as s:
        odd = make_article(s, name="Plate 中", sku="TA-200").id
    assert notes_of(configured.get(f"/articles/{odd}/label.png")) == ["missing"]


def test_right_to_left_text_is_a_note_on_the_article_preview(configured, ready_client):
    with sm_of(ready_client).begin() as s:
        odd = make_article(s, name="שלום", sku="TA-201").id
    assert notes_of(configured.get(f"/articles/{odd}/label.png")) == ["rtl"]


def test_a_very_long_qr_address_is_a_note_on_both_previews(configured, article):
    with sm_of(configured).begin() as s:
        st.set_setting(s, "qr_base_url", LONG_BASE, commit=False)
    assert notes_of(configured.get("/settings/labels/preview.png")) == ["fine"]
    assert notes_of(configured.get(f"/articles/{article}/label.png")) == ["fine"]


def test_a_cut_line_is_in_the_notes_too(configured, ready_client):
    with sm_of(ready_client).begin() as s:
        long = make_article(s, name="N" * 300, sku="TA-202").id
    response = configured.get(f"/articles/{long}/label.png")
    assert response.headers["x-label-cut"] == "0"
    assert notes_of(response) == ["cut"]


def test_all_nine_details_are_a_rows_note_on_both_previews_and_the_last_one_is_not_drawn(configured, ready_client):
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "label_fields", list(fields.FIELDS), commit=False)
        full = make_article(s, name="Template A", sku="TA-300", min_stock=4, buffer=2, target_model="A1",
                            label_text="Shelf B3").id
    for response in (configured.get(f"/articles/{full}/label.png"), configured.get("/settings/labels/preview.png")):
        assert notes_of(response) == ["rows"]
        assert response.headers["x-label-dropped"] == "date"  # the keys, so the page can name them
        top = Image.open(io.BytesIO(response.content)).convert("L").crop((260, 0, 696, 8))
        assert top.getextrema()[0] == 255  # the margin above the text stays white


def test_a_label_that_fits_has_no_rows_note(configured, ready_client):
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "label_fields", list(fields.FIELDS[:8]), commit=False)
    assert notes_of(configured.get("/settings/labels/preview.png")) == []
    assert configured.get("/settings/labels/preview.png").headers["x-label-dropped"] == ""


def test_the_notes_are_on_the_settings_tab_and_the_article_card_hidden_until_the_script_shows_them(configured, article):
    for page in (configured.get(SAME_PAGE).text, configured.get(f"/articles/{article}").text):
        for token, key in (("cut", "labels.cut_warning"), ("missing", "labels.note_missing"),
                           ("rtl", "labels.note_rtl"), ("fine", "labels.note_fine"),
                           ("rows", "labels.note_rows")):
            match = re.search(rf'<p[^>]*data-note="{token}"[^>]*>', page)
            assert match and " hidden" in match.group(0), token
            assert en(key) in page
        assert f'src="/static/js/labels.js?v=' in page


def test_the_article_preview_tells_the_script_where_to_ask_for_the_notes(configured, article):
    page = configured.get(f"/articles/{article}").text
    assert f'data-label-url="/articles/{article}/label.png"' in page


def _preview_img(page):
    match = re.search(r'<img[^>]*id="label-preview"[^>]*>', re.sub(r"<noscript>.*?</noscript>", "", page, flags=re.S))
    assert match, "no preview image outside <noscript>"
    return match.group(0)


def _noscript_img(page):
    match = re.search(r"<noscript>\s*(<img[^>]*>)\s*</noscript>", page, flags=re.S)
    assert match, "no <noscript><img>"
    return match.group(1)


def test_the_article_preview_is_asked_for_once_by_the_script_not_also_by_the_browser(configured, article):
    """p127: an <img src> would have the picture drawn once for the browser and once more for the script
    that only wants the header X-Label-Notes. The script asks once and gives the picture to the page."""
    page = configured.get(f"/articles/{article}").text
    assert " src=" not in _preview_img(page)
    assert f'data-label-url="/articles/{article}/label.png"' in _preview_img(page)
    assert f'src="/articles/{article}/label.png"' in _noscript_img(page)  # without a script the picture is there as before
    assert 'alt="' in _noscript_img(page) and 'width="696"' in _noscript_img(page)


def test_the_scripted_picture_is_hidden_until_it_has_its_image_so_no_empty_box_shows(configured, article):
    """Without a script the <noscript> picture is the only one; with one the box is shown when the
    picture is there (an img without src would be an empty bordered box with its alt text)."""
    for page in (configured.get(SAME_PAGE).text, configured.get(f"/articles/{article}").text):
        assert " hidden" in _preview_img(page)
    script = configured.get("/static/js/labels.js").text
    assert script.count('removeAttribute("hidden")') >= 1
    assert ".label-preview[hidden]" in configured.get("/static/css/reprintbuddy.css").text


def test_the_settings_preview_is_asked_for_once_by_the_script_not_also_by_the_browser(configured):
    page = configured.get(SAME_PAGE).text
    assert " src=" not in _preview_img(page)
    assert "/settings/labels/preview.png?fields=name,sku,label_text" in _noscript_img(page)


def test_the_script_reads_the_notes_header(configured):
    script = configured.get("/static/js/labels.js")
    assert script.status_code == 200 and "X-Label-Notes" in script.text


def test_the_rows_note_has_the_name_of_every_detail_so_the_script_can_say_which_are_missing(configured, article):
    for page in (configured.get(SAME_PAGE).text, configured.get(f"/articles/{article}").text):
        for key in fields.FIELDS:
            assert re.search(rf'<span hidden data-detail="{key}">{re.escape(en("labels.field." + key))}</span>', page), key
        assert "data-dropped" in page
    assert "X-Label-Dropped" in configured.get("/static/js/labels.js").text


def test_the_notes_are_in_german_too():
    for key in ("labels.note_missing", "labels.note_rtl", "labels.note_fine", "labels.note_rows"):
        assert de(key) != key and de(key) != en(key)
