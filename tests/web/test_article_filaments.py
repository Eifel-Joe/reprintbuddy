"""The filament group of each slot of an article (Spec 5.2, 9.1): tick the spool sorts, one is reordered."""

import re
from html import unescape

from conftest import csrf_of, sm_of
from factories import make_article
from reprintbuddy import settings as st
from reprintbuddy.bambuddy.client import BambuddyRejected
from reprintbuddy.models import ArticleFilament, ArticleFilamentSort, FilamentSort, utcnow
from reprintbuddy.web import regions
from reprintbuddy.web.i18n import translate


def make(client, *, archived=False, slots=((1, "PLA", "#000000", 42.5),)):
    with sm_of(client).begin() as s:
        art = make_article(s, name="Template A")
        for slot, material, color, grams in slots:
            s.add(ArticleFilament(article_id=art.id, slot=slot, grams=grams, material=material, color_hex=color))
        if archived:
            art.archived_at = utcnow()
        return art.id


def group(client, article_id, slot=1):
    """(ticked sort ids, reordered sort id) of one slot."""
    with sm_of(client).begin() as s:
        rows = s.query(ArticleFilamentSort).filter_by(article_id=article_id, slot=slot).all()
        return {r.sort_id for r in rows}, next((r.sort_id for r in rows if r.reordered), None)


def sort_id(client, brand, material="PLA", color_hex="#000000"):
    """The id of a spool sort; the page has to be shown once first, which enters the sorts."""
    with sm_of(client).begin() as s:
        return s.query(FilamentSort).filter_by(brand=brand, material=material).filter(
            FilamentSort.color_hex.in_([color_hex, "#1A1A1A"])).one().id


def boxes(html, slot=1):
    """(value, checked) of the tick boxes of one slot."""
    return [
        (v, bool(c))
        for v, c in re.findall(rf'<input type="checkbox"[^>]*name="tick_{slot}" value="(\d+)"( checked)?', html)
    ]


def radios(html, slot=1):
    return [
        (v, bool(c))
        for v, c in re.findall(rf'<input type="radio"[^>]*name="reorder_{slot}" value="(\d+)"( checked)?', html)
    ]


def post(client, a, fields, page_path=None):
    data: dict[str, list[str]] = {"csrf_token": [csrf_of(client, page_path or f"/articles/{a}/filaments")]}
    for key, value in fields:
        data.setdefault(key, []).append(value)
    return client.post(f"/articles/{a}/filaments", data=data, follow_redirects=False)


def two_blacks(fake):
    fake.add_spool("PLA", brand="Brand A", color_hex="#000000", color_name="Black")
    fake.add_spool("PLA", brand="Brand B", color_hex="#1A1A1A", color_name="Schwarz")  # delta E 5.5


def test_only_fitting_spool_sorts_are_offered_nothing_ticked(bb_client, fake_bambuddy):  # Spec 5.2, 9.1
    two_blacks(fake_bambuddy)
    fake_bambuddy.add_spool("PLA", brand="Brand A", color_hex="#333333", color_name="Anthracite")  # 13.4 away
    fake_bambuddy.add_spool("PETG", brand="Brand A", color_hex="#000000", color_name="Black")  # other material
    fake_bambuddy.add_spool("PLA", brand="Brand C", subtype="Matte", color_hex="#000000")  # subtype does not count
    a = make(bb_client)
    page = bb_client.get(f"/articles/{a}/filaments")
    assert page.status_code == 200
    offered = {int(v) for v, _ in boxes(page.text)}
    with sm_of(bb_client).begin() as s:
        fitting = {x.id for x in s.query(FilamentSort).filter(FilamentSort.material == "PLA", FilamentSort.color_hex != "#333333")}
    assert len(fitting) == 3 and offered == fitting
    assert not any(c for _, c in boxes(page.text)) and not any(c for _, c in radios(page.text))
    assert group(bb_client, a) == (set(), None)  # nothing is stored by looking


def test_each_choice_shows_brand_subtype_colour_and_stock(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", brand="Brand C", subtype="Matte", color_hex="#000000", color_name="Black",
                            remaining_g=640.0)
    a = make(bb_client)
    text = unescape(bb_client.get(f"/articles/{a}/filaments").text)
    row = re.search(r'<tr class="choice">.*?</tr>', text, re.S).group(0)
    assert "Brand C" in row and "Matte" in row and "Black" in row and "640 g" in row
    assert 'class="swatch"' in row


def test_one_ticked_sort_is_reordered_by_itself(bb_client, fake_bambuddy):  # Spec 5.2
    two_blacks(fake_bambuddy)
    a = make(bb_client)
    bb_client.get(f"/articles/{a}/filaments")
    first = sort_id(bb_client, "Brand A")
    r = post(bb_client, a, [("slot", "1"), ("tick_1", str(first))])
    assert r.status_code == 303 and r.headers["location"] == f"/articles/{a}"
    assert group(bb_client, a) == ({first}, first)
    page = bb_client.get(f"/articles/{a}/filaments").text
    assert (str(first), True) in boxes(page) and (str(first), True) in radios(page)


def test_several_ticked_need_the_reordered_one(bb_client, fake_bambuddy):
    two_blacks(fake_bambuddy)
    a = make(bb_client)
    bb_client.get(f"/articles/{a}/filaments")
    one, two = sort_id(bb_client, "Brand A"), sort_id(bb_client, "Brand B")
    r = post(bb_client, a, [("slot", "1"), ("tick_1", str(one)), ("tick_1", str(two))])
    assert r.status_code == 200 and 'role="alert"' in r.text
    assert translate("en", "filament.choose_reordered") in unescape(r.text)
    assert {v for v, c in boxes(r.text) if c} == {str(one), str(two)}  # what was typed stays
    assert group(bb_client, a) == (set(), None)
    r = post(bb_client, a, [("slot", "1"), ("tick_1", str(one)), ("tick_1", str(two)), ("reorder_1", str(two))])
    assert r.status_code == 303
    assert group(bb_client, a) == ({one, two}, two)


def test_reordered_must_be_ticked(bb_client, fake_bambuddy):
    two_blacks(fake_bambuddy)
    a = make(bb_client)
    bb_client.get(f"/articles/{a}/filaments")
    one, two = sort_id(bb_client, "Brand A"), sort_id(bb_client, "Brand B")
    r = post(bb_client, a, [("slot", "1"), ("tick_1", str(one)), ("tick_1", str(two)), ("reorder_1", "999")])
    assert r.status_code == 200 and translate("en", "filament.reordered_not_ticked") in unescape(r.text)


def test_untick_everything_clears_the_slot(bb_client, fake_bambuddy):
    two_blacks(fake_bambuddy)
    a = make(bb_client)
    bb_client.get(f"/articles/{a}/filaments")
    one = sort_id(bb_client, "Brand A")
    assert post(bb_client, a, [("slot", "1"), ("tick_1", str(one))]).status_code == 303
    assert post(bb_client, a, [("slot", "1")]).status_code == 303
    assert group(bb_client, a) == (set(), None)


def test_a_sort_that_does_not_fit_is_refused(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PETG", brand="Brand A", color_hex="#000000", color_name="Black")
    a = make(bb_client)
    bb_client.get(f"/articles/{a}/filaments")
    petg = sort_id(bb_client, "Brand A", "PETG")
    r = post(bb_client, a, [("slot", "1"), ("tick_1", str(petg))])
    assert r.status_code == 200 and translate("en", "filament.not_a_candidate") in unescape(r.text)
    assert group(bb_client, a) == (set(), None)


def test_invalid_values(bb_client, fake_bambuddy):
    two_blacks(fake_bambuddy)
    a = make(bb_client)
    bb_client.get(f"/articles/{a}/filaments")
    for fields in (
        [("slot", "1"), ("tick_1", "abc")],
        [("slot", "1"), ("tick_1", "9" * 20)],
        [("slot", "1"), ("tick_1", "999")],
        [("slot", "7")],
        [("slot", "x")],
    ):
        r = post(bb_client, a, fields)
        assert r.status_code in (200, 404) and 'role="alert"' in r.text, fields
    assert "Unknown filament slot" in post(bb_client, a, [("slot", "7")]).text
    assert group(bb_client, a) == (set(), None)


def test_other_slots_stay_when_one_is_saved(bb_client, fake_bambuddy):
    two_blacks(fake_bambuddy)
    fake_bambuddy.add_spool("PLA", brand="Brand A", color_hex="#FFFFFF", color_name="White")
    a = make(bb_client, slots=((1, "PLA", "#000000", 10.0), (2, "PLA", "#FFFFFF", 5.0)))
    bb_client.get(f"/articles/{a}/filaments")
    black, white = sort_id_by_label(bb_client, "Brand A PLA Black"), sort_id_by_label(bb_client, "Brand A PLA White")
    assert post(bb_client, a, [("slot", "1"), ("tick_1", str(black)), ("slot", "2"), ("tick_2", str(white))]).status_code == 303
    assert post(bb_client, a, [("slot", "1")]).status_code == 303
    assert group(bb_client, a, 1) == (set(), None) and group(bb_client, a, 2) == ({white}, white)


def sort_id_by_label(client, label):
    with sm_of(client).begin() as s:
        return s.query(FilamentSort).filter_by(label=label).one().id


def test_slot_without_candidates_says_so(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PETG", brand="Brand A", color_hex="#000000")
    a = make(bb_client)
    page = bb_client.get(f"/articles/{a}/filaments").text
    assert boxes(page) == [] and translate("en", "filament.no_candidates") in unescape(page)


def test_unknown_stock_keeps_the_choices_without_stock(bb_client, fake_bambuddy):
    two_blacks(fake_bambuddy)
    a = make(bb_client)
    bb_client.get(f"/articles/{a}/filaments")  # enters the spool sorts
    one = sort_id(bb_client, "Brand A")
    assert post(bb_client, a, [("slot", "1"), ("tick_1", str(one))]).status_code == 303
    fake_bambuddy.unavailable = True  # and there is no record of any day
    page = bb_client.get(f"/articles/{a}/filaments")
    assert page.status_code == 200 and translate("en", "filament.unknown") in unescape(page.text)
    assert (str(one), True) in boxes(page.text) and len(boxes(page.text)) == 2
    assert post(bb_client, a, [("slot", "1"), ("tick_1", str(one))]).status_code == 303  # saving loses nothing
    assert group(bb_client, a) == ({one}, one)


def test_no_connection_is_not_a_500(ready_client):
    a = make(ready_client)
    page = ready_client.get(f"/articles/{a}/filaments")
    assert page.status_code == 200 and boxes(page.text) == []


def test_rejected_spool_list_is_not_a_500(bb_client, fake_bambuddy):
    two_blacks(fake_bambuddy)
    a = make(bb_client)
    fake_bambuddy.fail_next("list_spools", BambuddyRejected("forbidden", status=403))
    page = bb_client.get(f"/articles/{a}/filaments")
    assert page.status_code == 200 and "not known" in page.text


def test_successful_save_does_not_ask_bambuddy(bb_client, fake_bambuddy):
    two_blacks(fake_bambuddy)
    a = make(bb_client)
    bb_client.get(f"/articles/{a}/filaments")
    one = sort_id(bb_client, "Brand A")
    token = csrf_of(bb_client, f"/articles/{a}/filaments")
    fake_bambuddy.unavailable = True
    fake_bambuddy.calls.clear()
    r = bb_client.post(f"/articles/{a}/filaments", data={"csrf_token": token, "slot": "1", "tick_1": str(one)},
                       follow_redirects=False)
    assert r.status_code == 303 and group(bb_client, a) == ({one}, one)
    assert "list_spools" not in fake_bambuddy.calls


def test_archived_article_is_read_only(bb_client, fake_bambuddy):
    two_blacks(fake_bambuddy)
    a = make(bb_client, archived=True)
    page = bb_client.get(f"/articles/{a}/filaments")
    assert page.status_code == 200 and "archived article" in page.text  # the form enters the sorts too
    assert 'type="submit">Save' not in page.text and "disabled" in page.text
    one = sort_id(bb_client, "Brand A")
    r = post(bb_client, a, [("slot", "1"), ("tick_1", str(one))], page_path=f"/articles/{a}")
    assert r.status_code == 200 and "archived" in r.text
    assert group(bb_client, a) == (set(), None)
    assert f"/articles/{a}/filaments" not in bb_client.get(f"/articles/{a}").text


def test_unknown_article(bb_client):
    assert bb_client.get("/articles/999/filaments").status_code == 404


# ---- the article page: what each slot uses, or "Filament not assigned" (Spec 9.1) ----------------


def test_detail_shows_not_assigned_and_the_link(bb_client):
    a = make(bb_client)
    detail = bb_client.get(f"/articles/{a}").text
    assert translate("en", "filament.not_assigned") in detail
    assert f"/articles/{a}/filaments" in detail


def test_detail_shows_the_reordered_sort_and_how_many_more(bb_client, fake_bambuddy):
    two_blacks(fake_bambuddy)
    a = make(bb_client)
    bb_client.get(f"/articles/{a}/filaments")
    one, two = sort_id(bb_client, "Brand A"), sort_id(bb_client, "Brand B")
    assert post(bb_client, a, [("slot", "1"), ("tick_1", str(one)), ("tick_1", str(two)), ("reorder_1", str(two))]).status_code == 303
    detail = unescape(bb_client.get(f"/articles/{a}").text)
    assert translate("en", "filament.and_more", label="Brand B PLA Schwarz", n=1) in detail
    assert translate("en", "filament.not_assigned") not in detail
    assert "Filament assignment saved." in detail


def test_german(bb_client, fake_bambuddy):
    two_blacks(fake_bambuddy)
    a = make(bb_client)
    with sm_of(bb_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    page = unescape(bb_client.get(f"/articles/{a}/filaments").text)
    assert translate("de", "filament.col.reordered") in page and translate("de", "filament.intro") in page
    assert "Filament nicht zugeordnet" in unescape(bb_client.get(f"/articles/{a}").text)


# ---- the look (page head, card head, table card per slot, info button) ---------------------------


def head_of(page):
    return re.search(r'<header class="page-head">.*?</header>', page, re.S).group(0)


def test_head_has_package_symbol_article_name_and_sub(bb_client, fake_bambuddy):
    two_blacks(fake_bambuddy)
    a = make(bb_client)
    head = head_of(bb_client.get(f"/articles/{a}/filaments").text)
    assert "#package" in head and "<h1>Template A</h1>" in head
    assert f'<p class="page-sub">{translate("en", "filament.map_sub")}</p>' in head


def test_one_table_card_per_slot_inside_the_form(bb_client, fake_bambuddy):
    two_blacks(fake_bambuddy)
    a = make(bb_client, slots=((1, "PLA", "#000000", 10.0), (2, "PLA", "#FFFFFF", 5.0)))
    page = bb_client.get(f"/articles/{a}/filaments").text
    form = re.search(rf'<form method="post" action="/articles/{a}/filaments".*?</form>', page, re.S).group(0)
    assert form.count('<div class="card table-wrap table-card slot-group">') == 2
    assert form.count('<input type="hidden" name="slot" value="') == 2
    assert 'class="live-stale"' in form and 'name="csrf_token"' in form
    assert re.search(r'<a class="btn secondary" href="/articles/\d+">Cancel</a>', form)
    for table in re.findall(r"<table>.*?</table>", form, re.S):
        assert len(re.findall(r"<th[ >]", table)) == 4  # narrow enough for a phone


def test_info_button_says_what_the_intro_does_not(bb_client, fake_bambuddy):  # p62
    two_blacks(fake_bambuddy)
    a = make(bb_client)
    page = bb_client.get(f"/articles/{a}/filaments").text
    pops = [unescape(p) for p in re.findall(r'<div class="info-pop" role="note">(.*?)</div>', page, re.S)]
    assert pops == [translate("en", "info.filament_mapping")]
    for lang in ("en", "de"):
        intro, info = translate(lang, "filament.intro"), translate(lang, "info.filament_mapping")
        assert info not in intro and intro not in info
        assert len(info) < 320


def test_form_region_holds_whole_cards_only(bb_client, fake_bambuddy):
    two_blacks(fake_bambuddy)
    a = make(bb_client)
    piece = bb_client.get(f"/articles/{a}/filaments", headers={regions.HEADER: "form"}).text
    assert piece.count("<table") == 1 and "card-head" in piece
