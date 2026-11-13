"""The buttons that only show more rows (0.4.1, p380): the new rows are where the user was.

One script (static/js/more_rows.js) adds the rows in place for the set form, the form of an article
made of parts and the new order. This file holds what the server owes it and what the page owes a
browser without the script: a jump mark on the first new row (`#more-new`, on the button's own
`formaction`) and a first control with `autofocus`, so the page does not start at the top again; and
the row templates the script copies, with the counters it keeps in step with the rows it adds.
"""

import re

from conftest import sm_of
from factories import make_set
from texts import en
from more_rows_support import templates_of, without_templates
from test_assembled_form import hose_form, parts
from test_orders_pages import one_article, order_fields
from test_sets_pages import set_fields, three_articles



def fresh(page: str) -> str:
    """The opening tag that carries the jump mark and what follows it (the first controls of the new row)."""
    marks = re.findall(r'<[a-z]+[^>]*\bid="more-new"[^>]*>', page)
    assert len(marks) == 1, marks  # one jump mark per page, or the browser jumps to the wrong one
    start = page.index(marks[0])
    return page[start:start + 700]


def action_buttons(page: str) -> list[str]:
    return re.findall(r'<button[^>]*name="action"[^>]*>', page)


# ---- the set form -----------------------------------------------------------------------------


def test_set_more_rows_marks_the_first_new_row_and_focuses_it(ready_client):
    a, b, c = three_articles(ready_client)
    for action, first in (("more_base", "base_3_qty"), ("more_c1", "c1_3_article")):
        page = ready_client.post("/sets/new", data=set_fields(ready_client, a, b, c, action=action)).text
        block = fresh(page)
        assert first in block and "autofocus" in block, action
        assert block.index("autofocus") < block.index(first) + 200, action  # on the row's own control


def test_set_add_choice_parts_2_marks_the_new_fieldset(ready_client):
    a, b, c = three_articles(ready_client)
    page = ready_client.post("/sets/new", data=set_fields(ready_client, a, b, c, action="add_c2")).text
    block = fresh(page)
    assert block.startswith("<fieldset") and en("sets.choices_2") in block and "autofocus" in block


def test_set_form_as_first_shown_and_saved_has_no_jump_mark(ready_client):
    three_articles(ready_client)
    assert 'id="more-new"' not in ready_client.get("/sets/new").text


def test_set_buttons_jump_to_the_new_rows_and_name_what_the_script_does(ready_client):
    three_articles(ready_client)
    page = ready_client.get("/sets/new").text
    buttons = action_buttons(page)
    assert len(buttons) == 4  # more rows in base parts, choice parts 1, add choice parts 2, and the one in the template
    for button in buttons:
        assert 'formaction="/sets/new#more-new"' in button, button
        assert "data-more-add" in button or "data-more-show" in button, button


def test_set_edit_buttons_keep_the_edit_address(ready_client):
    a, b, c = three_articles(ready_client)
    with sm_of(ready_client).begin() as s:
        set_id = make_set(s, "Kit", [(1, a)], [(1, [(b, None), (c, None)])], sku="K").id
    page = ready_client.get(f"/sets/{set_id}/edit").text
    for button in action_buttons(page):
        assert f'formaction="/sets/{set_id}/edit#more-new"' in button, button


def test_set_rows_come_with_a_template_and_a_counter_the_script_keeps(ready_client):
    three_articles(ready_client)
    page = ready_client.get("/sets/new").text
    shown = without_templates(page)
    for token in ("{i}", "{i1}", "{j}", "{j1}"):
        assert token not in shown, token  # the tokens are filled in by the script, never shown
    assert 'data-more-group="base_rows"' in shown and 'name="base_rows" value="3"' in shown
    assert 'data-more-group="c1_rows"' in shown and 'name="c1_rows" value="3"' in shown
    row = next(t for t in templates_of(page) if "base_{i}_qty" in t)
    assert "base_{i}_article" in row and "{i1}" in row and "data-set-article" in row
    reveal = next(t for t in templates_of(page) if 'name="c2_shown"' in t)
    assert 'name="c2_rows" value="3"' in reveal and 'name="c2_2_article"' in reveal  # c2 comes with its three rows
    assert "data-more-reveal" in shown


def test_set_with_choice_parts_2_has_no_reveal_template(ready_client):
    a, b, c = three_articles(ready_client)
    data = set_fields(ready_client, a, b, c, c2_shown="1", c2_rows="3", c2_qty="1")
    page = ready_client.post("/sets/new", data={**data, "action": "more_base"}).text
    assert "data-more-reveal" not in page and "data-more-show" not in page


def test_set_script_is_loaded(ready_client):
    three_articles(ready_client)
    page = ready_client.get("/sets/new").text
    assert re.search(r'<script src="/static/js/more_rows\.js\?v=[^"]+" defer></script>', page)
    assert ready_client.get("/static/js/more_rows.js").status_code == 200


# ---- the form of an article made of parts -----------------------------------------------------


def test_assembled_more_marks_the_first_new_row_option_or_field(ready_client):
    ids = parts(ready_client)
    cases = (
        ("more_base", "base_1_qty"),        # one base row typed, two more: the first new one is row 1
        ("more_c1", "c1_2_suffix"),         # two options typed, the first new option is option 2
        ("more_c1_0", "c1_0_2_qty"),        # option 0 had two rows
        ("add_c2", "c2_0_suffix"),
    )
    for action, first in cases:
        page = ready_client.post("/articles/new-assembled", data=hose_form(ready_client, ids, action=action)).text
        block = fresh(page)
        assert first in block and "autofocus" in block, action


def test_assembled_buttons_jump_and_name_what_the_script_does(ready_client):
    parts(ready_client)
    page = ready_client.get("/articles/new-assembled").text
    buttons = action_buttons(page)
    assert len(buttons) >= 5
    for button in buttons:
        assert 'formaction="/articles/new-assembled#more-new"' in button, button
        assert "data-more-add" in button or "data-more-show" in button, button


def test_assembled_templates_carry_tokens_and_counters_at_both_levels(ready_client):
    parts(ready_client)
    page = ready_client.get("/articles/new-assembled").text
    shown = without_templates(page)
    for token in ("{i}", "{i1}", "{j}", "{j1}"):
        assert token not in shown, token
    assert 'data-more-group="base_rows"' in shown and 'data-more-group="c1_options"' in shown
    assert 'data-more-group="c1_0_rows"' in shown and 'data-more-group="c1_1_rows"' in shown
    options = templates_of(page)
    row = next(t for t in options if "c1_0_{j}_qty" in t)  # a row of option 0: only the row is a token
    assert "c1_0_{j}_article" in row and "{j1}" in row
    option = next(t for t in options if 'name="c1_{i}_suffix"' in t)  # a new option: its own number and its rows
    assert 'name="c1_{i}_rows" value="2"' in option and 'name="c1_{i}_0_qty"' in option
    assert 'data-more-group="c1_{i}_rows"' in option and 'data-more-key="j"' in option
    assert "c1_{i}_{j}_qty" in option  # the template for the rows that option gets later
    assert 'name="c1_{i}_id" value=""' in option


def test_assembled_script_is_loaded(ready_client):
    parts(ready_client)
    page = ready_client.get("/articles/new-assembled").text
    assert re.search(r'<script src="/static/js/more_rows\.js\?v=[^"]+" defer></script>', page)


# ---- the new order ----------------------------------------------------------------------------


def test_order_more_rows_marks_the_first_new_row(ready_client):
    a = one_article(ready_client)
    page = ready_client.post("/orders/new", data=order_fields(ready_client, item_0=f"article:{a}", action="more")).text
    block = fresh(page)
    assert "qty_5" in block and "autofocus" in block


def test_order_form_has_template_counter_and_button(ready_client):
    one_article(ready_client)
    page = ready_client.get("/orders/new").text
    assert 'id="more-new"' not in page
    shown = without_templates(page)
    assert "{i}" not in shown and 'data-more-group="rows"' in shown and 'name="rows" value="5"' in shown
    row = templates_of(page)[0]
    assert "qty_{i}" in row and "item_{i}" in row and "data-more-row" in row
    (button,) = action_buttons(page)
    assert 'formaction="/orders/new#more-new"' in button and "data-more-add" in button
    assert "formnovalidate" in button  # more rows must not ask for the reference first
    assert 'data-more-max="50"' in page and 'data-more-step="5"' in page
    assert re.search(r'<script src="/static/js/more_rows\.js\?v=[^"]+" defer></script>', page)


def test_order_form_without_script_still_adds_rows_and_saves_nothing(ready_client):
    a = one_article(ready_client)
    response = ready_client.post("/orders/new", data=order_fields(ready_client, item_0=f"article:{a}", qty_0="2", action="more"))
    assert response.status_code == 200 and 'name="qty_9"' in response.text and 'name="qty_10"' not in response.text


# ---- the script and the stylesheet ------------------------------------------------------------


def test_the_script_knows_every_attribute_the_templates_use():
    from pathlib import Path

    import reprintbuddy.web

    web = Path(reprintbuddy.web.__file__).parent
    script = (web / "static" / "js" / "more_rows.js").read_text(encoding="utf-8")
    used = set()
    for name in ("set_form", "assembled_form", "order_new"):
        used |= set(re.findall(r"\b(data-more-[a-z]+)", (web / "templates" / f"{name}.html").read_text(encoding="utf-8")))
    used |= set(re.findall(r"\b(data-more-[a-z]+)", (web / "templates" / "_ui.html").read_text(encoding="utf-8")))
    assert {"data-more-group", "data-more-row", "data-more-template", "data-more-add", "data-more-show"} <= used
    assert [name for name in sorted(used) if name not in script] == []
    css = (web / "static" / "css" / "reprintbuddy.css").read_text(encoding="utf-8")
    assert ".more-added" in css and "#more-new" in css and "prefers-reduced-motion" in css
