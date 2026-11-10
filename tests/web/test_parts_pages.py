import re

from sqlalchemy import func, select

from conftest import csrf_of, form_token_of, sm_of
from reprintbuddy import settings as st
from reprintbuddy.domain import catalog
from reprintbuddy.domain.errors import ValidationError
from reprintbuddy.models import FormToken, Part
from reprintbuddy.web.forms import error_text
from reprintbuddy.web.i18n import translate


def en(key, **params):
    return translate("en", key, **params)


def set_language(client, lang):
    with sm_of(client).begin() as s:
        st.set_setting(s, "language", lang, commit=False)


def part_fields(client, **over):
    fields = {
        "csrf_token": csrf_of(client, "/parts/new"),
        "form_token": form_token_of(client, "/parts/new", "/parts/new"),
        "name": "Insert M4", "unit": "pcs", "initial_stock": "50", "min_stock": "0",
        "pack_size": "50", "min_order_packs": "1", "lead_time_days": "5",
        "safety_value": "10", "safety_unit": "qty", "supplier": "Example Supplier",
        "order_url": "https://shop.example/insert-m4",
    }
    fields.update(over)
    return fields


def parts_in_db(client):
    with sm_of(client).begin() as s:
        return list(s.scalars(select(Part)))


def suppliers_in_db(client, part_id):
    with sm_of(client).begin() as s:
        return [
            (r.position, r.name, r.order_url, r.pack_size, r.min_order_packs, r.lead_time_days, r.is_default)
            for r in catalog.part_suppliers(s, part_id)
        ]


def two_suppliers(client, **over):
    """The form with two suppliers: each supplier field is sent once per supplier, in order."""
    fields = part_fields(client)
    fields.update({
        "supplier": ["Example Supplier", "Example Supplier B"],
        "order_url": ["https://shop.example/insert-m4", ""],
        "pack_size": ["50", "10"], "min_order_packs": ["1", "2"], "lead_time_days": ["5", "1"],
        "supplier_default": "0",
    })
    fields.update(over)
    return fields


def blocks(page):
    """The supplier blocks of a form page: their name fields (not the one of the <template>)."""
    return re.findall(r'<input id="supplier-\d+-name"[^>]*name="supplier"[^>]*>', page)


def test_parts_list_empty(ready_client):
    response = ready_client.get("/parts")
    assert response.status_code == 200
    assert en("parts.empty") in response.text


def test_create_part(ready_client):
    response = ready_client.post("/parts/new", data=part_fields(ready_client))
    assert response.status_code == 303 and response.headers["location"] == "/parts"
    page = ready_client.get("/parts")
    assert "Insert M4" in page.text and "50" in page.text
    assert en("parts.created", name="Insert M4") in page.text
    assert en("parts.created", name="Insert M4") not in ready_client.get("/parts").text  # shown once


def test_create_part_invalid(ready_client):
    response = ready_client.post("/parts/new", data=part_fields(ready_client, pack_size="0"))
    assert response.status_code == 200
    expected = error_text(
        "en", ValidationError("form.invalid_number", field="parts.field.pack_size", min=1, max=1_000_000)
    )
    assert expected in response.text
    assert 'value="Insert M4"' in response.text  # input stays
    assert parts_in_db(ready_client) == []


def test_create_part_double_submit(ready_client):
    fields = part_fields(ready_client)
    first = ready_client.post("/parts/new", data=fields)
    second = ready_client.post("/parts/new", data=fields)
    assert first.status_code == 303 and second.status_code == 303
    assert len(parts_in_db(ready_client)) == 1
    assert en("form.duplicate") in ready_client.get("/parts").text


def test_invalid_submit_keeps_token_usable(ready_client):
    fields = part_fields(ready_client, pack_size="0")
    assert ready_client.post("/parts/new", data=fields).status_code == 200
    fields["pack_size"] = "50"
    assert ready_client.post("/parts/new", data=fields).status_code == 303
    assert len(parts_in_db(ready_client)) == 1


def test_forged_token_rejected(ready_client):
    response = ready_client.post("/parts/new", data=part_fields(ready_client, form_token="nonsense"))
    assert response.status_code == 400
    assert en("form.expired") in response.text
    assert parts_in_db(ready_client) == []


def test_get_form_writes_no_token(ready_client):
    assert ready_client.get("/parts/new").status_code == 200
    with sm_of(ready_client).begin() as s:
        assert s.scalar(select(func.count()).select_from(FormToken)) == 0


def test_edit_part_keeps_stock(ready_client):
    ready_client.post("/parts/new", data=part_fields(ready_client))
    part = parts_in_db(ready_client)[0]
    page = ready_client.get(f"/parts/{part.id}/edit")
    assert page.status_code == 200 and en("parts.stock_via_bookings") in page.text
    assert 'name="initial_stock"' not in page.text
    response = ready_client.post(
        f"/parts/{part.id}/edit",
        data={
            "csrf_token": csrf_of(ready_client, f"/parts/{part.id}/edit"),
            "name": "Insert M4 long", "unit": "pcs", "min_stock": "5", "pack_size": "100",
            "min_order_packs": "1", "lead_time_days": "5", "safety_value": "10",
            "safety_unit": "days", "supplier": "", "order_url": "",
        },
    )
    assert response.status_code == 303
    changed = parts_in_db(ready_client)[0]
    assert (changed.name, changed.stock, changed.safety_unit) == ("Insert M4 long", 50, "days")
    assert suppliers_in_db(ready_client, changed.id) == [(0, None, None, 100, 1, 5, True)]


def test_edit_part_invalid_and_unknown(ready_client):
    assert ready_client.get("/parts/999/edit").status_code == 404
    ready_client.post("/parts/new", data=part_fields(ready_client))
    part = parts_in_db(ready_client)[0]
    response = ready_client.post(
        f"/parts/{part.id}/edit",
        data={"csrf_token": csrf_of(ready_client, "/parts"), "name": "x", "unit": "pcs", "min_stock": "0",
              "pack_size": "abc", "min_order_packs": "1", "lead_time_days": "0", "safety_value": "0",
              "safety_unit": "days"},
    )
    assert response.status_code == 200
    assert parts_in_db(ready_client)[0].name == "Insert M4"


def test_parts_posts_require_csrf(ready_client):
    fields = part_fields(ready_client)
    del fields["csrf_token"]
    assert ready_client.post("/parts/new", data=fields).status_code == 403
    assert ready_client.post("/parts/1/edit", data={"name": "x"}).status_code == 403


def test_invalid_numbers_no_500(ready_client):
    for field in ("initial_stock", "min_stock", "pack_size", "min_order_packs", "lead_time_days", "safety_value"):
        for bad in ("", "abc", "-1", "1.5", "1e9", "9" * 20):
            response = ready_client.post("/parts/new", data=part_fields(ready_client, **{field: bad}))
            assert response.status_code == 200, (field, bad)
    assert parts_in_db(ready_client) == []


def test_bad_url_and_unit(ready_client):
    for over in ({"order_url": "ftp://x"}, {"safety_unit": "weeks"}, {"unit": ""}):
        assert ready_client.post("/parts/new", data=part_fields(ready_client, **over)).status_code == 200
    assert parts_in_db(ready_client) == []


def test_parts_german(ready_client):
    set_language(ready_client, "de")
    page = ready_client.get("/parts/new").text
    for word in ("Bestelleinheit", "Mindestbestellmenge", "Sicherheitspolster", "Lieferant", "Bestell-Link"):
        assert word in page, word
    assert "Zukaufteile" in ready_client.get("/parts").text


def test_nav_parts_active(ready_client):
    assert 'class="nav-link active" href="/parts"' in ready_client.get("/parts").text


# ---- several suppliers (Spec 5.3) ----------------------------------------------------------


def test_create_a_part_with_two_suppliers_and_the_second_as_default(ready_client):
    response = ready_client.post("/parts/new", data=two_suppliers(ready_client, supplier_default="1"))
    assert response.status_code == 303
    part = parts_in_db(ready_client)[0]
    assert suppliers_in_db(ready_client, part.id) == [
        (0, "Example Supplier", "https://shop.example/insert-m4", 50, 1, 5, False),
        (1, "Example Supplier B", None, 10, 2, 1, True),
    ]


def test_a_single_supplier_is_the_default_without_choosing(ready_client):
    fields = part_fields(ready_client)
    fields.pop("supplier_default", None)
    assert ready_client.post("/parts/new", data=fields).status_code == 303
    assert suppliers_in_db(ready_client, parts_in_db(ready_client)[0].id)[0][-1] is True


def test_two_suppliers_without_a_default_are_refused(ready_client):
    fields = two_suppliers(ready_client)
    del fields["supplier_default"]
    response = ready_client.post("/parts/new", data=fields)
    assert response.status_code == 200
    assert en("parts.supplier_default") in response.text
    assert len(blocks(response.text)) == 2 and 'value="Example Supplier B"' in response.text  # input stays
    assert parts_in_db(ready_client) == []


def test_an_error_names_the_supplier(ready_client):
    response = ready_client.post("/parts/new", data=two_suppliers(ready_client, pack_size=["50", "0"]))
    assert response.status_code == 200
    inner = error_text("en", ValidationError("form.invalid_number", field="parts.field.pack_size", min=1, max=1_000_000))
    assert en("parts.supplier_error", n=2, error=inner) in response.text
    assert parts_in_db(ready_client) == []


def test_adding_a_supplier_without_javascript_saves_nothing(ready_client):
    fields = part_fields(ready_client)
    fields["suppliers_add"] = "1"
    response = ready_client.post("/parts/new", data=fields)
    assert response.status_code == 200
    assert len(blocks(response.text)) == 2
    assert 'value="Example Supplier"' in response.text  # what was typed stays
    assert parts_in_db(ready_client) == []
    fields.pop("suppliers_add")
    assert ready_client.post("/parts/new", data=fields).status_code == 303  # the token still works


def test_removing_a_supplier_without_javascript(ready_client):
    response = ready_client.post("/parts/new", data=two_suppliers(ready_client, suppliers_remove="0", supplier_default="1"))
    assert response.status_code == 200
    assert len(blocks(response.text)) == 1
    assert 'value="Example Supplier B"' in response.text and 'value="Example Supplier"' not in response.text
    assert re.search(r'name="supplier_default" value="0"[^>]*checked', response.text)  # B stays the default
    assert parts_in_db(ready_client) == []


def test_removing_the_default_leaves_none_chosen(ready_client):
    page = ready_client.post("/parts/new", data=two_suppliers(ready_client, suppliers_remove="0")).text
    assert len(blocks(page)) == 1
    assert not re.search(r'name="supplier_default"[^>]*checked', page)


def test_the_last_supplier_cannot_be_removed(ready_client):
    fields = part_fields(ready_client)
    page = ready_client.get("/parts/new").text
    assert len(blocks(page)) == 1
    assert re.search(r'<button[^>]*name="suppliers_remove"[^>]*disabled', page)
    fields["suppliers_remove"] = "0"
    assert len(blocks(ready_client.post("/parts/new", data=fields).text)) == 1


def test_at_most_ten_suppliers(ready_client):
    ten = {key: [value] * 10 for key, value in (
        ("supplier", "Example Supplier"), ("order_url", ""), ("pack_size", "1"),
        ("min_order_packs", "1"), ("lead_time_days", "0"),
    )}
    page = ready_client.post("/parts/new", data=part_fields(ready_client, suppliers_add="1", supplier_default="0", **ten)).text
    assert len(blocks(page)) == 10
    assert re.search(r'<button[^>]*name="suppliers_add"[^>]*disabled', page)


def test_the_edit_form_shows_every_supplier_and_the_default(ready_client):
    ready_client.post("/parts/new", data=two_suppliers(ready_client, supplier_default="1"))
    part = parts_in_db(ready_client)[0]
    page = ready_client.get(f"/parts/{part.id}/edit").text
    assert len(blocks(page)) == 2
    assert re.search(r'name="supplier_default" value="1"[^>]*checked', page)
    assert 'value="https://shop.example/insert-m4"' in page


def test_saving_the_edit_form_with_one_supplier_less(ready_client):
    ready_client.post("/parts/new", data=two_suppliers(ready_client))
    part = parts_in_db(ready_client)[0]
    fields = part_fields(ready_client, csrf_token=csrf_of(ready_client, f"/parts/{part.id}/edit"), supplier_default="0")
    del fields["form_token"], fields["initial_stock"]
    assert ready_client.post(f"/parts/{part.id}/edit", data=fields).status_code == 303
    assert suppliers_in_db(ready_client, part.id) == [(0, "Example Supplier", "https://shop.example/insert-m4", 50, 1, 5, True)]


def test_names_in_use_are_offered_while_typing(ready_client):
    ready_client.post("/parts/new", data=two_suppliers(ready_client))
    page = ready_client.get("/parts/new").text
    datalist = re.search(r'<datalist id="supplier-names">(.*?)</datalist>', page, re.S).group(1)
    assert re.findall(r'<option value="([^"]+)">', datalist) == ["Example Supplier", "Example Supplier B"]
    assert re.search(r'<input[^>]*name="supplier"[^>]*list="supplier-names"', page)


def test_supplier_fields_in_german(ready_client):
    set_language(ready_client, "de")
    page = ready_client.get("/parts/new").text
    for word in ("Lieferanten", "Lieferant 1", "Standard", "Lieferant hinzufügen", "Entfernen"):
        assert word in page, word


def test_the_form_brings_its_script_and_enter_saves(ready_client):
    from pathlib import Path

    from reprintbuddy import __version__
    from reprintbuddy.web import i18n as web_i18n

    page = ready_client.get("/parts/new").text
    assert f'<script src="/static/js/suppliers.js?v={__version__}" defer></script>' in page
    assert ready_client.get(f"/static/js/suppliers.js?v={__version__}").status_code == 200
    form = re.search(r'<form method="post" action="/parts/new".*?</form>', page, re.S).group(0)
    first = re.search(r"<button[^>]*>", form).group(0)  # the one Enter presses: a plain save
    assert 'type="submit"' in first and "name=" not in first and 'tabindex="-1"' in first
    script = (Path(web_i18n.__file__).parent / "static" / "js" / "suppliers.js").read_text(encoding="utf-8")
    assert "data-changed" in script  # a removed block changes no field; live.js must not undo it


def test_eleven_suppliers_are_refused_not_cut_off(ready_client):
    eleven = {key: [value] * 11 for key, value in (
        ("supplier", "Example Supplier"), ("order_url", ""), ("pack_size", "1"),
        ("min_order_packs", "1"), ("lead_time_days", "0"),
    )}
    response = ready_client.post("/parts/new", data=part_fields(ready_client, supplier_default="0", **eleven))
    assert response.status_code == 200
    assert en("parts.suppliers_count", max=10) in response.text
    assert parts_in_db(ready_client) == []



def test_the_supplier_name_field_is_labelled_name(ready_client):  # review R8, Spec 5.3
    page = ready_client.get("/parts/new").text
    assert re.search(r'<label for="supplier-0-name">Name</label>', page)
