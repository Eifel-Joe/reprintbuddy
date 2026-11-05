from sqlalchemy import func, select

from conftest import csrf_of, form_token_of, sm_of
from reprintbuddy import settings as st
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
    assert (changed.name, changed.pack_size, changed.stock, changed.safety_unit) == ("Insert M4 long", 100, 50, "days")


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
