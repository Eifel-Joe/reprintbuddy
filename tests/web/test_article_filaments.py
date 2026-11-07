"""The filament mapping page of an article (Spec 5.2, B6 Entscheidung 15)."""

import re

from conftest import csrf_of, sm_of
from factories import make_article
from reprintbuddy import settings as st
from reprintbuddy.bambuddy.client import BambuddyRejected
from reprintbuddy.models import ArticleFilament, FilamentSort, utcnow


def make(client, *, archived=False):
    with sm_of(client).begin() as s:
        art = make_article(s, name="Template A")
        s.add(ArticleFilament(article_id=art.id, slot=1, grams=42.5, material="PLA", color_hex="#000000"))
        if archived:
            art.archived_at = utcnow()
        return art.id


def slot_one(client, article_id):
    with sm_of(client).begin() as s:
        row = s.query(ArticleFilament).filter_by(article_id=article_id, slot=1).one()
        return row.spool_filament_id


def sorts_by_label(client):
    with sm_of(client).begin() as s:
        return {x.label: x.id for x in s.query(FilamentSort)}


def options(html, name="slot_1"):
    block = re.search(rf'<select[^>]*name="{name}"[^>]*>(.*?)</select>', html, re.S).group(1)
    return [
        (v, bool(sel), label)
        for v, sel, label in re.findall(r'<option value="([^"]*)"( selected)?>([^<]*)</option>', block)
    ]


def post(client, a, page_path=None, **fields):
    data = {"csrf_token": csrf_of(client, page_path or f"/articles/{a}/filaments")}
    data.update(fields)
    return client.post(f"/articles/{a}/filaments", data=data, follow_redirects=False)


def first_sort_id(client, a):
    return options(client.get(f"/articles/{a}/filaments").text)[1][0]


def test_form_preselects_suggestion(bb_client, fake_bambuddy):  # Spec 5.2
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    fake_bambuddy.add_spool("PETG", color_hex="#000000", color_name="Black")
    a = make(bb_client)
    page = bb_client.get(f"/articles/{a}/filaments")
    assert page.status_code == 200
    by = sorts_by_label(bb_client)
    opts = options(page.text)
    assert opts[0][0] == "" and len(opts) == 3
    assert [(v, sel) for v, sel, _ in opts if sel] == [(str(by["Example Brand PLA Black"]), True)]
    assert 'class="suggested ' in page.text
    assert slot_one(bb_client, a) is None  # nothing is stored until the user saves


def test_save_mapping(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    a = make(bb_client)
    sort_id = first_sort_id(bb_client, a)
    r = post(bb_client, a, slot_1=sort_id)
    assert r.status_code == 303 and r.headers["location"] == f"/articles/{a}"
    assert slot_one(bb_client, a) == int(sort_id)
    detail = bb_client.get(f"/articles/{a}").text
    assert "Example Brand PLA Black" in detail
    assert f"/articles/{a}/filaments" in detail
    assert "Filament assignment saved." in detail


def test_ambiguous_no_preselection(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    fake_bambuddy.add_spool("PLA", brand="Other Brand", color_hex="#000000", color_name="Black")
    a = make(bb_client)
    page = bb_client.get(f"/articles/{a}/filaments")
    opts = options(page.text)
    assert len(opts) == 3
    assert [v for v, sel, _ in opts if sel] == [""]
    assert 'class="suggested ' not in page.text


def test_clear_mapping(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    a = make(bb_client)
    sort_id = first_sort_id(bb_client, a)
    assert post(bb_client, a, slot_1=sort_id).status_code == 303
    page = bb_client.get(f"/articles/{a}/filaments")
    assert [sel for _, sel, _ in options(page.text)] == [False, True]
    assert 'class="suggested ' not in page.text  # stored, so not merely suggested
    assert post(bb_client, a, slot_1="").status_code == 303
    assert slot_one(bb_client, a) is None


def test_invalid_value(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    a = make(bb_client)
    sort_id = first_sort_id(bb_client, a)
    assert post(bb_client, a, slot_1=sort_id).status_code == 303
    for bad in ("abc", "9" * 20, "999", "-1", " 1"):
        r = post(bb_client, a, slot_1=bad)
        assert r.status_code == 200, bad
        assert 'role="alert"' in r.text, bad
        assert slot_one(bb_client, a) == int(sort_id), bad
    assert "This filament sort does not exist." in post(bb_client, a, slot_1="999").text
    assert "Choose a filament sort from the list." in post(bb_client, a, slot_1="abc").text


def test_unknown_slot_field(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    a = make(bb_client)
    r = post(bb_client, a, slot_7="")
    assert r.status_code == 200 and "Unknown filament slot" in r.text


def test_unavailable_shows_saved_only(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    a = make(bb_client)
    sort_id = first_sort_id(bb_client, a)
    assert post(bb_client, a, slot_1=sort_id).status_code == 303
    fake_bambuddy.unavailable = True  # and there is no record of any day
    page = bb_client.get(f"/articles/{a}/filaments")
    assert page.status_code == 200
    assert "not known" in page.text
    assert [(v, sel) for v, sel, _ in options(page.text)] == [("", False), (sort_id, True)]
    assert post(bb_client, a, slot_1=sort_id).status_code == 303  # saving loses nothing
    assert slot_one(bb_client, a) == int(sort_id)


def test_no_connection_is_not_a_500(ready_client):
    a = make(ready_client)
    page = ready_client.get(f"/articles/{a}/filaments")
    assert page.status_code == 200
    assert [o[0] for o in options(page.text)] == [""]


def test_archived_article_is_read_only(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    a = make(bb_client, archived=True)
    page = bb_client.get(f"/articles/{a}/filaments")
    assert page.status_code == 200
    assert "archived article" in page.text
    assert 'type="submit">Save' not in page.text
    with sm_of(bb_client).begin() as s:
        sort_id = s.query(FilamentSort).one().id
    r = post(bb_client, a, page_path=f"/articles/{a}", slot_1=str(sort_id))
    assert r.status_code == 200 and "archived" in r.text
    assert slot_one(bb_client, a) is None
    assert f"/articles/{a}/filaments" not in bb_client.get(f"/articles/{a}").text


def test_unknown_article(bb_client):
    assert bb_client.get("/articles/999/filaments").status_code == 404


def test_detail_has_sort_column_and_link(bb_client):
    a = make(bb_client)
    detail = bb_client.get(f"/articles/{a}").text
    assert "Filament sort" in detail and f"/articles/{a}/filaments" in detail


def test_filaments_german(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    a = make(bb_client)
    with sm_of(bb_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    assert "Vorschlag" in bb_client.get(f"/articles/{a}/filaments").text
    assert "Filamentsorte" in bb_client.get(f"/articles/{a}").text


def test_select_sits_in_its_own_full_width_row(bb_client, fake_bambuddy):  # phone: no sideways scrolling
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    a = make(bb_client)
    html = bb_client.get(f"/articles/{a}/filaments").text
    assert re.search(r'<tr class="slot-select">\s*<td colspan="4">\s*<select[^>]*name="slot_1"', html)
    assert len(re.findall(r"<th[ >]", html)) == 4


def test_rejected_spool_list_is_not_a_500(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    a = make(bb_client)
    fake_bambuddy.fail_next("list_spools", BambuddyRejected("forbidden", status=403))
    page = bb_client.get(f"/articles/{a}/filaments")
    assert page.status_code == 200 and "not known" in page.text


def test_successful_save_does_not_ask_bambuddy(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    a = make(bb_client)
    sort_id = first_sort_id(bb_client, a)
    token = csrf_of(bb_client, f"/articles/{a}/filaments")
    fake_bambuddy.unavailable = True
    fake_bambuddy.calls.clear()
    r = bb_client.post(f"/articles/{a}/filaments", data={"csrf_token": token, "slot_1": sort_id}, follow_redirects=False)
    assert r.status_code == 303 and slot_one(bb_client, a) == int(sort_id)
    assert "list_spools" not in fake_bambuddy.calls


# ---- the look of v0.3.3 (page head, card head, table card, info button) --------------------------

from html import unescape  # noqa: E402

from reprintbuddy.web import regions  # noqa: E402
from reprintbuddy.web.i18n import translate  # noqa: E402


def head_of(page):
    return re.search(r'<header class="page-head">.*?</header>', page, re.S).group(0)


def test_head_has_package_symbol_article_name_and_sub(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    a = make(bb_client)
    head = head_of(bb_client.get(f"/articles/{a}/filaments").text)
    assert "#package" in head and "<h1>Template A</h1>" in head
    assert f'<p class="page-sub">{translate("en", "filament.map_sub")}</p>' in head


def test_title_sits_in_a_card_head_and_no_loose_heading_is_left(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    a = make(bb_client)
    page = bb_client.get(f"/articles/{a}/filaments").text
    assert re.search(r'<div class="card-head"><h2>.*?Filament assignment</h2>', page, re.S)
    assert len(re.findall(r"<h2\b", page)) == 1  # the one inside the card head
    assert "section wide" not in page and "<section" not in page


def test_table_is_a_table_card_inside_the_form(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    a = make(bb_client)
    page = bb_client.get(f"/articles/{a}/filaments").text
    form = re.search(rf'<form method="post" action="/articles/{a}/filaments".*?</form>', page, re.S).group(0)
    assert '<div class="card table-wrap table-card">' in form
    assert 'class="live-stale"' in form and 'name="csrf_token"' in form
    assert 'name="slot_1"' in form
    assert re.search(r'<a class="btn secondary" href="/articles/\d+">Cancel</a>', form)


def test_mapping_has_its_info_button(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    a = make(bb_client)
    page = bb_client.get(f"/articles/{a}/filaments").text
    pops = [unescape(p) for p in re.findall(r'<div class="info-pop" role="note">(.*?)</div>', page, re.S)]
    assert pops == [translate("en", "info.filament_mapping")]
    assert 'aria-label="Explanation of Filament assignment"' in page
    for label in re.findall(r"<label\b.*?</label>", page, re.S):
        assert 'class="info"' not in label


def test_look_in_german(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    a = make(bb_client)
    with sm_of(bb_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    page = bb_client.get(f"/articles/{a}/filaments").text
    assert translate("de", "filament.map_sub") in head_of(page)
    assert unescape(translate("de", "info.filament_mapping")) in unescape(page)


def test_read_only_page_keeps_the_look_without_a_save_button(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    a = make(bb_client, archived=True)
    page = bb_client.get(f"/articles/{a}/filaments").text
    assert '<div class="card table-wrap table-card">' in page and "disabled" in page
    assert 'type="submit">Save' not in page


def test_form_region_holds_whole_cards_only(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    a = make(bb_client)
    piece = bb_client.get(f"/articles/{a}/filaments", headers={regions.HEADER: "form"}).text
    assert piece.count("<table") == 1 and "card-head" in piece
