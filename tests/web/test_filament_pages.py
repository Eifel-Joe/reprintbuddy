"""The filament page and the settings of a filament sort (Spec 9.3, 9.4, 10.3; B6 Task 9)."""

import re
from datetime import timedelta

import pytest

from conftest import csrf_of, sm_of
from factories import make_article, make_filament_settings, make_snapshot, make_sort
from reprintbuddy import settings as st
from reprintbuddy.models import ArticleFilament, FilamentSettings, utcnow


def rows_of(html):
    return re.findall(r"<tr>(.*?)</tr>", html, re.S)


def test_filament_list_live(bb_client, fake_bambuddy):  # Spec 9.1
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black", remaining_g=800.0)
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black", remaining_g=300.0)
    page = bb_client.get("/filament")
    assert page.status_code == 200
    lines = [r for r in rows_of(page.text) if "Example Brand PLA Black" in r]
    assert len(lines) == 1 and "1100" in lines[0] and "1100.0" not in lines[0]
    assert "einstellen" not in lines[0] and "Configure" in lines[0]  # no settings yet
    assert "Live from Bambuddy" in page.text


def test_configured_sort_links_to_form(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    with sm_of(bb_client).begin() as s:
        sort = make_sort(s)  # same key as the spool: one directory entry
        make_filament_settings(s, sort.id)
    page = bb_client.get("/filament")
    assert f'href="/filament/{sort.id}"' in page.text and "Configured" in page.text


def test_filament_page_unavailable_uses_snapshot(bb_client, fake_bambuddy):  # Review Focus 2
    yesterday = utcnow().date() - timedelta(days=1)
    with sm_of(bb_client).begin() as s:
        sort = make_sort(s)
        make_snapshot(s, yesterday, 1, sort.id, 640.0)
    fake_bambuddy.unavailable = True
    page = bb_client.get("/filament")
    assert page.status_code == 200
    assert "640" in page.text and f"Stock as of {yesterday:%Y-%m-%d}" in page.text


def test_filament_page_unknown_without_record(bb_client, fake_bambuddy):  # Anti-Ziel 4
    with sm_of(bb_client).begin() as s:
        sort = make_sort(s)
    fake_bambuddy.unavailable = True
    page = bb_client.get("/filament")
    assert page.status_code == 200
    assert "nothing has been recorded yet" in page.text
    assert "saved assignment" not in page.text  # that sentence belongs to the mapping page
    line = [r for r in rows_of(page.text) if "Example Brand PLA Black" in r][0]
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


VALID = {
    "lead_time_days": "5", "safety_value": "300", "safety_unit": "grams", "pack_grams": "1000",
    "min_order_packs": "1", "supplier": "Example Supplier", "order_url": "https://shop.example/pla-black",
}


def post(client, sort_id, **fields):
    data = {"csrf_token": csrf_of(client, f"/filament/{sort_id}"), **VALID, **fields}
    return client.post(f"/filament/{sort_id}", data=data, follow_redirects=False)


def saved(client, sort_id):
    with sm_of(client).begin() as s:
        row = s.get(FilamentSettings, sort_id)
        return None if row is None else (row.lead_time_days, row.safety_value, row.safety_unit,
                                         row.pack_grams, row.min_order_packs, row.supplier, row.order_url)


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
    assert 'name="pack_grams"' in form and 'value="1000"' in form and "Example Brand PLA Black" in form


@pytest.mark.parametrize(
    "field,value",
    [("pack_grams", "0"), ("pack_grams", ""), ("pack_grams", "abc"), ("lead_time_days", "366"),
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


def test_unknown_sort_is_404(ready_client):
    assert ready_client.get("/filament/999").status_code == 404
    token = csrf_of(ready_client, "/filament")
    r = ready_client.post("/filament/999", data={"csrf_token": token, **VALID}, follow_redirects=False)
    assert r.status_code == 404


def test_filament_german(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA", color_hex="#000000", color_name="Black")
    sid = sort_id_of(bb_client)  # same key as the spool
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
        art = make_article(s, name="Template A")
        s.add(ArticleFilament(article_id=art.id, slot=1, grams=10.0, material="PETG", color_hex="#FFFFFF"))
        art_id = art.id
    html = bb_client.get("/filament").text
    sid = re.search(r'<a href="/filament/(\d+)">Example Brand PLA Black</a>', html).group(1)
    assert f'href="/filament/{sid}"' in html
    # in a table cell, where the app styles links green (td a)
    assert re.search(rf'<td><a href="/articles/{art_id}/filaments">', html)
