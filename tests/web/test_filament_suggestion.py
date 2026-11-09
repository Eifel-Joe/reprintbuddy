"""Creating an article: its filament page starts from a similar article (Spec 5.2, 9.1, p156).

Only the page shows the suggestion; nothing is stored until "Save" ("Later" stores nothing)."""

import re
from html import unescape

from conftest import csrf_of, form_token_of, sm_of
from factories import make_article
from reprintbuddy.bambuddy.types import FilamentRequirement
from reprintbuddy.models import ArticleFilament, ArticleFilamentSort, FilamentSort, utcnow
from reprintbuddy.web.i18n import translate

FORM = "/articles/new/file/1/plate/1"


def en(key, **params):
    return translate("en", key, **params)


def boxes(html, slot=1):
    return [
        (v, bool(c))
        for v, c in re.findall(rf'<input type="checkbox"[^>]*name="tick_{slot}" value="(\d+)"( checked)?', html)
    ]


def radios(html, slot=1):
    return [
        (v, bool(c))
        for v, c in re.findall(rf'<input type="radio"[^>]*name="reorder_{slot}" value="(\d+)"( checked)?', html)
    ]


def two_sorts(fake):
    fake.add_spool("PLA", brand="Brand A", color_hex="#000000", color_name="Black")
    fake.add_spool("PLA", brand="Brand B", color_hex="#000000", color_name="Schwarz")


def source(client, fake, *, name="Source article", ticks=("Brand A",), reordered="Brand A", **fields):
    """An article of another file with a black PLA slot whose group is ticked (through the page, like a person)."""
    with sm_of(client).begin() as s:
        art = make_article(s, name=name, library_file_id=2, plate_id=1, **fields)
        s.add(ArticleFilament(article_id=art.id, slot=1, grams=9.0, material="PLA", color_hex="#000000"))
        a = art.id
    client.get(f"/articles/{a}/filaments")  # enters the spool sorts
    with sm_of(client).begin() as s:
        by_brand = {x.brand: x.id for x in s.query(FilamentSort).all()}
        for brand in ticks:
            s.add(ArticleFilamentSort(article_id=a, slot=1, sort_id=by_brand[brand], reordered=brand == reordered))
    return a, by_brand


def create(client):
    data = {
        "csrf_token": csrf_of(client, FORM), "form_token": form_token_of(client, FORM, FORM),
        "name": "New article", "sku": "", "units_per_print": "1", "min_stock": "0", "buffer": "0", "target": "model:A1",
    }
    response = client.post(FORM, data=data)
    assert response.status_code == 303
    return response.headers["location"]


def new_id(location):
    return int(location.split("/")[2])


def stored(client, article_id):
    with sm_of(client)() as s:
        return s.query(ArticleFilamentSort).filter_by(article_id=article_id).count()


def test_creating_leads_to_the_page_with_the_similar_articles_choice_ticked(bb_client, fake_bambuddy):
    two_sorts(fake_bambuddy)
    src, ids = source(bb_client, fake_bambuddy, ticks=("Brand A", "Brand B"), reordered="Brand B")
    location = create(bb_client)
    assert unescape(location) == f"/articles/{new_id(location)}/filaments?later=1&suggest=1"
    page = bb_client.get(location)
    assert page.status_code == 200
    assert sorted(boxes(page.text)) == sorted([(str(ids["Brand A"]), True), (str(ids["Brand B"]), True)])
    assert (str(ids["Brand B"]), True) in radios(page.text) and (str(ids["Brand A"]), False) in radios(page.text)
    assert en("filament.suggested", name="Source article") in unescape(page.text)


def test_the_page_only_shows_it_nothing_is_stored_and_later_stores_nothing(bb_client, fake_bambuddy):
    two_sorts(fake_bambuddy)
    source(bb_client, fake_bambuddy)
    location = create(bb_client)
    n = new_id(location)
    bb_client.get(location)
    assert stored(bb_client, n) == 0  # the page was opened: still nothing stored
    detail = bb_client.get(f"/articles/{n}")  # "Later" leads here
    assert en("filament.not_assigned") in detail.text
    assert stored(bb_client, n) == 0


def test_save_stores_what_the_page_shows(bb_client, fake_bambuddy):
    two_sorts(fake_bambuddy)
    _, ids = source(bb_client, fake_bambuddy, ticks=("Brand A", "Brand B"), reordered="Brand B")
    location = create(bb_client)
    n = new_id(location)
    page = bb_client.get(location).text
    data = {
        "csrf_token": csrf_of(bb_client, location), "slot": "1",
        "tick_1": [v for v, c in boxes(page) if c], "reorder_1": next(v for v, c in radios(page) if c),
    }
    response = bb_client.post(f"/articles/{n}/filaments?later=1&suggest=1", data=data)
    assert response.status_code == 303 and response.headers["location"] == f"/articles/{n}"
    with sm_of(bb_client)() as s:
        rows = {(r.sort_id, r.reordered) for r in s.query(ArticleFilamentSort).filter_by(article_id=n)}
    assert rows == {(ids["Brand A"], False), (ids["Brand B"], True)}


def test_without_the_mark_nothing_is_ticked_and_nothing_is_said(bb_client, fake_bambuddy):
    two_sorts(fake_bambuddy)
    source(bb_client, fake_bambuddy)
    n = new_id(create(bb_client))
    for path in (f"/articles/{n}/filaments", f"/articles/{n}/filaments?later=1"):  # a later visit, a changed file
        page = bb_client.get(path).text
        assert [c for _, c in boxes(page)] == [False, False] and [c for _, c in radios(page)] == [False, False]
        assert en("filament.suggested", name="Source article") not in unescape(page)


def test_the_form_keeps_both_marks_and_a_refused_save_shows_what_was_typed(bb_client, fake_bambuddy):
    two_sorts(fake_bambuddy)
    _, ids = source(bb_client, fake_bambuddy, ticks=("Brand A", "Brand B"), reordered="Brand B")
    location = create(bb_client)
    n = new_id(location)
    page = bb_client.get(location).text
    assert f'action="/articles/{n}/filaments?later=1&amp;suggest=1"' in page
    # two ticked and none named as reordered: refused; the page shows what was sent, not the suggestion again
    data = {"csrf_token": csrf_of(bb_client, location), "slot": "1", "tick_1": [str(ids["Brand A"]), str(ids["Brand B"])]}
    refused = bb_client.post(f"/articles/{n}/filaments?later=1&suggest=1", data=data)
    assert refused.status_code == 200
    assert [c for _, c in boxes(refused.text)] == [True, True] and [c for _, c in radios(refused.text)] == [False, False]
    assert stored(bb_client, n) == 0


def test_a_slot_that_has_a_group_keeps_it(bb_client, fake_bambuddy):
    two_sorts(fake_bambuddy)
    _, ids = source(bb_client, fake_bambuddy)
    n = new_id(create(bb_client))
    with sm_of(bb_client).begin() as s:
        s.add(ArticleFilamentSort(article_id=n, slot=1, sort_id=ids["Brand B"], reordered=True))
    page = bb_client.get(f"/articles/{n}/filaments?later=1&suggest=1").text
    assert (str(ids["Brand B"]), True) in boxes(page) and (str(ids["Brand A"]), False) in boxes(page)
    assert en("filament.suggested", name="Source article") not in unescape(page)


def test_the_name_of_the_source_is_escaped(bb_client, fake_bambuddy):
    two_sorts(fake_bambuddy)
    source(bb_client, fake_bambuddy, name="<b>Bold</b> & co")
    page = bb_client.get(create(bb_client)).text
    assert "<b>Bold</b>" not in page and "&lt;b&gt;Bold&lt;/b&gt; &amp; co" in page


def test_an_archived_source_gives_nothing(bb_client, fake_bambuddy):
    two_sorts(fake_bambuddy)
    source(bb_client, fake_bambuddy, archived_at=utcnow())
    page = bb_client.get(create(bb_client)).text
    assert [c for _, c in boxes(page)] == [False, False] and "filament-suggested" not in page


def test_the_colour_in_another_slot_of_the_new_file_is_found(bb_client, fake_bambuddy):
    two_sorts(fake_bambuddy)
    _, ids = source(bb_client, fake_bambuddy)  # black in slot 1 of its file
    fake_bambuddy.set_requirements(
        1, 1, [FilamentRequirement(1, 5.0, "PLA", "#FFFFFF"), FilamentRequirement(2, 42.5, "PLA", "#000000")]
    )  # black is slot 2 here
    page = bb_client.get(create(bb_client)).text
    assert boxes(page, 1) == [] or all(not c for _, c in boxes(page, 1))  # white: nobody to ask
    assert (str(ids["Brand A"]), True) in boxes(page, 2)
    assert en("filament.suggested", name="Source article") in unescape(page)
