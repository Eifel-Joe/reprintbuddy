"""Switching between print article and article made of parts on the web (Spec 5.6 "Wechsel", review
finding 3 of p341, 0.4.0)."""

from sqlalchemy import select

from conftest import csrf_of, form_token_of, sm_of
from factories import make_article, make_assembled
from texts import en
from reprintbuddy.domain import assembled
from reprintbuddy.models import Article, ArticleComponent


def test_print_article_page_offers_the_switch(ready_client):
    with sm_of(ready_client).begin() as s:
        lamp = make_article(s, name="Lamp").id
        stocked = make_article(s, name="Shelf", stock=2).id
    assert f'href="/articles/{lamp}/to-assembled"' in ready_client.get(f"/articles/{lamp}").text
    assert f'href="/articles/{stocked}/to-assembled"' not in ready_client.get(f"/articles/{stocked}").text


def test_switch_to_assembled_from_the_form(ready_client):
    with sm_of(ready_client).begin() as s:
        lamp = make_article(s, name="Lamp", sku="EX-L1").id
        clip = make_article(s, name="Clip").id
    page = ready_client.get(f"/articles/{lamp}/to-assembled")
    assert page.status_code == 200 and f'action="/articles/{lamp}/to-assembled"' in page.text and 'value="EX-L1"' in page.text
    data = {
        "csrf_token": csrf_of(ready_client, f"/articles/{lamp}/to-assembled"),
        "form_token": form_token_of(ready_client, f"/articles/{lamp}/to-assembled", f"/articles/{lamp}/to-assembled"),
        "name": "Lamp", "sku": "EX-L1", "mode": "stock", "base_rows": "1", "base_0_qty": "2", "base_0_article": str(clip),
        "c1_options": "1", "c1_0_rows": "1",
    }
    response = ready_client.post(f"/articles/{lamp}/to-assembled", data=data, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == f"/articles/{lamp}"
    with sm_of(ready_client).begin() as s:
        row = s.get(Article, lamp)
        assert (row.kind, row.sku, row.library_file_id) == ("assembled", "EX-L1", None)


def _switch_form(client, lamp, clip, **over):
    path = f"/articles/{lamp}/to-assembled"
    data = {
        "csrf_token": csrf_of(client, path), "form_token": form_token_of(client, path, path),
        "name": "Lamp", "sku": "EX-L1", "mode": "stock", "base_rows": "1", "base_0_qty": "2", "base_0_article": str(clip),
        "c1_options": "1", "c1_0_rows": "1",
    }
    data.update(over)
    return data


def test_switch_sent_twice_says_already_saved_and_a_bad_input_keeps_the_form(ready_client):
    """0.4.8 (R9): a bad input keeps the form. p540 (0.4.9): the second sending answers "already saved"
    and leads to the article like every other one-time form, no longer "not found"."""
    with sm_of(ready_client).begin() as s:
        lamp = make_article(s, name="Lamp", sku="EX-L1").id
        clip = make_article(s, name="Clip").id
    path = f"/articles/{lamp}/to-assembled"
    bad = ready_client.post(path, data=_switch_form(ready_client, lamp, clip, base_0_qty="abc", name="Lamp 2"))
    assert bad.status_code == 200 and 'value="Lamp 2"' in bad.text and f'action="{path}"' in bad.text
    forged = ready_client.post(path, data=_switch_form(ready_client, lamp, clip, form_token="forged"))
    assert forged.status_code == 400 and en("form.expired") in forged.text
    data = _switch_form(ready_client, lamp, clip)
    assert ready_client.post(path, data=data, follow_redirects=False).status_code == 303
    # Sent again (double click, back and send once more): the article is no print article any more, but the
    # repeat still reaches the double-submit guard. Nothing is booked twice.
    again = ready_client.post(path, data=data, follow_redirects=False)
    assert again.status_code == 303 and again.headers["location"] == f"/articles/{lamp}"
    assert en("form.duplicate") in ready_client.get(f"/articles/{lamp}").text
    with sm_of(ready_client).begin() as s:
        assert s.get(Article, lamp).kind == "assembled"


def test_switch_of_something_that_is_no_print_article_is_still_not_found(ready_client):
    """Only a repeat of the form is "already saved"; an unknown article, and an article made of parts that
    meets a fresh form, stay "not found"."""
    with sm_of(ready_client).begin() as s:
        clip = make_article(s, name="Clip").id
        lamp = make_assembled(s, name="Lamp", sku="EX-L1", base=[(clip, 2)]).id
    path = f"/articles/{lamp}/to-assembled"
    fresh = {"csrf_token": csrf_of(ready_client, f"/articles/{clip}/to-assembled"),
             "form_token": form_token_of(ready_client, f"/articles/{clip}/to-assembled", f"/articles/{clip}/to-assembled"),
             "name": "Lamp", "sku": "EX-L1", "mode": "stock", "base_rows": "1", "base_0_qty": "2", "base_0_article": str(clip),
             "c1_options": "1", "c1_0_rows": "1"}
    assert ready_client.post(path, data=fresh, follow_redirects=False).status_code == 404
    assert ready_client.post("/articles/999/to-assembled", data=fresh, follow_redirects=False).status_code == 404
    with sm_of(ready_client).begin() as s:
        assert s.get(Article, lamp).kind == "assembled"


def test_relink_of_assembled_makes_it_a_print_article(bb_client, fake_bambuddy):
    with sm_of(bb_client).begin() as s:
        clip = make_article(s, name="Clip", library_file_id=50).id
        lamp = make_assembled(s, name="Lamp", sku="EX-L1", base=[(clip, 2)]).id
    page = bb_client.get(f"/articles/{lamp}").text
    assert f'href="/articles/{lamp}/relink"' in page and en("assembled.to_print") in page
    path = f"/articles/{lamp}/relink/file/1/plate/1"
    data = {"csrf_token": csrf_of(bb_client, f"/articles/{lamp}/relink/file/1"),
            "form_token": form_token_of(bb_client, f"/articles/{lamp}/relink/file/1", path)}
    assert bb_client.post(path, data=data).status_code in (200, 303)
    with sm_of(bb_client).begin() as s:
        row = s.get(Article, lamp)
        assert (row.kind, row.library_file_id, row.plate_id, row.sku) == ("print", 1, 1, "EX-L1")
        assert s.scalars(select(ArticleComponent).where(ArticleComponent.article_id == lamp)).all() == []


def test_a_variant_row_has_no_relink(bb_client):
    with sm_of(bb_client).begin() as s:
        a, b = make_article(s, name="A").id, make_article(s, name="B").id
        composite = make_assembled(s, name="Bracket", sku="EX-K1", fields=[[("-A", [(a, 2)]), ("-B", [(b, 2)])]])
        variant = assembled.variants(s, composite)[0].id
    assert bb_client.get(f"/articles/{variant}/relink").status_code == 404
