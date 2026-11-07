"""The purchased parts of an article in the create and the edit form (Spec 5.2).

A checkbox "purchased parts needed" opens a list of only the parts in use: per row a part and
the quantity per unit, a button to remove the row, and a button to add a row. Without
JavaScript, adding and removing send the form and show it again, nothing is saved.
"""

import re
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

from conftest import csrf_of, form_token_of, sm_of
from factories import make_article, make_part
from reprintbuddy import __version__
from reprintbuddy import settings as st
from reprintbuddy.domain import catalog
from reprintbuddy.models import Article, ArticlePart, Part
from reprintbuddy.web import i18n
from reprintbuddy.web.i18n import translate

FORM = "/articles/new/file/1/plate/1"
SCRIPT = Path(i18n.__file__).parent / "static" / "js" / "article_parts.js"


def en(key, **params):
    return translate("en", key, **params)


def create_fields(client, **over):
    data = {
        "csrf_token": csrf_of(client, FORM),
        "form_token": form_token_of(client, FORM, FORM),
        "name": "Template A", "sku": "", "units_per_print": "1", "min_stock": "0", "buffer": "0",
        "target": "model:A1",
    }
    data.update(over)
    return data


def edit_fields(client, article_id, **over):
    data = {
        "csrf_token": csrf_of(client, f"/articles/{article_id}/edit"),
        "name": "Template A", "sku": "", "units_per_print": "1", "min_stock": "0", "buffer": "0",
        "target": "model:A1",
    }
    data.update(over)
    return data


def two_parts(client):
    with sm_of(client).begin() as s:
        return make_part(s, name="Insert M4").id, make_part(s, name="Hex key 2").id


def links(client):
    with sm_of(client).begin() as s:
        return {(l.article_id, l.part_id): l.qty_per_unit for l in s.scalars(select(ArticlePart))}


def article_count(client):
    with sm_of(client).begin() as s:
        return len(list(s.scalars(select(Article))))


def form_of(html):
    """The article form (the page also has the sign-out form in the menu)."""
    forms = [body for body in re.findall(r"<form[^>]*>(.*?)</form>", html, re.S) if 'name="units_per_print"' in body]
    assert len(forms) == 1
    return forms[0]


def rows_of(html):
    """The rows of the list as (chosen part id, quantity, offered part ids); the template row is left out."""
    block = re.sub(r"<template.*?</template>", "", form_of(html), flags=re.S)
    rows = []
    for row in re.findall(r'<div class="part-row"[^>]*>(.*?)</div>', block, re.S):
        select_ = re.search(r'<select[^>]*name="parts_part"[^>]*>(.*?)</select>', row, re.S).group(1)
        chosen = re.search(r'<option value="([^"]*)" selected', select_)
        offered = [v for v, attrs in re.findall(r'<option value="([^"]+)"([^>]*)>', select_) if "disabled" not in attrs]
        qty = re.search(r'<input[^>]*name="parts_qty"[^>]*value="([^"]*)"', row).group(1)
        rows.append((chosen.group(1) if chosen else "", qty, offered))
    return rows


def has_parts_checked(html):
    tag = re.search(r'<input[^>]*name="has_parts"[^>]*>', html).group(0)
    return " checked" in tag


# ---- the form -------------------------------------------------------------------------------


def test_create_form_has_the_checkbox_unticked_and_one_empty_row(bb_client):
    insert, hex_key = two_parts(bb_client)
    page = bb_client.get(FORM).text
    assert en("articles.field.has_parts") in page
    assert not has_parts_checked(page)
    assert rows_of(page) == [("", "", [str(hex_key), str(insert)])]  # sorted by name
    assert 'name="part_' not in page  # no longer one field per part


def test_the_list_offers_only_active_parts(bb_client):
    insert, hex_key = two_parts(bb_client)
    with sm_of(bb_client).begin() as s:
        catalog.archive_part(s, hex_key)
    page = bb_client.get(FORM).text
    assert rows_of(page) == [("", "", [str(insert)])]
    template = re.search(r"<template[^>]*>(.*?)</template>", page, re.S).group(1)
    assert f'value="{insert}"' in template and f'value="{hex_key}"' not in template


def test_without_parts_there_is_no_checkbox(bb_client):
    page = bb_client.get(FORM).text
    assert 'name="has_parts"' not in page


def test_the_block_brings_its_script_and_buttons(bb_client):
    two_parts(bb_client)
    page = bb_client.get(FORM).text
    assert f'<script src="/static/js/article_parts.js?v={__version__}" defer></script>' in page
    assert re.search(r'<button[^>]*name="parts_add"[^>]*formnovalidate', page)
    assert re.search(r'<button[^>]*name="parts_remove" value="0"[^>]*formnovalidate', page)
    assert en("articles.part_add") in page and en("articles.part_remove") in page
    assert bb_client.get(f"/static/js/article_parts.js?v={__version__}").status_code == 200


def test_enter_in_a_field_saves_and_does_not_add_or_remove(bb_client):
    """The first submit button of a form is the one Enter presses: it must be a plain save."""
    two_parts(bb_client)
    form = form_of(bb_client.get(FORM).text)
    first = re.search(r"<button[^>]*>", form).group(0)
    assert 'type="submit"' in first and "name=" not in first
    assert 'tabindex="-1"' in first and 'aria-hidden="true"' in first


def test_german_labels(bb_client):
    two_parts(bb_client)
    with sm_of(bb_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    page = bb_client.get(FORM).text
    assert "Zukaufteile nötig" in page and "Teil hinzufügen" in page


def test_the_script_marks_the_form_changed_for_the_live_channel():
    """A removed row changes no field: the script says so, so a live update does not undo it."""
    assert "data-changed" in SCRIPT.read_text(encoding="utf-8")
    live = (SCRIPT.parent / "live.js").read_text(encoding="utf-8")
    assert 'hasAttribute("data-changed")' in live


# ---- saving ---------------------------------------------------------------------------------


def test_create_with_parts(bb_client):
    insert, hex_key = two_parts(bb_client)
    response = bb_client.post(FORM, data=create_fields(
        bb_client, has_parts="on", parts_part=[str(insert), str(hex_key)], parts_qty=["2", "1"]))
    assert response.status_code == 303
    a = next(iter({a for a, _ in links(bb_client)}))
    assert links(bb_client) == {(a, insert): 2, (a, hex_key): 1}


def test_rows_without_the_checkbox_are_not_saved(bb_client):
    insert, _ = two_parts(bb_client)
    response = bb_client.post(FORM, data=create_fields(bb_client, parts_part=[str(insert)], parts_qty=["2"]))
    assert response.status_code == 303
    assert article_count(bb_client) == 1 and links(bb_client) == {}


def test_an_empty_row_is_ignored(bb_client):
    insert, _ = two_parts(bb_client)
    response = bb_client.post(FORM, data=create_fields(
        bb_client, has_parts="on", parts_part=["", str(insert), ""], parts_qty=["", "3", "1"]))
    assert response.status_code == 303
    assert list(links(bb_client).values()) == [3]


def test_the_same_part_twice_is_an_error(bb_client):
    insert, hex_key = two_parts(bb_client)
    response = bb_client.post(FORM, data=create_fields(
        bb_client, has_parts="on", parts_part=[str(insert), str(hex_key), str(insert)], parts_qty=["2", "1", "4"]))
    assert response.status_code == 200
    assert en("articles.part_twice", name="Insert M4") in response.text
    assert article_count(bb_client) == 0
    assert [(c, q) for c, q, _ in rows_of(response.text)] == [(str(insert), "2"), (str(hex_key), "1"), (str(insert), "4")]
    assert has_parts_checked(response.text)


def test_a_chosen_part_needs_a_quantity_of_at_least_one(bb_client):
    insert, _ = two_parts(bb_client)
    for qty in ("0", "", "abc", "1001"):
        response = bb_client.post(FORM, data=create_fields(
            bb_client, has_parts="on", parts_part=[str(insert)], parts_qty=[qty]))
        assert response.status_code == 200, qty
        assert en("articles.part_qty_invalid", name="Insert M4", min=1, max=1000) in response.text, qty
        assert article_count(bb_client) == 0


def test_an_unknown_part_is_an_error(bb_client):
    two_parts(bb_client)
    for part in ("999", "abc"):
        response = bb_client.post(FORM, data=create_fields(
            bb_client, has_parts="on", parts_part=[part], parts_qty=["1"]))
        assert response.status_code == 200, part
        assert en("parts.not_found") in response.text
        assert article_count(bb_client) == 0


# ---- adding and removing without JavaScript ---------------------------------------------


def test_add_without_javascript_shows_one_more_row_and_saves_nothing(bb_client):
    insert, hex_key = two_parts(bb_client)
    data = create_fields(bb_client, name="Typed name", has_parts="on", parts_part=[str(insert)], parts_qty=["2"], parts_add="1")
    response = bb_client.post(FORM, data=data)
    assert response.status_code == 200
    assert article_count(bb_client) == 0
    assert 'value="Typed name"' in response.text and has_parts_checked(response.text)
    rows = rows_of(response.text)
    assert rows == [(str(insert), "2", [str(hex_key), str(insert)]), ("", "", [str(hex_key)])]
    assert re.search(r'<select[^>]*name="parts_part"[^>]*autofocus', response.text)  # the new row
    # the one-time token was not used up: the same form can still be saved
    del data["parts_add"]
    assert bb_client.post(FORM, data=data).status_code == 303
    assert article_count(bb_client) == 1


def test_remove_without_javascript_drops_the_row_and_saves_nothing(bb_client):
    insert, hex_key = two_parts(bb_client)
    response = bb_client.post(FORM, data=create_fields(
        bb_client, has_parts="on", parts_part=[str(insert), str(hex_key)], parts_qty=["2", "1"], parts_remove="0"))
    assert response.status_code == 200
    assert article_count(bb_client) == 0
    assert [(c, q) for c, q, _ in rows_of(response.text)] == [(str(hex_key), "1")]


def test_removing_the_last_row_leaves_an_empty_one(bb_client):
    insert, _ = two_parts(bb_client)
    response = bb_client.post(FORM, data=create_fields(
        bb_client, has_parts="on", parts_part=[str(insert)], parts_qty=["2"], parts_remove="0"))
    assert [(c, q) for c, q, _ in rows_of(response.text)] == [("", "")]


def test_a_bad_remove_index_changes_nothing(bb_client):
    insert, _ = two_parts(bb_client)
    for index in ("5", "-1", "x"):
        response = bb_client.post(FORM, data=create_fields(
            bb_client, has_parts="on", parts_part=[str(insert)], parts_qty=["2"], parts_remove=index))
        assert response.status_code == 200, index
        assert [(c, q) for c, q, _ in rows_of(response.text)] == [(str(insert), "2")], index
    assert article_count(bb_client) == 0


# ---- editing --------------------------------------------------------------------------------


def article_with_parts(client):
    insert, hex_key = two_parts(client)
    with sm_of(client).begin() as s:
        a = make_article(s).id
        s.add(ArticlePart(article_id=a, part_id=insert, qty_per_unit=2))
    return a, insert, hex_key


def test_edit_shows_the_parts_in_use_ticked(ready_client):
    a, insert, hex_key = article_with_parts(ready_client)
    page = ready_client.get(f"/articles/{a}/edit").text
    assert has_parts_checked(page)
    assert rows_of(page) == [(str(insert), "2", [str(hex_key), str(insert)])]


def test_edit_offers_no_part_chosen_in_another_row(ready_client):
    a, insert, hex_key = article_with_parts(ready_client)
    with sm_of(ready_client).begin() as s:
        s.add(ArticlePart(article_id=a, part_id=hex_key, qty_per_unit=1))
    rows = rows_of(ready_client.get(f"/articles/{a}/edit").text)
    assert rows == [(str(hex_key), "1", [str(hex_key)]), (str(insert), "2", [str(insert)])]


def test_edit_without_parts_is_unticked(ready_client):
    two_parts(ready_client)
    with sm_of(ready_client).begin() as s:
        a = make_article(s).id
    page = ready_client.get(f"/articles/{a}/edit").text
    assert not has_parts_checked(page) and rows_of(page)[0][:2] == ("", "")


def test_edit_change_and_add_parts(ready_client):
    a, insert, hex_key = article_with_parts(ready_client)
    response = ready_client.post(f"/articles/{a}/edit", data=edit_fields(
        ready_client, a, has_parts="on", parts_part=[str(insert), str(hex_key)], parts_qty=["5", "1"]))
    assert response.status_code == 303
    assert links(ready_client) == {(a, insert): 5, (a, hex_key): 1}


def test_edit_untick_and_save_removes_the_parts(ready_client):
    a, insert, _ = article_with_parts(ready_client)
    response = ready_client.post(f"/articles/{a}/edit", data=edit_fields(
        ready_client, a, parts_part=[str(insert)], parts_qty=["2"]))
    assert response.status_code == 303
    assert links(ready_client) == {}


def test_edit_add_and_remove_without_javascript_save_nothing(ready_client):
    a, insert, hex_key = article_with_parts(ready_client)
    added = ready_client.post(f"/articles/{a}/edit", data=edit_fields(
        ready_client, a, name="Renamed", has_parts="on", parts_part=[str(insert)], parts_qty=["7"], parts_add="1"))
    assert added.status_code == 200 and 'value="Renamed"' in added.text
    assert [(c, q) for c, q, _ in rows_of(added.text)] == [(str(insert), "7"), ("", "")]
    removed = ready_client.post(f"/articles/{a}/edit", data=edit_fields(
        ready_client, a, has_parts="on", parts_part=[str(insert)], parts_qty=["7"], parts_remove="0"))
    assert removed.status_code == 200
    assert links(ready_client) == {(a, insert): 2}
    with sm_of(ready_client).begin() as s:
        assert s.get(Article, a).name == "Template A"


def test_edit_error_keeps_the_rows_typed(ready_client):
    a, insert, _ = article_with_parts(ready_client)
    response = ready_client.post(f"/articles/{a}/edit", data=edit_fields(
        ready_client, a, has_parts="on", parts_part=[str(insert), str(insert)], parts_qty=["3", "4"]))
    assert response.status_code == 200
    assert en("articles.part_twice", name="Insert M4") in response.text
    assert [(c, q) for c, q, _ in rows_of(response.text)] == [(str(insert), "3"), (str(insert), "4")]
    assert links(ready_client) == {(a, insert): 2}


def test_edit_unticked_rows_are_kept_in_the_form_after_an_error(ready_client):
    """Unticking only hides the list: an error elsewhere does not throw the rows away."""
    a, insert, _ = article_with_parts(ready_client)
    response = ready_client.post(f"/articles/{a}/edit", data=edit_fields(
        ready_client, a, name="", parts_part=[str(insert)], parts_qty=["2"]))
    assert response.status_code == 200
    assert not has_parts_checked(response.text)
    assert [(c, q) for c, q, _ in rows_of(response.text)] == [(str(insert), "2")]


# ---- limits ---------------------------------------------------------------------------------


def test_too_many_rows_are_refused_and_not_cut_off(bb_client):
    """A part in row 201 must not be lost quietly: the save says no."""
    insert, _ = two_parts(bb_client)
    response = bb_client.post(FORM, data=create_fields(
        bb_client, has_parts="on",
        parts_part=[""] * 200 + [str(insert)], parts_qty=[""] * 200 + ["1"]))
    assert response.status_code == 200
    assert en("articles.part_too_many", max=200) in response.text
    assert article_count(bb_client) == 0


def test_add_stops_at_the_row_limit(bb_client):
    insert, _ = two_parts(bb_client)
    response = bb_client.post(FORM, data=create_fields(
        bb_client, has_parts="on", parts_part=[""] * 200, parts_qty=[""] * 200, parts_add="1"))
    assert response.status_code == 200 and len(rows_of(response.text)) == 200


# ---- archived parts -------------------------------------------------------------------------


def test_a_part_archived_meanwhile_gets_the_archived_message(bb_client):
    insert, hex_key = two_parts(bb_client)
    data = create_fields(bb_client, has_parts="on", parts_part=[str(hex_key)], parts_qty=["1"])
    with sm_of(bb_client).begin() as s:
        catalog.archive_part(s, hex_key)  # archived in another tab after the form was opened
    response = bb_client.post(FORM, data=data)
    assert response.status_code == 200
    assert en("parts.archived", name="Hex key 2") in response.text
    assert article_count(bb_client) == 0


def test_a_link_to_an_archived_part_survives_editing(ready_client):
    """The form does not show it, but saving must not drop it (stock still deducts it)."""
    a, insert, hex_key = article_with_parts(ready_client)
    with sm_of(ready_client).begin() as s:
        s.add(ArticlePart(article_id=a, part_id=hex_key, qty_per_unit=3))
        s.flush()
        s.get(Part, hex_key).archived_at = datetime.now(UTC)
    page = ready_client.get(f"/articles/{a}/edit").text
    assert rows_of(page) == [(str(insert), "2", [str(insert)])]  # only the active part is in the list
    for fields in ({"has_parts": "on", "parts_part": [str(insert)], "parts_qty": ["5"]}, {}):
        assert ready_client.post(f"/articles/{a}/edit", data=edit_fields(ready_client, a, **fields)).status_code == 303
    assert links(ready_client) == {(a, hex_key): 3}  # unticked: the active link is gone, the archived stays


def test_the_script_ticks_the_box_when_a_row_is_added():
    """As on the server: adding a part means parts are needed (a browser without :has shows the list always)."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'input[name="has_parts"]' in source and ".checked = true" in source
