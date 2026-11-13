import re

import pytest
from sqlalchemy import select

from conftest import csrf_of, form_token_of, sm_of
from texts import en
from reprintbuddy.bambuddy.fake import FakeBambuddy
from reprintbuddy.models import Article, ArticleFilament, JournalEntry

FORM = "/articles/new/file/1/plate/1"


def tag_of(html, name):
    match = re.search(rf'<(?:input|select)[^>]*\bname="{name}"[^>]*>', html)
    assert match, f"no field {name}"
    return match.group(0)


def value_of(html, name):
    return re.search(r'\bvalue="([^"]*)"', tag_of(html, name)).group(1)


def options_of(html, name="target"):
    block = re.search(rf'<select[^>]*name="{name}"[^>]*>(.*?)</select>', html, re.S).group(1)
    return [(v, bool(sel), label) for v, sel, label in re.findall(r'<option value="([^"]*)"( selected)?>([^<]*)</option>', block)]


def fields(client, path=FORM, **over):
    data = {
        "csrf_token": csrf_of(client, path),
        "form_token": form_token_of(client, path, path),
        "name": "Template A", "sku": "", "units_per_print": "1", "min_stock": "0", "buffer": "0",
        "target": "model:A1",
    }
    data.update(over)
    return data


def articles(client):
    with sm_of(client).begin() as s:
        return list(s.scalars(select(Article)))


def test_browse_root_and_folder(bb_client):
    root = bb_client.get("/articles/new")
    assert root.status_code == 200 and "Templates" in root.text
    assert "/articles/new?folder=1" in root.text
    folder = bb_client.get("/articles/new?folder=1")
    assert "Template A.gcode.3mf" in folder.text
    assert "/articles/new/file/1" in folder.text


def test_browse_unknown_folder_param(bb_client):
    assert bb_client.get("/articles/new?folder=abc").status_code in (200, 404, 422)
    assert bb_client.get("/articles/new?folder=999").status_code == 200


def test_plates_listed_with_thumbnails(bb_client):
    page = bb_client.get("/articles/new/file/2")
    # the page header carries the logo too: look at the plate pictures only
    images = [src for src in re.findall(r'<img[^>]*src="([^"]+)"', page.text) if "/plate/" in src]
    assert images == ["/articles/new/file/2/plate/1/thumbnail", "/articles/new/file/2/plate/2/thumbnail"]
    image = bb_client.get(images[0])
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    assert image.content.startswith(b"\x89PNG")
    assert bb_client.get("/articles/new/file/2/plate/9/thumbnail").status_code == 404
    assert bb_client.get("/articles/new/file/99/plate/1/thumbnail").status_code == 404


def test_unknown_file_is_404_with_message(bb_client):
    response = bb_client.get("/articles/new/file/99")
    assert response.status_code == 404 and en("articles.file_missing") in response.text
    assert bb_client.get("/articles/new/file/99/plate/1").status_code == 404
    assert bb_client.get("/articles/new/file/1/plate/7").status_code == 404


def test_form_prefilled_with_spec_defaults(bb_client):  # Spec 5.2
    page = bb_client.get(FORM).text
    assert value_of(page, "name") == "Template A"
    assert value_of(page, "units_per_print") == "1"
    assert value_of(page, "min_stock") == "0" and value_of(page, "buffer") == "0"
    assert (("model:A1", True, "Any A1")) in options_of(page)
    assert " checked" not in tag_of(page, "needs_assembly")
    assert " checked" not in tag_of(page, "auto_enabled")
    assert "42.5" in page and "PLA" in page and "#000000" in page


def test_target_select_options(bb_client):
    labels = [label for _v, _s, label in options_of(bb_client.get(FORM).text)]
    assert labels == ["Any A1", "Any P1S", "Printer 1 (A1)", "Printer 2 (A1)", "Printer 3 (P1S)", "No print target"]


def test_target_file_model_without_printers(bb_client):
    bb_client.app.state.bambuddy_factory = lambda: _no_printers()
    options = options_of(bb_client.get(FORM).text)
    assert options == [("model:A1", True, "Any A1"), ("", False, "No print target")]


def _no_printers():
    fake = FakeBambuddy()
    folder = fake.add_folder("Templates")
    fake.add_file("Template A.gcode.3mf", folder.id, printer_model="A1")
    return fake


def test_form_german(bb_client):
    from reprintbuddy import settings as st

    with sm_of(bb_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    page = bb_client.get(FORM).text
    for word in ("Stück je Druck", "Mindestbestand", "Puffer", "Druckziel", "Handarbeit nötig", "Automatik", "Jeder A1", "Kein Druckziel"):
        assert word in page, word


def test_create_article(bb_client):  # Spec 5.1
    response = bb_client.post(FORM, data=fields(bb_client, sku="A-100"))
    created = articles(bb_client)
    assert len(created) == 1
    # p153: with filament slots the filament page comes first (the article page follows its save)
    assert response.status_code == 303 and response.headers["location"] == f"/articles/{created[0].id}/filaments?later=1&suggest=1"
    art = created[0]
    assert (art.file_name, art.plate_id, art.file_fingerprint, art.sku) == ("Template A.gcode.3mf", 1, "v1", "A-100")
    assert art.thumbnail == b"\x89PNG\r\n\x1a\n1:1"
    with sm_of(bb_client).begin() as s:
        rows = s.scalars(select(ArticleFilament)).all()
        assert [(r.slot, r.grams, r.material, r.color_hex) for r in rows] == [(1, 42.5, "PLA", "#000000")]
        assert s.scalars(select(JournalEntry)).all() == []  # creating is no booking


def test_create_leads_to_the_filament_page_with_both_notices(bb_client):  # p153
    response = bb_client.post(FORM, data=fields(bb_client))
    page = bb_client.get(response.headers["location"])
    assert page.status_code == 200
    assert en("articles.created", name="Template A") in page.text
    assert en("articles.created_filament") in page.text
    assert en("filament.title") in page.text
    assert en("articles.created", name="Template A") not in bb_client.get(response.headers["location"]).text  # shown once


def test_create_of_a_plate_without_filament_slots_goes_to_the_article_page(bb_client, fake_bambuddy):  # p153
    fake_bambuddy.set_requirements(1, 1, [])
    response = bb_client.post(FORM, data=fields(bb_client))
    created = articles(bb_client)
    assert len(created) == 1
    assert response.status_code == 303 and response.headers["location"] == f"/articles/{created[0].id}"
    page = bb_client.get(response.headers["location"]).text
    assert en("articles.created", name="Template A") in page and en("articles.created_filament") not in page


def test_create_uses_fresh_bambuddy_data_not_hidden_fields(bb_client, fake_bambuddy):
    data = fields(bb_client, file_name="Evil.3mf", fingerprint="forged", thumbnail="x", library_file_id="9")
    bb_client.post(FORM, data=data)
    art = articles(bb_client)[0]
    assert (art.file_name, art.file_fingerprint, art.library_file_id) == ("Template A.gcode.3mf", "v1", 1)


def test_plate_taken_marked_and_rejected(bb_client):
    bb_client.post(FORM, data=fields(bb_client))
    assert en("articles.plate_taken_by", name="Template A") in bb_client.get("/articles/new/file/1").text
    response = bb_client.post(FORM, data=fields(bb_client))
    assert response.status_code == 200
    assert en("articles.plate_taken", name="Template A") in response.text
    assert len(articles(bb_client)) == 1


def test_double_submit_creates_one(bb_client):
    data = fields(bb_client)
    first = bb_client.post(FORM, data=data)
    second = bb_client.post(FORM, data=data)
    assert first.status_code == 303 and second.status_code == 303
    assert second.headers["location"] != "" and len(articles(bb_client)) == 1
    assert en("form.duplicate") in bb_client.get("/articles").text


def test_bambuddy_missing_factory(ready_client):
    page = ready_client.get("/articles/new")
    assert page.status_code == 200 and en("articles.bambuddy_missing") in page.text
    for path in ("/articles/new/file/1", FORM):
        assert en("articles.bambuddy_missing") in ready_client.get(path).text
    assert ready_client.post(FORM, data={"csrf_token": csrf_of(ready_client, "/articles"), "form_token": "x"}).status_code == 200


def test_bambuddy_unavailable(bb_client, fake_bambuddy):
    data = fields(bb_client)
    fake_bambuddy.unavailable = True
    for path in ("/articles/new", "/articles/new/file/1", FORM):
        response = bb_client.get(path)
        assert response.status_code == 200 and en("articles.bambuddy_error") in response.text, path
    response = bb_client.post(FORM, data=data)
    assert response.status_code == 200 and en("articles.bambuddy_error") in response.text
    assert articles(bb_client) == []


def test_file_deleted_before_submit(bb_client, fake_bambuddy):
    data = fields(bb_client)
    fake_bambuddy.remove_file(1)
    response = bb_client.post(FORM, data=data)
    assert response.status_code == 200 and en("articles.file_missing") in response.text
    assert articles(bb_client) == []


@pytest.mark.parametrize(
    "over",
    [
        {"units_per_print": "abc"}, {"units_per_print": "0"}, {"units_per_print": "1.5"},
        {"units_per_print": ""}, {"min_stock": "-1"}, {"min_stock": "9" * 20}, {"buffer": "x"},
        {"target": "printer:abc"}, {"target": "bogus"}, {"name": "  "},
    ],
)
def test_invalid_numbers_no_500(bb_client, over):
    response = bb_client.post(FORM, data=fields(bb_client, **over))
    assert response.status_code == 200
    assert articles(bb_client) == []


def test_target_printer(bb_client):
    bb_client.post(FORM, data=fields(bb_client, target="printer:2"))
    art = articles(bb_client)[0]
    assert art.target_model is None and art.printer_id == 2


def test_checkboxes_and_parts(bb_client):
    from reprintbuddy.domain.catalog import PartInput, create_part

    with sm_of(bb_client).begin() as s:
        insert = create_part(s, PartInput(name="Insert M4"), 10, "user").id
        hex_key = create_part(s, PartInput(name="Hex key 2"), 10, "user").id
    page = bb_client.get(FORM).text
    offered = re.search(r'<select[^>]*name="parts_part"[^>]*>(.*?)</select>', page, re.S).group(1)
    assert f'<option value="{insert}"' in offered and f'<option value="{hex_key}"' in offered
    bb_client.post(FORM, data=fields(bb_client, needs_assembly="on", auto_enabled="on", has_parts="on",
                                     parts_part=[str(insert), ""], parts_qty=["2", ""]))
    art = articles(bb_client)[0]
    assert art.needs_assembly and art.auto_enabled
    from reprintbuddy.models import ArticlePart

    with sm_of(bb_client).begin() as s:
        links = {l.part_id: l.qty_per_unit for l in s.scalars(select(ArticlePart))}
    assert links == {insert: 2}


def test_bad_part_quantity(bb_client):
    from reprintbuddy.domain.catalog import PartInput, create_part

    with sm_of(bb_client).begin() as s:
        insert = create_part(s, PartInput(name="Insert M4"), 0, "user").id
    response = bb_client.post(FORM, data=fields(bb_client, has_parts="on", parts_part=[str(insert)], parts_qty=["abc"]))
    assert response.status_code == 200 and articles(bb_client) == []


def test_create_requires_csrf(bb_client):
    data = fields(bb_client)
    del data["csrf_token"]
    assert bb_client.post(FORM, data=data).status_code == 403


def test_no_bambuddy_write_calls(bb_client, fake_bambuddy):
    bb_client.get("/articles/new")
    bb_client.get("/articles/new?folder=1")
    bb_client.get("/articles/new/file/1")
    bb_client.post(FORM, data=fields(bb_client))
    assert fake_bambuddy.calls  # something was read
    assert "enqueue" not in fake_bambuddy.calls
