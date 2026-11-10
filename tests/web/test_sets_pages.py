import re

from sqlalchemy import func, select

from conftest import csrf_of, form_token_of, sm_of
from factories import make_article
from reprintbuddy import settings as st
from reprintbuddy.domain import catalog
from reprintbuddy.domain.catalog import PositionInput, SetInput
from reprintbuddy.models import ProductSet, SetPosition
from reprintbuddy.web.i18n import translate


def en(key, **params):
    return translate("en", key, **params)


def three_articles(client, a=3, b=3, c=5):
    with sm_of(client).begin() as s:
        ids = (
            make_article(s, name="Template A", stock=a).id,
            make_article(s, name="Template B", stock=b).id,
            make_article(s, name="Template C", stock=c).id,
        )
    return ids


def set_fields(client, a, b, c, **over):
    data = {
        "csrf_token": csrf_of(client, "/sets/new"),
        "form_token": form_token_of(client, "/sets/new", "/sets/new"),
        "name": "Starter Set", "sku": "S-1", "rows": "5",
        "pos_0_qty": "1", "pos_0_articles": [str(a)],
        "pos_1_qty": "2", "pos_1_articles": [str(b), str(c)],
    }
    data.update(over)
    return data


def sets_in_db(client):
    with sm_of(client).begin() as s:
        return list(s.scalars(select(ProductSet)))


def test_sets_empty(ready_client):
    page = ready_client.get("/sets")
    assert page.status_code == 200 and en("sets.empty") in page.text


def test_create_set_fixed_and_choice(ready_client):
    a, b, c = three_articles(ready_client)
    response = ready_client.post("/sets/new", data=set_fields(ready_client, a, b, c))
    assert response.status_code == 303 and response.headers["location"] == "/sets"
    page = ready_client.get("/sets").text
    assert "Starter Set" in page and "S-1" in page
    assert "1 × Template A" in page and "2 × (Template B | Template C)" in page
    # v0.3.16 p173 (Spec 5.4): a choice counts its smallest option, not the sum of the options.
    assert '<td class="num">1</td>' in page  # min(3, min(3 // 2, 5 // 2))


def test_negative_available_is_red(ready_client):
    with sm_of(ready_client).begin() as s:
        a = make_article(s, name="Template A", stock=0, reserved=1).id
        catalog.create_set(s, SetInput("Starter Set", None, [PositionInput(1, [a])]))
    assert '<td class="num neg">-1</td>' in ready_client.get("/sets").text


def test_more_rows_keeps_values(ready_client):
    a, b, c = three_articles(ready_client)
    response = ready_client.post("/sets/new", data=set_fields(ready_client, a, b, c, action="more"))
    assert response.status_code == 200
    assert 'name="pos_9_qty"' in response.text and 'name="pos_10_qty"' not in response.text
    assert 'value="Starter Set"' in response.text
    assert re.search(rf'<option value="{b}" selected>', response.text)  # selections stay
    assert sets_in_db(ready_client) == []


def test_set_validation_message(ready_client):
    a, b, c = three_articles(ready_client)
    data = set_fields(ready_client, a, b, c, pos_0_articles=[], pos_1_articles=[])
    response = ready_client.post("/sets/new", data=data)
    assert response.status_code == 200 and en("sets.no_positions") in response.text
    assert sets_in_db(ready_client) == []


def test_invalid_numbers_no_500(ready_client):
    a, b, c = three_articles(ready_client)
    for over in ({"pos_0_qty": "abc"}, {"pos_0_qty": "0"}, {"pos_0_qty": ""}, {"pos_0_qty": "1.5"},
                 {"pos_0_articles": ["x"]}, {"pos_0_articles": ["99999999999999999999"]},
                 {"name": " "}):
        response = ready_client.post("/sets/new", data=set_fields(ready_client, a, b, c, **over))
        assert response.status_code == 200, over
    assert sets_in_db(ready_client) == []


def test_bad_row_count_falls_back_to_default(ready_client):
    a, b, c = three_articles(ready_client)
    for rows, sku in (("abc", "S-1"), ("-4", "S-2"), ("999999", "S-3")):
        response = ready_client.post("/sets/new", data=set_fields(ready_client, a, b, c, rows=rows, sku=sku))
        assert response.status_code == 303, rows
    assert len(sets_in_db(ready_client)) == 3


def test_unknown_article_in_position(ready_client):
    a, b, c = three_articles(ready_client)
    response = ready_client.post("/sets/new", data=set_fields(ready_client, a, b, c, pos_0_articles=["9999"]))
    assert response.status_code == 200 and en("articles.not_found") in response.text


def test_set_double_submit(ready_client):
    a, b, c = three_articles(ready_client)
    data = set_fields(ready_client, a, b, c)
    assert ready_client.post("/sets/new", data=data).status_code == 303
    assert ready_client.post("/sets/new", data=data).status_code == 303
    assert len(sets_in_db(ready_client)) == 1
    assert en("form.duplicate") in ready_client.get("/sets").text


def test_edit_set(ready_client):
    a, b, c = three_articles(ready_client)
    ready_client.post("/sets/new", data=set_fields(ready_client, a, b, c))
    set_id = sets_in_db(ready_client)[0].id
    page = ready_client.get(f"/sets/{set_id}/edit").text
    assert 'value="Starter Set"' in page and re.search(rf'<option value="{c}" selected>', page)
    response = ready_client.post(
        f"/sets/{set_id}/edit",
        data={"csrf_token": csrf_of(ready_client, f"/sets/{set_id}/edit"), "name": "Starter Set 2", "sku": "",
              "rows": "5", "pos_0_qty": "3", "pos_0_articles": [str(b)]},
    )
    assert response.status_code == 303 and response.headers["location"] == "/sets"
    row = None
    with sm_of(ready_client).begin() as s:
        row = catalog.list_sets(s)[0]
        assert s.scalar(select(func.count()).select_from(SetPosition)) == 1
    assert (row.name, row.sku, row.available) == ("Starter Set 2", None, 1)


def test_edit_unknown_set(ready_client):
    assert ready_client.get("/sets/999/edit").status_code == 404


def test_edit_validation_keeps_form(ready_client):
    a, b, c = three_articles(ready_client)
    ready_client.post("/sets/new", data=set_fields(ready_client, a, b, c))
    set_id = sets_in_db(ready_client)[0].id
    response = ready_client.post(
        f"/sets/{set_id}/edit",
        data={"csrf_token": csrf_of(ready_client, "/sets"), "name": "Changed", "sku": "", "rows": "5"},
    )
    assert response.status_code == 200 and en("sets.no_positions") in response.text
    assert sets_in_db(ready_client)[0].name == "Starter Set"


def test_sets_requires_csrf(ready_client):
    a, b, c = three_articles(ready_client)
    data = set_fields(ready_client, a, b, c)
    del data["csrf_token"]
    assert ready_client.post("/sets/new", data=data).status_code == 403
    assert ready_client.post("/sets/1/edit", data={"name": "x"}).status_code == 403


def test_sets_german(ready_client):
    three_articles(ready_client)
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
        catalog.create_set(s, SetInput("Starter Set", None, [PositionInput(1, [1])]))
    page = ready_client.get("/sets").text
    assert "Sets" in page and "Verfügbar" in page


def test_nav_sets_active(ready_client):
    assert 'class="nav-link active" href="/sets"' in ready_client.get("/sets").text


def _first_button(html, action):
    form = re.search(rf'<form[^>]*action="{re.escape(action)}"[^>]*>(.*?)</form>', html, re.S).group(1)
    return re.search(r"<button[^>]*>", form).group(0)


def test_enter_in_a_field_saves_instead_of_adding_rows(ready_client):
    """The browser's Enter key presses the first submit button of the form; that must be Save."""
    a, b, c = three_articles(ready_client)
    new_form = _first_button(ready_client.get("/sets/new").text, "/sets/new")
    assert 'name="action"' not in new_form and 'type="submit"' in new_form
    ready_client.post("/sets/new", data=set_fields(ready_client, a, b, c))
    set_id = sets_in_db(ready_client)[0].id
    edit_form = _first_button(ready_client.get(f"/sets/{set_id}/edit").text, f"/sets/{set_id}/edit")
    assert 'name="action"' not in edit_form
