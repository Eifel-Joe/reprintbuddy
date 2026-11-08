"""Changing the print file of an article at any time (Spec 5.1, v0.3.10 p135)."""

import re

from conftest import csrf_of, form_token_of, sm_of
from factories import make_article, make_sort, tick_sorts
from sqlalchemy import select

from reprintbuddy import settings as st
from reprintbuddy.bambuddy.types import FilamentRequirement
from reprintbuddy.models import Article, ArticleFilament, ArticleFilamentSort, utcnow
from reprintbuddy.web.i18n import translate


def en(key, **params):
    return translate("en", key, **params)


def make(client, **fields):
    with sm_of(client).begin() as s:
        return make_article(s, name=fields.pop("name", "Template A"), **fields).id


def button(html: str, article_id: int):
    return re.search(rf'<a class="btn[^"]*" href="/articles/{article_id}/relink">(.*?)</a>', html, re.S)


# ---- the button ---------------------------------------------------------------------------


def test_button_on_the_page_of_an_active_article_without_any_pause(bb_client):
    a = make(bb_client)
    html = bb_client.get(f"/articles/{a}").text
    found = button(html, a)
    assert found is not None and en("articles.change_file") in found.group(1)
    assert "article-pause" not in html  # the article is not paused: the button stands without it


def test_button_in_german(bb_client):
    a = make(bb_client)
    with sm_of(bb_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    assert "Druckdatei ändern" in button(bb_client.get(f"/articles/{a}").text, a).group(1)


def test_button_is_there_for_every_pause_and_the_pause_card_keeps_its_own(bb_client):
    a = make(bb_client, paused=True, pause_reason="file_missing")
    html = bb_client.get(f"/articles/{a}").text
    assert en("articles.change_file") in html
    assert en("articles.choose_file_again") in html  # the card of the pause keeps its button


def test_no_button_for_an_archived_article(bb_client):
    a = make(bb_client, archived_at=utcnow())
    assert button(bb_client.get(f"/articles/{a}").text, a) is None


# ---- the way: choose, plate, change; stock and settings stay -------------------------------------


def choose(client, article_id, file_id=2, plate=1):
    page = f"/articles/{article_id}/relink/file/{file_id}"
    path = f"{page}/plate/{plate}"
    data = {"csrf_token": csrf_of(client, page), "form_token": form_token_of(client, page, path)}
    return client.post(path, data=data)


def test_stock_and_settings_stay(bb_client):
    a = make(bb_client, stock=7, min_stock=3, buffer=1, units_per_print=2, sku="TPL-1", target_model="A1")
    response = choose(bb_client, a)
    assert response.status_code == 303 and response.headers["location"] == f"/articles/{a}"
    with sm_of(bb_client)() as s:
        row = s.get(Article, a)
        assert (row.library_file_id, row.file_name) == (2, "Template B.gcode.3mf")
        assert (row.stock, row.min_stock, row.buffer, row.units_per_print) == (7, 3, 1, 2)
        assert (row.sku, row.target_model, row.name) == ("TPL-1", "A1", "Template A")


def _with_group(client, material="PLA", color="#000000"):
    with sm_of(client).begin() as s:
        a = make_article(s, name="Template A", library_file_id=1, plate_id=1, target_model="A1").id
        s.add(ArticleFilament(article_id=a, slot=1, grams=42.5, material=material, color_hex=color))
        s.flush()
        sort = make_sort(s, material=material, color_hex=color)
        tick_sorts(s, a, 1, sort, reordered=sort)
        return a, sort.id


def ticked(client, article_id):
    with sm_of(client)() as s:
        return [r.sort_id for r in s.scalars(select(ArticleFilamentSort).where(ArticleFilamentSort.article_id == article_id))]


def test_unchanged_slot_keeps_its_group_and_no_note(bb_client, fake_bambuddy):
    a, sort_id = _with_group(bb_client)
    fake_bambuddy.set_requirements(2, 1, [FilamentRequirement(1, 10.0, "PLA", "#000000")])
    assert choose(bb_client, a).status_code == 303
    assert ticked(bb_client, a) == [sort_id]
    page = bb_client.get(f"/articles/{a}").text
    assert en("articles.relinked", name="Template A") in page
    assert en("articles.relinked_regroup", slots="1") not in page


def test_changed_slot_loses_its_group_and_the_page_says_to_tick_again(bb_client, fake_bambuddy):
    a, _ = _with_group(bb_client)
    fake_bambuddy.set_requirements(2, 1, [FilamentRequirement(1, 10.0, "PETG", "#FF0000")])
    assert choose(bb_client, a).status_code == 303
    assert ticked(bb_client, a) == []
    page = bb_client.get(f"/articles/{a}").text
    assert en("articles.relinked", name="Template A") in page
    assert en("articles.relinked_regroup", slots="1") in page
    assert en("articles.relinked_regroup", slots="1") not in bb_client.get(f"/articles/{a}").text  # shown once
