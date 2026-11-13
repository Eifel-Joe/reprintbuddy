"""The form of an article made of parts (Spec 5.6, 0.4.0 p341)."""

import re

from sqlalchemy import select

from conftest import csrf_of, form_token_of, sm_of
from factories import make_article, make_assembled
from texts import en
from more_rows_support import without_templates
from reprintbuddy.domain import assembled
from reprintbuddy.models import Article


def parts(client):
    with sm_of(client).begin() as s:
        return {n: make_article(s, name=n).id for n in (
            "Segment", "Side part L", "Side part R", "Socket L32", "Socket L40", "Clip")}


def hose_form(client, ids, **over):
    data = {
        "csrf_token": csrf_of(client, "/articles/new-assembled"),
        "form_token": form_token_of(client, "/articles/new-assembled", "/articles/new-assembled"),
        "name": "Hose", "sku": "EX-H1", "mode": "order", "base_rows": "1",
        "base_0_qty": "12", "base_0_article": str(ids["Segment"]),
        "c1_options": "2",
        "c1_0_suffix": "-L32", "c1_0_rows": "2",
        "c1_0_0_qty": "1", "c1_0_0_article": str(ids["Side part L"]),
        "c1_0_1_qty": "1", "c1_0_1_article": str(ids["Socket L32"]),
        "c1_1_suffix": "-L40", "c1_1_rows": "2",
        "c1_1_0_qty": "1", "c1_1_0_article": str(ids["Side part L"]),
        "c1_1_1_qty": "1", "c1_1_1_article": str(ids["Socket L40"]),
    }
    data.update(over)
    return data


def select_html(page: str, name: str) -> str:
    return re.search(rf'<select name="{name}"[^>]*>(.*?)</select>', page, re.S).group(1)


def test_new_assembled_form_has_sections_and_mode(ready_client):
    parts(ready_client)
    page = ready_client.get("/articles/new-assembled")
    assert page.status_code == 200
    text = page.text
    for key in ("sets.base", "sets.choices_1", "assembled.mode.stock", "assembled.mode.order", "sets.add_choices_2"):
        assert en(key) in text
    assert 'name="c1_1_1_article"' in text and 'name="base_2_article"' in text  # 3 base rows, 2 options x 2 rows
    assert 'name="c2_0_suffix"' not in without_templates(text)  # the script has them in a template


def test_create_hose_from_the_form(ready_client):
    ids = parts(ready_client)
    response = ready_client.post("/articles/new-assembled", data=hose_form(ready_client, ids), follow_redirects=False)
    assert response.status_code == 303
    with sm_of(ready_client).begin() as s:
        hose = s.scalars(select(Article).where(Article.kind == "assembled")).one()
        assert response.headers["location"] == f"/articles/{hose.id}"
        assert [v.sku for v in assembled.variants(s, hose)] == ["EX-H1-L32", "EX-H1-L40"]
        assert hose.assembly_mode == "order"


def test_create_double_submit_creates_one_and_a_forged_form_is_refused(ready_client):
    """0.4.8 (R9): held before the save moved into the shared double-submit helper."""
    ids = parts(ready_client)
    data = hose_form(ready_client, ids)
    first = ready_client.post("/articles/new-assembled", data=data, follow_redirects=False)
    again = ready_client.post("/articles/new-assembled", data=data, follow_redirects=False)
    assert first.status_code == again.status_code == 303
    assert again.headers["location"] == "/articles"
    assert en("form.duplicate") in ready_client.get("/articles").text
    forged = ready_client.post("/articles/new-assembled", data={**data, "form_token": "forged", "sku": "EX-H2"})
    assert forged.status_code == 400 and en("form.expired") in forged.text and 'value="EX-H2"' in forged.text
    with sm_of(ready_client).begin() as s:
        assert len(s.scalars(select(Article).where(Article.kind == "assembled")).all()) == 1


def test_more_rows_and_options_keep_what_was_typed(ready_client):
    ids = parts(ready_client)
    for action, field in (("more_base", 'name="base_2_article"'), ("more_c1", 'name="c1_3_suffix"'),
                          ("more_c1_0", 'name="c1_0_3_article"'), ("add_c2", 'name="c2_0_suffix"')):
        page = ready_client.post("/articles/new-assembled", data=hose_form(ready_client, ids, action=action))
        assert page.status_code == 200, action
        assert field in page.text, action
        assert 'value="-L40"' in page.text  # typed, not saved
    with sm_of(ready_client).begin() as s:
        assert s.scalars(select(Article).where(Article.kind == "assembled")).all() == []


def test_article_may_repeat_across_options_in_the_offer(ready_client):
    ids = parts(ready_client)
    page = ready_client.post("/articles/new-assembled", data=hose_form(ready_client, ids, action="more_c1")).text
    other_option = select_html(page, "c1_1_0_article")
    side = re.search(rf'<option value="{ids["Side part L"]}"[^>]*>', other_option).group(0)
    assert "hidden" not in side  # side part L may stand in -L32 and -L40
    segment = re.search(rf'<option value="{ids["Segment"]}"[^>]*>', other_option).group(0)
    assert "hidden" in segment  # a base part is in no option


def test_errors_keep_input_and_name_the_article(ready_client):
    ids = parts(ready_client)
    data = hose_form(ready_client, ids, c1_0_1_article=str(ids["Side part L"]))  # twice in -L32
    page = ready_client.post("/articles/new-assembled", data=data)
    assert page.status_code == 200
    assert en("assembled.article_twice", name="Side part L") in page.text.replace("&#39;", "'")
    assert 'value="-L40"' in page.text


def test_edit_shows_variant_table_with_targets(ready_client):
    ids = parts(ready_client)
    with sm_of(ready_client).begin() as s:
        hose = make_assembled(s, name="Hose", sku="EX-H1", base=[(ids["Segment"], 12)], fields=[[
            ("-L", [(ids["Side part L"], 1)]), ("-R", [(ids["Side part R"], 1)])]])
        hose_id = hose.id
        variant = assembled.variants(s, hose)[0].id
    page = ready_client.get(f"/articles/{hose_id}/assembled")
    assert page.status_code == 200
    assert f'name="v_{variant}_min"' in page.text and "EX-H1-L" in page.text
    data = {
        "csrf_token": csrf_of(ready_client, f"/articles/{hose_id}/assembled"),
        "name": "Hose", "sku": "EX-H1", "mode": "stock", "base_rows": "1",
        "base_0_qty": "12", "base_0_article": str(ids["Segment"]),
    }
    with sm_of(ready_client).begin() as s:
        options = assembled.options(s, s.get(Article, hose_id))[0]
    for i, (option, part) in enumerate(zip(options, ("Side part L", "Side part R"))):
        data.update({f"c1_{i}_id": str(option.id), f"c1_{i}_suffix": option.suffix, f"c1_{i}_rows": "1",
                     f"c1_{i}_0_qty": "1", f"c1_{i}_0_article": str(ids[part])})
    data["c1_options"] = "2"
    data[f"v_{variant}_min"] = "3"
    data[f"v_{variant}_buffer"] = "1"
    response = ready_client.post(f"/articles/{hose_id}/assembled", data=data, follow_redirects=False)
    assert response.status_code == 303
    with sm_of(ready_client).begin() as s:
        row = s.get(Article, variant)
        assert (row.min_stock, row.buffer, row.archived_at) == (3, 1, None)


def test_sold_alone_switch_on_print_article_form(ready_client):
    ids = parts(ready_client)
    page = ready_client.get(f"/articles/{ids['Clip']}/edit").text
    box = re.search(r'<input name="sold_alone"[^>]*>', page).group(0)
    assert "checked" in box


def test_edit_of_assembled_goes_to_its_form(ready_client):
    ids = parts(ready_client)
    with sm_of(ready_client).begin() as s:
        box = make_assembled(s, name="Storage box", base=[(ids["Clip"], 4)]).id
    response = ready_client.get(f"/articles/{box}/edit", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"/articles/{box}/assembled"
