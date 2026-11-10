"""The filament page and the settings of a filament sort (Spec 9.3, 9.4, 10.3; B6 Task 9)."""

import re
from datetime import timedelta

import pytest

from conftest import csrf_of, sm_of
from factories import assign_sort, make_article, make_filament_settings, make_part, make_snapshot, make_sort, tick_sorts
from reprintbuddy import settings as st
from reprintbuddy.bambuddy.client import BambuddyRejected
from reprintbuddy.domain import filament
from reprintbuddy.models import ArticleFilament, FilamentSettings, FilamentSort, utcnow


def rows_of(html):
    return re.findall(r"<tr>(.*?)</tr>", html, re.S)


def use_sort(client, **sort_fields):
    """A sort (key as the fake spool's) used by an active article: the page lists it (p46)."""
    with sm_of(client).begin() as s:
        sort = make_sort(s, **sort_fields)
        assign_sort(s, sort, name=f"Uses {sort.id}")
        return sort.id


def test_filament_list_live(bb_client, fake_bambuddy):  # Spec 9.1
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black", remaining_g=800.0)
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black", remaining_g=300.0)
    use_sort(bb_client)
    page = bb_client.get("/filament")
    assert page.status_code == 200
    lines = [r for r in rows_of(page.text) if "PLA Black" in r]
    assert len(lines) == 1 and "1100" in lines[0] and "1100.0" not in lines[0]
    assert "einstellen" not in lines[0] and "Configure" in lines[0]  # no settings yet
    assert "Live from Bambuddy" in page.text


def test_configured_sort_links_to_form(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    with sm_of(bb_client).begin() as s:
        sort = make_sort(s)  # same key as the spool: one directory entry
        make_filament_settings(s, sort.id)
        assign_sort(s, sort)
    page = bb_client.get("/filament")
    assert f'href="/filament/{sort.id}"' in page.text and '<span class="badge tone-ok">configured</span>' in page.text


def test_filament_page_unavailable_uses_snapshot(bb_client, fake_bambuddy):  # Review Focus 2
    yesterday = utcnow().date() - timedelta(days=1)
    with sm_of(bb_client).begin() as s:
        sort = make_sort(s)
        assign_sort(s, sort)
        make_snapshot(s, yesterday, 1, sort.id, 640.0)
    fake_bambuddy.unavailable = True
    page = bb_client.get("/filament")
    assert page.status_code == 200
    assert "640" in page.text and f"Stock as of {yesterday:%Y-%m-%d}" in page.text


def recorded_page(bb_client, fake_bambuddy):
    """The page while Bambuddy cannot be reached and a record of yesterday is shown."""
    yesterday = utcnow().date() - timedelta(days=1)
    with sm_of(bb_client).begin() as s:
        sort = make_sort(s)
        assign_sort(s, sort)
        make_snapshot(s, yesterday, 2, sort.id, 640.0)
    fake_bambuddy.unavailable = True
    return bb_client.get("/filament").text, f"{yesterday:%Y-%m-%d}"


def test_recorded_stock_is_marked_at_the_numbers(bb_client, fake_bambuddy):  # p63
    page, day = recorded_page(bb_client, fake_bambuddy)
    note = en("filament.as_of", day=day)
    summary = regions.extract(page, ["summary"])["summary"]
    # The two cards that show recorded numbers say from when they are; the card of the sorts is no stock.
    for label in (en("kpi.filament_stock"), en("kpi.filament_spools")):
        assert re.search(rf'<span class="kpi-label">{re.escape(label)}</span><span class="kpi-note">{re.escape(note)}</span>', summary), label
    assert summary.count('class="kpi-note"') == 2
    assert [label for _, label in kpi_cards(summary)][:3] == [
        en("kpi.filament_sorts"), en("kpi.filament_stock"), en("kpi.filament_spools"),
    ]
    heads = re.findall(r"<th[^>]*>(.*?)</th>", page, re.S)
    marked = [h for h in heads if note in h]
    assert len(marked) == 2 and en("filament.col.stock") in marked[0] and en("filament.col.spools") in marked[1]
    assert en("filament.stale", day=day) in page  # the notice on top stays


def test_live_stock_is_not_marked(bb_client, fake_bambuddy):  # p63: nothing to say while it is live
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black", remaining_g=800.0)
    use_sort(bb_client)
    page = bb_client.get("/filament").text
    assert "kpi-note" not in page and "col-note" not in page and "As of" not in page


def test_filament_page_unknown_without_record(bb_client, fake_bambuddy):  # Anti-Ziel 4
    with sm_of(bb_client).begin() as s:
        sort = make_sort(s)
        assign_sort(s, sort)
    fake_bambuddy.unavailable = True
    page = bb_client.get("/filament")
    assert page.status_code == 200
    assert "nothing has been recorded yet" in page.text
    assert "saved assignment" not in page.text  # that sentence belongs to the mapping page
    line = [r for r in rows_of(page.text) if "PLA Black" in r][0]
    cells = re.findall(r"<td[^>]*>(.*?)</td>", line, re.S)
    assert cells[2].strip() == "—" and cells[3].strip() == "—"  # stock and spools, never "0 g"
    assert f'/filament/{sort.id}"' in cells[6]


def test_unmapped_slot_hint(bb_client, fake_bambuddy):
    with sm_of(bb_client).begin() as s:
        art = make_article(s, name="Template A")
        s.add(ArticleFilament(article_id=art.id, slot=2, grams=10.0, material="PLA", color_hex="#000000"))
        art_id = art.id
    page = bb_client.get("/filament")
    assert f'href="/articles/{art_id}/filaments"' in page.text and "Template A" in page.text


def sort_id_of(client):
    with sm_of(client).begin() as s:
        return make_sort(s).id


# One supplier (Spec 9.3): the field names of the block are those of a part's suppliers.
VALID = {
    "lead_time_days": "5", "safety_value": "300", "safety_unit": "grams", "pack_size": "1000",
    "min_order_packs": "1", "supplier": "Example Supplier", "order_url": "https://shop.example/pla-black",
}


def post(client, sort_id, **fields):
    data = {"csrf_token": csrf_of(client, f"/filament/{sort_id}"), **VALID, **fields}
    return client.post(f"/filament/{sort_id}", data=data, follow_redirects=False)


def post_rows(client, sort_id, rows, **fields):
    """A form with several suppliers: `rows` of (name, pack size, lead time)."""
    data = {
        "csrf_token": csrf_of(client, f"/filament/{sort_id}"), "safety_value": "0", "safety_unit": "days",
        "supplier": [name for name, _pack, _lead in rows], "order_url": ["" for _row in rows],
        "pack_size": [pack for _name, pack, _lead in rows], "min_order_packs": ["1" for _row in rows],
        "lead_time_days": [lead for _name, _pack, lead in rows], **fields,
    }
    return client.post(f"/filament/{sort_id}", data=data, follow_redirects=False)


def saved(client, sort_id):
    """Safety margin and unit, then lead time, roll size, minimum order, name and link of the default."""
    with sm_of(client).begin() as s:
        row = s.get(FilamentSettings, sort_id)
        if row is None:
            return None
        default = filament.default_supplier(s, sort_id)
        return (default.lead_time_days, row.safety_value, row.safety_unit, default.pack_size,
                default.min_order_packs, default.name, default.order_url)


def blocks(html):
    """The supplier blocks of the form, without the template for a new one."""
    return re.sub(r"<template.*?</template>", "", html, flags=re.S).count("data-supplier>")


def suppliers_of(client, sort_id):
    with sm_of(client).begin() as s:
        return [(r.name, r.pack_size, r.min_order_packs, r.lead_time_days, r.is_default)
                for r in filament.sort_suppliers(s, sort_id)]


def test_save_settings(ready_client):
    sid = sort_id_of(ready_client)  # works without Bambuddy
    r = post(ready_client, sid)
    assert r.status_code == 303 and r.headers["location"] == "/filament"
    assert saved(ready_client, sid) == (5, 300, "grams", 1000, 1, "Example Supplier", "https://shop.example/pla-black")
    form = ready_client.get(f"/filament/{sid}").text
    assert 'value="Example Supplier"' in form and 'value="300"' in form
    assert re.search(r'<option value="grams" selected>', form)


def test_settings_defaults(ready_client):
    sid = sort_id_of(ready_client)
    form = ready_client.get(f"/filament/{sid}").text
    assert 'name="pack_size"' in form and 'value="1000"' in form and "PLA Black" in form
    assert blocks(form) == 1  # one supplier to start with


def test_several_suppliers_with_one_default(ready_client):
    """Spec 9.3 (p281): like a part, 1 to 10 suppliers, exactly one the default."""
    sid = sort_id_of(ready_client)
    r = post_rows(ready_client, sid, [("Example Shop", "1000", "3"), ("Roll Shop", "500", "1")], supplier_default="1")
    assert r.status_code == 303
    assert suppliers_of(ready_client, sid) == [("Example Shop", 1000, 1, 3, False), ("Roll Shop", 500, 1, 1, True)]
    form = ready_client.get(f"/filament/{sid}").text
    assert blocks(form) == 2
    assert re.search(r'name="supplier_default" value="1" checked', form)


def test_no_default_is_an_error_at_the_form(ready_client):
    sid = sort_id_of(ready_client)
    r = post_rows(ready_client, sid, [("Example Shop", "1000", "0"), ("Roll Shop", "1000", "0")])
    assert r.status_code == 200 and en("filament.supplier_default") in unescape(r.text)
    assert saved(ready_client, sid) is None and 'value="Roll Shop"' in r.text


def test_an_error_names_the_supplier(ready_client):
    sid = sort_id_of(ready_client)
    r = post_rows(ready_client, sid, [("", "1000", "0"), ("", "0", "0")], supplier_default="0")
    assert r.status_code == 200 and "Supplier 2:" in unescape(r.text)


def test_add_and_remove_without_javascript_save_nothing(ready_client):
    sid = sort_id_of(ready_client)
    added = post(ready_client, sid, suppliers_add="1")
    assert added.status_code == 200 and blocks(added.text) == 2
    assert saved(ready_client, sid) is None
    removed = post_rows(ready_client, sid, [("Example Shop", "1000", "0"), ("Roll Shop", "1000", "0")],
                        supplier_default="0", suppliers_remove="0")
    assert removed.status_code == 200 and blocks(removed.text) == 1
    assert 'value="Roll Shop"' in removed.text and 'value="Example Shop"' not in removed.text
    assert saved(ready_client, sid) is None


def test_the_names_of_parts_and_sorts_are_offered(ready_client):
    """Spec 9.3: names used at parts and spool sorts are offered while typing."""
    sid = sort_id_of(ready_client)
    with sm_of(ready_client).begin() as s:
        make_part(s, supplier="Part Shop")
        make_filament_settings(s, make_sort(s, brand="Brand B").id, supplier="Roll Shop")
    form = ready_client.get(f"/filament/{sid}").text
    assert '<option value="Part Shop">' in form and '<option value="Roll Shop">' in form
    assert 'list="supplier-names"' in form
    assert '<script src="/static/js/suppliers.js' in form


@pytest.mark.parametrize(
    "field,value",
    [("pack_size", "0"), ("pack_size", ""), ("pack_size", "abc"), ("lead_time_days", "366"),
     ("order_url", "ftp://x"), ("min_order_packs", "1001"), ("safety_value", "-1")],
)
def test_settings_invalid_numbers(ready_client, field, value):  # Review Focus 4
    sid = sort_id_of(ready_client)
    r = post(ready_client, sid, **{field: value})
    assert r.status_code == 200 and 'role="alert"' in r.text
    assert saved(ready_client, sid) is None
    assert 'value="Example Supplier"' in r.text  # inputs stay


def test_settings_replace_existing(ready_client):
    sid = sort_id_of(ready_client)
    with sm_of(ready_client).begin() as s:
        make_filament_settings(s, sid, lead_time_days=1)
    assert post(ready_client, sid).status_code == 303
    assert saved(ready_client, sid)[0] == 5
    assert len(suppliers_of(ready_client, sid)) == 1


def test_unknown_sort_is_404(ready_client):
    assert ready_client.get("/filament/999").status_code == 404
    token = csrf_of(ready_client, "/filament")
    r = ready_client.post("/filament/999", data={"csrf_token": token, **VALID}, follow_redirects=False)
    assert r.status_code == 404


def test_filament_german(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    sid = use_sort(bb_client)  # same key as the spool
    with sm_of(bb_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    page = bb_client.get("/filament").text
    assert "Filament" in page and "Bestand" in page and "einstellen" in page
    form = bb_client.get(f"/filament/{sid}").text
    for word in ("Lieferzeit", "Sicherheitspolster", "Bestelleinheit", "Mindestbestellmenge",
                 "Lieferant", "Bestell-Link"):
        assert word in form


@pytest.mark.parametrize("path", ["/filament", "/filament/{id}"])
def test_nav_filament_active(bb_client, path):
    sid = sort_id_of(bb_client)
    html = bb_client.get(path.format(id=sid)).text
    active = re.findall(r'class="nav-link active"[^>]*>.*?<span class="nav-label">(.*?)</span>', html, re.S)
    assert set(active) == {"Filament"}
    assert 'href="/filament"' in html


def test_sort_label_links_to_settings_and_hint_links_are_cell_links(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    with sm_of(bb_client).begin() as s:
        assign_sort(s, make_sort(s))
        art = make_article(s, name="Template A")
        s.add(ArticleFilament(article_id=art.id, slot=1, grams=10.0, material="PETG", color_hex="#FFFFFF"))
        art_id = art.id
    html = bb_client.get("/filament").text
    sid = re.search(r'<a href="/filament/(\d+)">Example Brand PLA Black</a>', html).group(1)
    assert f'href="/filament/{sid}"' in html
    # in a table cell, where the app styles links green (td a)
    assert re.search(rf'<td><a href="/articles/{art_id}/filaments">', html)


def test_filament_page_rejected_spool_list_is_not_a_500(bb_client, fake_bambuddy):  # Review Focus 2
    yesterday = utcnow().date() - timedelta(days=1)
    with sm_of(bb_client).begin() as s:
        sort = make_sort(s)
        assign_sort(s, sort)
        make_snapshot(s, yesterday, 1, sort.id, 640.0)
    fake_bambuddy.fail_next("list_spools", BambuddyRejected("forbidden", status=403))
    page = bb_client.get("/filament")
    assert page.status_code == 200
    assert "640" in page.text and f"Stock as of {yesterday:%Y-%m-%d}" in page.text


def test_filament_list_spool_without_color_name(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", subtype="Matte", color_hex="#000000", color_name=None)
    use_sort(bb_client, subtype="Matte", color_name=None)
    page = bb_client.get("/filament")
    assert page.status_code == 200
    lines = [r for r in rows_of(page.text) if "PLA Matte #000000" in r]
    assert len(lines) == 1
    visible = re.sub(r'(?:aria-label|title)="[^"]*"', "", lines[0])  # the spoken name of the row button repeats the label (p68)
    assert "Matte" in visible and visible.count("Matte") == 1
    assert 'class="swatch"' in lines[0]
    assert "None" not in page.text


# ---- the look of v0.3.3 (page head, key figures, table card, symbol buttons, info buttons) -----

from html import unescape  # noqa: E402

from reprintbuddy.web import regions  # noqa: E402
from reprintbuddy.web.i18n import translate  # noqa: E402


def en(key, **params):
    return translate("en", key, **params)


def de(key, **params):
    return translate("de", key, **params)


def head_of(page):
    return re.search(r'<header class="page-head">.*?</header>', page, re.S).group(0)


def region(client, path, name):
    response = client.get(path, headers={regions.HEADER: name})
    assert response.status_code == 200
    return response.text


def kpi_cards(html):
    """(value, label) of every key figure card in `html`."""
    return re.findall(r'<span class="kpi-value">(.*?)</span><span class="kpi-label">(.*?)</span>', html)


def spool_pair(fake):
    fake.add_spool("PLA", color_hex="#000000", color_name="Black", remaining_g=800.0)
    fake.add_spool("PLA", color_hex="#000000", color_name="Black", remaining_g=300.0)


def test_list_head_has_symbol_title_and_sub(bb_client, fake_bambuddy):
    spool_pair(fake_bambuddy)
    head = head_of(bb_client.get("/filament").text)
    assert "#disc-3" in head and "<h1>Filament</h1>" in head
    assert f'<p class="page-sub">{en("filament.sub")}</p>' in head


def test_list_key_figures_match_the_rows(bb_client, fake_bambuddy):
    spool_pair(fake_bambuddy)
    fake_bambuddy.add_spool("PETG", color_hex="#FFFFFF", color_name="White", remaining_g=250.0)
    use_sort(bb_client)
    use_sort(bb_client, material="PETG", color_hex="#FFFFFF", color_name="White")
    page = bb_client.get("/filament").text
    summary = regions.extract(page, ["summary"])["summary"]
    cards = {label: value for value, label in kpi_cards(summary)}
    assert cards[en("kpi.filament_sorts")] == "2"
    assert cards[en("kpi.filament_stock")] == "1350"  # 1100 + 250, as the rows show them
    assert cards[en("kpi.filament_spools")] == "3"
    assert cards[en("kpi.filament_unconfigured")] == "2"  # no settings yet: no suggestions for them
    assert len([r for r in rows_of(page) if 'class="swatch"' in r]) == 2


def test_list_key_figures_drop_the_unconfigured_card_once_set(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    with sm_of(bb_client).begin() as s:
        make_filament_settings(s, make_sort(s).id)
    summary = region(bb_client, "/filament", "summary")
    assert [label for _, label in kpi_cards(summary)] == [
        en("kpi.filament_sorts"), en("kpi.filament_stock"), en("kpi.filament_spools"),
    ]


def test_list_key_figures_without_known_stock_show_no_zero(bb_client, fake_bambuddy):  # Anti-Ziel 4
    with sm_of(bb_client).begin() as s:
        make_sort(s)
    fake_bambuddy.unavailable = True
    summary = region(bb_client, "/filament", "summary")
    labels = [label for _, label in kpi_cards(summary)]
    assert en("kpi.filament_sorts") in labels
    assert en("kpi.filament_stock") not in labels and en("kpi.filament_spools") not in labels


def test_list_summary_region_follows_the_list_topics(bb_client):
    page = bb_client.get("/filament").text
    assert 'data-live-region="summary" data-live-topics="filament article queue"' in page


def test_list_is_a_table_card_with_symbol_buttons(bb_client, fake_bambuddy):
    spool_pair(fake_bambuddy)
    fake_bambuddy.add_spool("PETG", color_hex="#FFFFFF", color_name="White")
    with sm_of(bb_client).begin() as s:
        configured = make_sort(s, material="PETG", color_hex="#FFFFFF", color_name="White")
        make_filament_settings(s, configured.id)
        assign_sort(s, configured)
        assign_sort(s, make_sort(s), name="Black user")
        cid = configured.id
    page = bb_client.get("/filament").text
    assert '<div class="card table-wrap table-card">' in page
    assert "<section" not in page and "section wide" not in page
    assert '<th class="actions-col"><span class="sr-only">Actions</span></th>' in page
    black = [r for r in rows_of(page) if "PLA Black" in r][0]
    assert re.search(r'<td class="actions-col"><div class="row-actions"><a class="icon-btn" href="/filament/\d+" '
                     r'title="Configure" aria-label="Configure: [^"]+">.*?#settings', black, re.S)
    assert '<span class="badge tone-warn">not configured</span>' in black
    done = [r for r in rows_of(page) if f'href="/filament/{cid}"' in r][0]
    assert 'aria-label="Edit settings: ' in done and "#pencil" in done
    assert '<span class="badge tone-ok">configured</span>' in done


def test_list_empty_state_is_not_a_table(bb_client):
    page = bb_client.get("/filament").text
    assert en("filament.no_sorts") in page and "<table" not in page


def test_unmapped_hint_is_a_card(bb_client, fake_bambuddy):
    with sm_of(bb_client).begin() as s:
        art = make_article(s, name="Template A")
        s.add(ArticleFilament(article_id=art.id, slot=2, grams=10.0, material="PLA", color_hex="#000000"))
    hint = region(bb_client, "/filament", "hint")
    assert '<div class="card hint-card">' in hint and "Template A" in hint and "<section" not in hint


def test_settings_head_card_head_and_buttons(ready_client):
    sid = sort_id_of(ready_client)
    page = ready_client.get(f"/filament/{sid}").text
    head = head_of(page)
    assert "#disc-3" in head and "<h1>Example Brand PLA Black</h1>" in head
    assert f'<p class="page-sub">{en("filament.settings_sub")}</p>' in head
    form = regions.extract(page, ["form"])["form"]
    assert re.search(rf'<div class="card-head"><h2>.*?#settings.*?{en("filament.settings_card")}</h2>', form, re.S)
    assert "section wide" not in page
    assert re.search(r'<form [^>]*data-live-watch="filament-settings:\d+">\s*<p class="live-stale"', form)
    assert re.search(r'<button class="btn" type="submit">Save</button>', form)
    assert re.search(r'<a class="btn secondary" href="/filament">Cancel</a>', form)


@pytest.mark.parametrize("lang,tr", [("en", en), ("de", de)])
def test_settings_fields_have_info_buttons(ready_client, lang, tr):
    sid = sort_id_of(ready_client)
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", lang, commit=False)
    page = ready_client.get(f"/filament/{sid}").text
    pops = [unescape(p) for p in re.findall(r'<div class="info-pop" role="note">(.*?)</div>', page, re.S)]
    assert pops == [tr(key) for key in
                    ("info.filament_safety", "info.filament_pack_grams", "info.filament_min_order_packs",
                     "info.filament_lead_time")]
    for name in ("lead_time_days", "pack_size", "safety_value", "min_order_packs"):
        tag = re.search(rf'<input[^>]*\bname="{name}"[^>]*>', page).group(0)
        field_id = re.search(r'\bid="([^"]+)"', tag).group(1)
        assert f'<label for="{field_id}">' in page
    for label in re.findall(r"<label\b.*?</label>", page, re.S):
        assert 'class="info"' not in label
    for target in re.findall(r'<label for="([^"]+)"', page):
        assert re.search(rf'<(?:input|select)[^>]*\bid="{re.escape(target)}"', page), target


def test_settings_form_field_names(ready_client):
    """Since 0016 the roll size is a supplier's field, named as a part supplier's pack size."""
    sid = sort_id_of(ready_client)
    page = ready_client.get(f"/filament/{sid}").text
    for name in ("csrf_token", "lead_time_days", "pack_size", "safety_value", "safety_unit",
                 "min_order_packs", "supplier", "order_url", "supplier_default"):
        assert f'name="{name}"' in page, name


def test_settings_error_page_keeps_the_card(ready_client):
    sid = sort_id_of(ready_client)
    r = post(ready_client, sid, pack_size="0")
    assert r.status_code == 200 and 'role="alert"' in r.text and 'class="card-head"' in r.text
    assert 'value="Example Supplier"' in r.text


# ---- p46: the page lists only sorts that an active article uses --------------------------------


def test_unassigned_sort_is_not_listed_or_counted(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black", remaining_g=800.0)
    fake_bambuddy.add_spool("PETG", color_hex="#FF0000", color_name="Red", remaining_g=500.0)
    with sm_of(bb_client).begin() as s:
        black = make_sort(s)  # the spool's sort, used by an article
        assign_sort(s, black)
    html = bb_client.get("/filament").text
    assert len([r for r in rows_of(html) if "PLA" in r]) == 1
    assert "PLA Black" in html and "PETG" not in html
    cards = {label: value for value, label in kpi_cards(regions.extract(html, ["summary"])["summary"])}
    assert cards[en("kpi.filament_sorts")] == "1"
    assert cards[en("kpi.filament_stock")] == "800" and cards[en("kpi.filament_spools")] == "1"


def test_sort_of_an_archived_article_is_not_listed(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    with sm_of(bb_client).begin() as s:
        assign_sort(s, make_sort(s), archived=True)
    html = bb_client.get("/filament").text
    assert "PLA Black" not in html
    assert en("filament.no_sorts") in html


def test_sort_used_by_two_articles_is_listed_once(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    with sm_of(bb_client).begin() as s:
        sort = make_sort(s)
        assign_sort(s, sort, name="One")
        assign_sort(s, sort, name="Two")
    html = bb_client.get("/filament").text
    assert len([r for r in rows_of(html) if "PLA Black" in r]) == 1


def test_assigning_in_the_article_makes_the_sort_appear(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    with sm_of(bb_client).begin() as s:
        sort = make_sort(s)
        art = make_article(s, name="Template A")
        s.add(ArticleFilament(article_id=art.id, slot=1, grams=10.0, material="PLA", color_hex="#000000"))
        art_id = art.id
    assert "PLA Black" not in bb_client.get("/filament").text
    token = csrf_of(bb_client, f"/articles/{art_id}/filaments")
    r = bb_client.post(
        f"/articles/{art_id}/filaments", data={"csrf_token": token, "slot": "1", "tick_1": str(sort.id)},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text[:300]
    assert "PLA Black" in bb_client.get("/filament").text


def test_article_page_offers_only_fitting_spool_sorts(bb_client, fake_bambuddy):  # Spec 9.1
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    fake_bambuddy.add_spool("PETG", color_hex="#FF0000", color_name="Red")
    with sm_of(bb_client).begin() as s:
        art = make_article(s, name="Template A")
        s.add(ArticleFilament(article_id=art.id, slot=1, grams=10.0, material="PLA", color_hex="#000000"))
        art_id = art.id
    html = bb_client.get(f"/articles/{art_id}/filaments").text
    assert "Black" in html and "Red" not in html


def test_settings_of_an_unassigned_sort_stay_reachable_by_url(bb_client):
    sid = sort_id_of(bb_client)
    assert bb_client.get(f"/filament/{sid}").status_code == 200


def test_empty_page_says_how_a_sort_gets_here(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    html = bb_client.get("/filament").text
    assert "PLA Black" not in html
    assert "article" in re.search(r'<p class="muted">(No spool sorts[^<]*)</p>', html).group(1).lower()


def test_unconfigured_card_counts_only_shown_sorts(bb_client, fake_bambuddy):  # p46
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    fake_bambuddy.add_spool("PETG", color_hex="#FF0000", color_name="Red")
    use_sort(bb_client)  # one shown sort without settings; the PETG sort is not used by an article
    cards = {label: value for value, label in kpi_cards(region(bb_client, "/filament", "summary"))}
    assert cards[en("kpi.filament_unconfigured")] == "1"


def test_empty_page_in_german(bb_client):  # p46
    with sm_of(bb_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    assert de("filament.no_sorts") in bb_client.get("/filament").text


# ---- v0.3.7: spool sorts, ticked and reordered (Spec 9.1, 9.3), one spelling of the state (p61) ----


def ticked_not_reordered(client):
    """Two spool sorts ticked in one slot; the first is reordered, the second only ticked."""
    with sm_of(client).begin() as s:
        first = make_sort(s, brand="Brand A")
        second = make_sort(s, brand="Brand B")
        art = make_article(s, name="Template A")
        s.add(ArticleFilament(article_id=art.id, slot=1, grams=10.0, material="PLA", color_hex="#000000"))
        s.flush()
        tick_sorts(s, art.id, 1, first, second)
        return first.id, second.id


def test_a_ticked_sort_that_is_not_reordered_is_listed_without_demand(bb_client, fake_bambuddy):
    first, second = ticked_not_reordered(bb_client)
    html = bb_client.get("/filament").text
    row = [r for r in rows_of(html) if f'href="/filament/{second}"' in r][0]
    assert "Brand B PLA Black" in row
    assert f'<span class="badge tone-neutral">{en("filament.not_reordered")}</span>' in row
    cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
    assert cells[5].strip() == "—"  # no planned demand of its own
    reordered = [r for r in rows_of(html) if f'href="/filament/{first}"' in r][0]
    assert en("filament.not_configured") in reordered
    cards = {label: value for value, label in kpi_cards(regions.extract(html, ["summary"])["summary"])}
    assert cards[en("kpi.filament_sorts")] == "2"
    assert cards[en("kpi.filament_unconfigured")] == "1"  # only the reordered one needs settings


def test_the_brand_is_part_of_the_name(bb_client, fake_bambuddy):  # Spec 9.3: with brand
    fake_bambuddy.add_spool("PLA", brand="Brand A", color_hex="#000000", color_name="Black")
    fake_bambuddy.add_spool("PLA", brand="Brand B", color_hex="#000000", color_name="Black")
    with sm_of(bb_client).begin() as s:
        assign_sort(s, make_sort(s, brand="Brand A"))
    html = bb_client.get("/filament").text
    assert "Brand A PLA Black" in html and "Brand B" not in html


@pytest.mark.parametrize("lang", ["en", "de"])
def test_the_state_is_written_alike_in_small_letters(bb_client, fake_bambuddy, lang):  # p61
    tr = en if lang == "en" else de
    with sm_of(bb_client).begin() as s:
        st.set_setting(s, "language", lang, commit=False)
        configured = make_sort(s, brand="Brand A")
        make_filament_settings(s, configured.id)
        assign_sort(s, configured)
        assign_sort(s, make_sort(s, brand="Brand B"), name="Other")
    html = bb_client.get("/filament").text
    badges = re.findall(r'<span class="badge tone-\w+">(.*?)</span>', html)
    assert tr("filament.configured") in badges and tr("filament.not_configured") in badges
    for text in (tr("filament.configured"), tr("filament.not_configured"), tr("filament.not_reordered")):
        assert text == text.lower()


def test_a_legacy_sort_is_neither_listed_nor_configurable(bb_client):
    with sm_of(bb_client).begin() as s:
        old = make_sort(s, brand=None)
        old.legacy, old.sort_key = True, f"legacy:{old.id}"
        assign_sort(s, make_sort(s))
        sid = old.id
    assert f'href="/filament/{sid}"' not in bb_client.get("/filament").text
    assert bb_client.get(f"/filament/{sid}").status_code == 404
