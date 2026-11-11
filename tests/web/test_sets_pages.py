"""The set form: base parts and choice parts per row in a drop-down (Spec 5.4, p340)."""

import re

from sqlalchemy import func, select

from conftest import csrf_of, form_token_of, sm_of
from factories import make_article, make_set
from reprintbuddy import settings as st
from reprintbuddy.domain import catalog
from reprintbuddy.domain.catalog import BaseInput, ChoiceInput, ChoiceOptionInput, SetInput
from reprintbuddy.models import Article, ProductSet, SetPosition, utcnow
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
    """Template A as base part, B and C as choice parts 1 with quantity 2."""
    data = {
        "csrf_token": csrf_of(client, "/sets/new"),
        "form_token": form_token_of(client, "/sets/new", "/sets/new"),
        "name": "Starter Set", "sku": "S-1", "base_rows": "3", "c1_rows": "3",
        "base_0_qty": "1", "base_0_article": str(a),
        "c1_qty": "2", "c1_0_article": str(b), "c1_0_suffix": "-B", "c1_1_article": str(c), "c1_1_suffix": "-C",
    }
    data.update(over)
    return data


def sets_in_db(client):
    with sm_of(client).begin() as s:
        return list(s.scalars(select(ProductSet)))


def select_html(page: str, name: str) -> str:
    return re.search(rf'<select name="{name}"[^>]*>(.*?)</select>', page, re.S).group(1)


def test_sets_empty(ready_client):
    page = ready_client.get("/sets")
    assert page.status_code == 200 and en("sets.empty") in page.text


def test_new_set_form_has_drop_downs_per_row_not_a_multi_select(ready_client):
    three_articles(ready_client)
    page = ready_client.get("/sets/new").text
    assert "multiple" not in page
    assert 'name="base_2_article"' in page and 'name="base_3_article"' not in page  # 3 rows, compact
    assert 'name="c1_2_article"' in page and 'name="c1_2_suffix"' in page and 'name="c1_3_article"' not in page
    assert 'name="c2_0_article"' not in page  # choice parts 2 only on request
    assert 'name="action" value="add_c2"' in page and en("sets.add_choices_2") in page
    assert en("sets.base") in page and en("sets.choices_1") in page
    assert 'src="/static/js/set_form.js' in page


def test_create_set_with_base_part_and_choices(ready_client):
    a, b, c = three_articles(ready_client)
    response = ready_client.post("/sets/new", data=set_fields(ready_client, a, b, c))
    assert response.status_code == 303 and response.headers["location"] == "/sets"
    with sm_of(ready_client).begin() as s:
        row = catalog.list_sets(s)[0]
    assert [(v.sku, v.available) for v in row.variants] == [("S-1-B", 1), ("S-1-C", 2)]
    assert (row.available, [b.name for b in row.base], row.choices[0].qty) == (3, ["Template A"], 2)


def test_add_choices_2_shows_the_second_field_and_saves_nothing(ready_client):
    a, b, c = three_articles(ready_client)
    response = ready_client.post("/sets/new", data=set_fields(ready_client, a, b, c, action="add_c2"))
    assert response.status_code == 200
    assert 'name="c2_0_article"' in response.text and 'name="c2_2_suffix"' in response.text
    assert 'name="action" value="add_c2"' not in response.text
    assert 'value="-C"' in response.text and 'value="Starter Set"' in response.text
    assert sets_in_db(ready_client) == []


def test_create_set_with_two_choice_fields(ready_client):
    with sm_of(ready_client).begin() as s:
        ids = [make_article(s, name=n, stock=4).id for n in ("Frame", "Plate S", "Plate L", "Red", "Blue")]
    frame, small, large, red, blue = ids
    data = set_fields(ready_client, frame, small, large, c1_qty="1", c2_shown="1", c2_rows="3", c2_qty="1",
                      c1_0_suffix="-S", c1_1_suffix="-L",
                      c2_0_article=str(red), c2_0_suffix="-1", c2_1_article=str(blue), c2_1_suffix="-2")
    assert ready_client.post("/sets/new", data=data).status_code == 303
    with sm_of(ready_client).begin() as s:
        assert [v.sku for v in catalog.list_sets(s)[0].variants] == ["S-1-S-1", "S-1-S-2", "S-1-L-1", "S-1-L-2"]


def test_more_rows_per_section_keeps_what_was_typed(ready_client):
    a, b, c = three_articles(ready_client)
    response = ready_client.post("/sets/new", data=set_fields(ready_client, a, b, c, action="more_base"))
    assert response.status_code == 200
    assert 'name="base_5_qty"' in response.text and 'name="base_6_qty"' not in response.text
    assert 'name="c1_2_article"' in response.text and 'name="c1_3_article"' not in response.text
    assert re.search(rf'<option value="{b}" selected>', select_html(response.text, "c1_0_article"))
    response = ready_client.post("/sets/new", data=set_fields(ready_client, a, b, c, action="more_c1"))
    assert 'name="c1_5_suffix"' in response.text and 'name="base_3_qty"' not in response.text
    assert sets_in_db(ready_client) == []


def test_article_chosen_in_one_row_is_not_offered_in_the_others(ready_client):
    a, b, c = three_articles(ready_client)
    page = ready_client.post("/sets/new", data=set_fields(ready_client, a, b, c, action="more_base")).text
    assert re.search(rf'<option value="{a}" selected>', select_html(page, "base_0_article"))
    for other in ("base_1_article", "c1_2_article"):
        assert re.search(rf'<option value="{a}" disabled hidden>', select_html(page, other)), other
        assert re.search(rf'<option value="{b}" disabled hidden>', select_html(page, other)), other
    assert re.search(rf'<option value="{c}" disabled hidden>', select_html(page, "c1_0_article"))


def test_duplicate_article_without_script_shows_its_name(ready_client):  # review focus 4
    a, b, c = three_articles(ready_client)
    data = set_fields(ready_client, a, b, c, base_1_qty="1", base_1_article=str(a))
    response = ready_client.post("/sets/new", data=data)
    assert response.status_code == 200 and en("sets.article_twice", name="Template A") in response.text
    assert 'value="-B"' in response.text and 'value="Starter Set"' in response.text
    assert sets_in_db(ready_client) == []


def test_set_validation_message(ready_client):
    a, b, c = three_articles(ready_client)
    data = set_fields(ready_client, a, b, c, base_0_article="", c1_0_article="", c1_1_article="")
    response = ready_client.post("/sets/new", data=data)
    assert response.status_code == 200 and en("sets.too_few_articles") in response.text
    assert sets_in_db(ready_client) == []


def test_invalid_numbers_no_500(ready_client):
    a, b, c = three_articles(ready_client)
    for over in ({"base_0_qty": "abc"}, {"base_0_qty": "0"}, {"base_0_qty": ""}, {"base_0_qty": "1.5"},
                 {"c1_qty": "x"}, {"base_0_article": "x"}, {"c1_0_article": "99999999999999999999"},
                 {"name": " "}):
        response = ready_client.post("/sets/new", data=set_fields(ready_client, a, b, c, **over))
        assert response.status_code == 200, over
    assert sets_in_db(ready_client) == []


def test_bad_row_count_falls_back_to_default(ready_client):
    a, b, c = three_articles(ready_client)
    for rows, sku in (("abc", "S-1"), ("-4", "S-2"), ("999999", "S-3")):
        data = set_fields(ready_client, a, b, c, base_rows=rows, c1_rows=rows, sku=sku)
        assert ready_client.post("/sets/new", data=data).status_code == 303, rows
    assert len(sets_in_db(ready_client)) == 3


def test_unknown_article_in_a_row(ready_client):
    a, b, c = three_articles(ready_client)
    response = ready_client.post("/sets/new", data=set_fields(ready_client, a, b, c, base_0_article="9999"))
    assert response.status_code == 200 and en("articles.not_found") in response.text


def test_set_double_submit(ready_client):
    a, b, c = three_articles(ready_client)
    data = set_fields(ready_client, a, b, c)
    assert ready_client.post("/sets/new", data=data).status_code == 303
    assert ready_client.post("/sets/new", data=data).status_code == 303
    assert len(sets_in_db(ready_client)) == 1
    assert en("form.duplicate") in ready_client.get("/sets").text


def test_edit_form_shows_base_parts_choices_and_suffixes(ready_client):
    a, b, c = three_articles(ready_client)
    with sm_of(ready_client).begin() as s:
        d = make_article(s, name="Template D", stock=2).id
        set_id = catalog.create_set(s, SetInput("Kit", "K", [BaseInput(2, a)], [
            ChoiceInput(1, [ChoiceOptionInput(b, "-B")]), ChoiceInput(3, [ChoiceOptionInput(c, "-C"), ChoiceOptionInput(d, "-D")]),
        ])).id
    page = ready_client.get(f"/sets/{set_id}/edit").text
    assert re.search(rf'<option value="{a}" selected>', select_html(page, "base_0_article"))
    assert 'name="base_0_qty" inputmode="numeric" min="1" value="2"' in page
    assert re.search(rf'<option value="{d}" selected>', select_html(page, "c2_1_article"))
    assert 'name="c2_1_suffix"' in page and 'value="-D"' in page and 'name="c2_qty" inputmode="numeric" min="1" value="3"' in page
    assert 'name="action" value="add_c2"' not in page


def test_edit_without_second_field_offers_to_add_it(ready_client):
    a, b, c = three_articles(ready_client)
    ready_client.post("/sets/new", data=set_fields(ready_client, a, b, c))
    page = ready_client.get(f"/sets/{sets_in_db(ready_client)[0].id}/edit").text
    assert 'name="c2_0_article"' not in page and 'name="action" value="add_c2"' in page


def test_edit_set(ready_client):
    a, b, c = three_articles(ready_client)
    ready_client.post("/sets/new", data=set_fields(ready_client, a, b, c))
    set_id = sets_in_db(ready_client)[0].id
    response = ready_client.post(
        f"/sets/{set_id}/edit",
        data={"csrf_token": csrf_of(ready_client, f"/sets/{set_id}/edit"), "name": "Starter Set 2", "sku": "",
              "base_rows": "3", "c1_rows": "3", "c1_qty": "1",
              "base_0_qty": "3", "base_0_article": str(b), "base_1_qty": "1", "base_1_article": str(c)},
    )
    assert response.status_code == 303 and response.headers["location"] == "/sets"
    with sm_of(ready_client).begin() as s:
        row = catalog.list_sets(s)[0]
        assert s.scalar(select(func.count()).select_from(SetPosition)) == 2
    assert (row.name, row.sku, row.available, row.choices) == ("Starter Set 2", None, 1, [])


def test_migrated_set_opens_with_empty_suffixes_and_saving_requires_them(ready_client):  # review focus 5
    a, b, c = three_articles(ready_client)
    with sm_of(ready_client).begin() as s:
        set_id = make_set(s, "Kit", [(1, a)], [(1, [(b, None), (c, None)])], sku="K").id
    edit = f"/sets/{set_id}/edit"
    page = ready_client.get(edit).text
    assert 'name="c1_0_suffix" value=""' in page and 'name="c1_1_suffix" value=""' in page
    data = {"csrf_token": csrf_of(ready_client, edit), "name": "Kit", "sku": "K", "base_rows": "3", "c1_rows": "3",
            "base_0_qty": "1", "base_0_article": str(a), "c1_qty": "1",
            "c1_0_article": str(b), "c1_0_suffix": "", "c1_1_article": str(c), "c1_1_suffix": ""}
    response = ready_client.post(edit, data=data)
    assert response.status_code == 200 and en("sets.suffix_required", name="Template B") in response.text
    response = ready_client.post(edit, data={**data, "c1_0_suffix": "-B", "c1_1_suffix": "-C"})
    assert response.status_code == 303
    with sm_of(ready_client).begin() as s:
        assert catalog.list_sets(s)[0].orderable


def test_archived_choice_article_stays_in_the_form_marked(ready_client):
    a, b, c = three_articles(ready_client)
    ready_client.post("/sets/new", data=set_fields(ready_client, a, b, c))
    with sm_of(ready_client).begin() as s:
        s.get(Article, c).archived_at = utcnow()
    page = ready_client.get(f"/sets/{sets_in_db(ready_client)[0].id}/edit").text
    chosen = select_html(page, "c1_1_article")
    assert re.search(rf'<option value="{c}" selected>Template C \({en("catalog.archived_badge")}\)', chosen)
    assert f'<option value="{c}"' not in select_html(page, "c1_2_article")  # not offered for a new row


def test_edit_unknown_set(ready_client):
    assert ready_client.get("/sets/999/edit").status_code == 404


def test_edit_validation_keeps_form(ready_client):
    a, b, c = three_articles(ready_client)
    ready_client.post("/sets/new", data=set_fields(ready_client, a, b, c))
    set_id = sets_in_db(ready_client)[0].id
    response = ready_client.post(
        f"/sets/{set_id}/edit",
        data={"csrf_token": csrf_of(ready_client, "/sets"), "name": "Changed", "sku": "", "base_rows": "3"},
    )
    assert response.status_code == 200 and en("sets.too_few_articles") in response.text
    assert sets_in_db(ready_client)[0].name == "Starter Set"


def test_sets_requires_csrf(ready_client):
    a, b, c = three_articles(ready_client)
    data = set_fields(ready_client, a, b, c)
    del data["csrf_token"]
    assert ready_client.post("/sets/new", data=data).status_code == 403
    assert ready_client.post("/sets/1/edit", data={"name": "x"}).status_code == 403


def test_sets_german(ready_client):
    a, b, _c = three_articles(ready_client)
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
        catalog.create_set(s, SetInput("Starter Set", None, [BaseInput(1, a), BaseInput(1, b)], []))
    page = ready_client.get("/sets").text
    assert "Sets" in page and "Verfügbar" in page
    form = ready_client.get("/sets/new").text
    assert "Basisteile" in form and "Wahlteile 1" in form and "Wahlteile 2 hinzufügen" in form
    assert "Aufklappliste" in form


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
