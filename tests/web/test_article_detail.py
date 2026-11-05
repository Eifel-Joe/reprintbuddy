import re

from sqlalchemy import select

from conftest import csrf_of, sm_of
from factories import make_article, make_part
from reprintbuddy import settings as st
from reprintbuddy.domain import stock
from reprintbuddy.models import Article, ArticleFilament, ArticlePart
from reprintbuddy.web.i18n import translate


def en(key, **params):
    return translate("en", key, **params)


def stat(html, label):
    """The number shown next to a label on the detail page."""
    return re.search(rf'{re.escape(label)}</span>\s*<span class="stat-value[^"]*">(-?\d+)<', html).group(1)


def options_of(html, name="target"):
    block = re.search(rf'<select[^>]*name="{name}"[^>]*>(.*?)</select>', html, re.S).group(1)
    return [(v, bool(sel), label) for v, sel, label in re.findall(r'<option value="([^"]*)"( selected)?>([^<]*)</option>', block)]


def make(client, **fields):
    with sm_of(client).begin() as s:
        return make_article(s, **fields).id


def reload(client, article_id):
    with sm_of(client).begin() as s:
        return s.get(Article, article_id)


def edit_fields(client, article_id, **over):
    data = {
        "csrf_token": csrf_of(client, f"/articles/{article_id}/edit"),
        "name": "Template A", "sku": "", "units_per_print": "1", "min_stock": "0", "buffer": "0",
        "target": "model:A1",
    }
    data.update(over)
    return data


def test_detail_numbers(ready_client):  # Spec 6.1
    a = make(ready_client, stock=3, reserved=1, provisional=2, awaiting_assembly=1)
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    page = ready_client.get(f"/articles/{a}").text
    assert stat(page, "Bestand") == "3"
    assert stat(page, "Reserviert") == "1"
    assert stat(page, "Verfügbar") == "2"
    assert stat(page, "Vorläufig") == "2"
    assert stat(page, "Wartet auf Montage") == "1"


def test_detail_negative_available_red(ready_client):
    a = make(ready_client, stock=1, reserved=3)
    page = ready_client.get(f"/articles/{a}").text
    assert re.search(r'<span class="stat-value neg">-2<', page)


def test_detail_shows_settings_filament_parts_and_journal(ready_client):
    with sm_of(ready_client).begin() as s:
        art = make_article(s, name="Template A", sku="A-100", min_stock=4, buffer=2, target_model="A1", stock=5)
        s.add(ArticleFilament(article_id=art.id, slot=1, grams=42.5, material="PLA", color_hex="#000000"))
        insert = make_part(s, name="Insert M4")
        s.add(ArticlePart(article_id=art.id, part_id=insert.id, qty_per_unit=2))
        stock.sell_unreserved(s, art.id, 1, "user", note="direct from shelf")
        a = art.id
    page = ready_client.get(f"/articles/{a}").text
    for expected in ("Template A", "A-100", "42.5", "PLA", "Insert M4", "direct from shelf", f"/journal?article={a}", "Any A1"):
        assert expected in page, expected


def test_detail_404(ready_client):
    assert ready_client.get("/articles/999").status_code == 404


def test_thumbnail_served(ready_client):
    a = make(ready_client, thumbnail=b"\x89PNG-data")
    response = ready_client.get(f"/articles/{a}/thumbnail")
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    assert response.content == b"\x89PNG-data"
    b = make(ready_client, name="Template B", thumbnail=None)
    assert ready_client.get(f"/articles/{b}/thumbnail").status_code == 404
    assert ready_client.get("/articles/999/thumbnail").status_code == 404


def test_edit_settings(ready_client):
    a = make(ready_client)
    response = ready_client.post(
        f"/articles/{a}/edit", data=edit_fields(ready_client, a, min_stock="4", buffer="2", auto_enabled="on")
    )
    assert response.status_code == 303 and response.headers["location"] == f"/articles/{a}"
    art = reload(ready_client, a)
    assert (art.min_stock, art.buffer, art.auto_enabled) == (4, 2, True)
    page = ready_client.get(f"/articles/{a}").text
    assert stat(page, "Target quantity") == "6"
    assert en("articles.saved", name="Template A") in page


def test_edit_invalid(ready_client):
    a = make(ready_client, min_stock=1)
    response = ready_client.post(f"/articles/{a}/edit", data=edit_fields(ready_client, a, min_stock="-1"))
    assert response.status_code == 200
    assert reload(ready_client, a).min_stock == 1


def test_edit_does_not_change_stock_or_file(ready_client):
    a = make(ready_client, stock=4)
    ready_client.post(f"/articles/{a}/edit", data=edit_fields(ready_client, a, stock="99", file_name="x", plate_id="9"))
    art = reload(ready_client, a)
    assert (art.stock, art.plate_id, art.file_name) == (4, 1, "Template A.gcode.3mf")


def test_edit_target_select(bb_client):
    a = make(bb_client, target_model="A1")
    page = bb_client.get(f"/articles/{a}/edit").text
    assert ("model:A1", True, "Any A1") in options_of(page)
    bb_client.post(f"/articles/{a}/edit", data=edit_fields(bb_client, a, target="model:P1S"))
    art = reload(bb_client, a)
    assert (art.target_model, art.printer_id) == ("P1S", None)
    bb_client.post(f"/articles/{a}/edit", data=edit_fields(bb_client, a, target="printer:1"))
    art = reload(bb_client, a)
    assert (art.target_model, art.printer_id) == (None, 1)


def test_edit_bambuddy_down_keeps_target(bb_client, fake_bambuddy):
    a = make(bb_client, target_model="A1")
    fake_bambuddy.unavailable = True
    page = bb_client.get(f"/articles/{a}/edit")
    assert page.status_code == 200 and en("articles.printers_unavailable") in page.text
    assert options_of(page.text) == [("model:A1", True, "Any A1")]
    response = bb_client.post(f"/articles/{a}/edit", data=edit_fields(bb_client, a, min_stock="2", target="model:A1"))
    assert response.status_code == 303
    art = reload(bb_client, a)
    assert (art.min_stock, art.target_model, art.printer_id) == (2, "A1", None)


def test_edit_without_factory_shows_stored_target(ready_client):
    a = make(ready_client, printer_id=2)
    page = ready_client.get(f"/articles/{a}/edit")
    assert en("articles.printers_unavailable") in page.text
    assert options_of(page.text) == [("printer:2", True, "Printer #2")]


def test_edit_unknown_stored_printer_offered(bb_client):
    a = make(bb_client, printer_id=99)
    options = options_of(bb_client.get(f"/articles/{a}/edit").text)
    assert ("printer:99", True, en("articles.target_printer_id", id=99)) in options


def test_edit_bad_target_no_500(bb_client):
    a = make(bb_client, target_model="A1")
    response = bb_client.post(f"/articles/{a}/edit", data=edit_fields(bb_client, a, target="printer:abc"))
    assert response.status_code == 200
    assert reload(bb_client, a).target_model == "A1"


def test_edit_sku_conflict_message(ready_client):
    make(ready_client, name="Template A", sku="A-100")
    b = make(ready_client, name="Template B")
    response = ready_client.post(f"/articles/{b}/edit", data=edit_fields(ready_client, b, name="Template B", sku="A-100"))
    assert response.status_code == 200 and en("catalog.sku_taken", sku="A-100") in response.text


def test_edit_parts(ready_client):
    with sm_of(ready_client).begin() as s:
        insert = make_part(s, name="Insert M4").id
        hex_key = make_part(s, name="Hex key 2").id
        a = make_article(s).id
    ready_client.post(f"/articles/{a}/edit", data=edit_fields(ready_client, a, **{f"part_{insert}": "2", f"part_{hex_key}": "1"}))
    ready_client.post(f"/articles/{a}/edit", data=edit_fields(ready_client, a, **{f"part_{insert}": "2", f"part_{hex_key}": "0"}))
    with sm_of(ready_client).begin() as s:
        assert {l.part_id: l.qty_per_unit for l in s.scalars(select(ArticlePart))} == {insert: 2}


def test_edit_requires_csrf(ready_client):
    a = make(ready_client)
    data = edit_fields(ready_client, a)
    del data["csrf_token"]
    assert ready_client.post(f"/articles/{a}/edit", data=data).status_code == 403
    assert ready_client.get("/articles/999/edit").status_code == 404
