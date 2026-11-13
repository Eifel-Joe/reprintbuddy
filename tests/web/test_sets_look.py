"""The set pages in the new look: status badges, short summary, search, filters, tiles or list and
live regions (Spec 10.1, plan Task 13)."""

import re

import pytest
from conftest import csrf_of, sm_of
from factories import make_article, make_part, make_set

from reprintbuddy import settings as st
from reprintbuddy.domain import catalog, stock
from reprintbuddy.models import ProductSet, utcnow
from reprintbuddy.web import regions
from reprintbuddy.web.i18n import translate


def en(key, **params):
    return translate("en", key, **params)


def kpis_of(page):
    """The cards of the summary region as (value, label) pairs."""
    block = regions.extract(page, ["summary"])["summary"]
    return [
        (int(value), label)
        for value, label in re.findall(r'<span class="kpi-value">(\d+)</span><span class="kpi-label">(.*?)</span>', block)
    ]


def text_of(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html)).strip()


def region_of(page, name):
    return regions.extract(page, [name])[name]


def list_of(page):
    return region_of(page, "list")


def set_with(client, name, sku=None, **article_fields):
    """A set of 1 × a new article and 1 × a plentiful box (a set has two articles at least, p340);
    returns (set id, article id): the new article decides the availability."""
    with sm_of(client).begin() as s:
        article = make_article(s, name=f"Part of {name}", **article_fields)
        box = make_article(s, name=f"Box of {name}", stock=99)
        product_set = make_set(s, name, [(1, article.id), (1, box.id)])
        product_set.sku = sku
        return product_set.id, article.id


def two_states(client):
    set_with(client, "Starter Set", sku="S-100", stock=2)
    set_with(client, "Big Set", sku="S-200", stock=0)


def test_set_row_has_a_status(ready_client):
    two_states(ready_client)
    with sm_of(ready_client).begin() as s:
        states = {row.name: row.status for row in catalog.list_sets(s)}
    assert states == {"Starter Set": "deliverable", "Big Set": "not_deliverable"}


@pytest.mark.parametrize(
    "stock_of_article,text,classes",
    [(1, "deliverable", "tone-ok"), (0, "not deliverable", "tone-bad")],
)
def test_set_badges(ready_client, stock_of_article, text, classes):
    set_with(ready_client, "Starter Set", stock=stock_of_article)
    assert f'<span class="badge {classes}">{text}</span>' in list_of(ready_client.get("/sets").text)


def test_sets_summary_filter_and_search(ready_client):
    two_states(ready_client)
    page = ready_client.get("/sets").text
    assert kpis_of(page) == [(2, "Sets"), (1, "not deliverable")]
    assert '<p class="summary">' not in page
    only = ready_client.get("/sets?status=not_deliverable").text
    assert "Big Set" in list_of(only) and "Starter Set" not in list_of(only)
    filters = region_of(only, "filters")
    assert re.search(r'class="filter active" href="/sets\?status=not_deliverable"', filters)
    assert re.search(r'>All <span class="count">2</span>', filters)
    by_number = ready_client.get("/sets?q=s-1").text
    assert "Starter Set" in list_of(by_number) and "Big Set" not in list_of(by_number)
    by_name = ready_client.get("/sets?q=BIG").text
    assert "Big Set" in list_of(by_name) and "Starter Set" not in list_of(by_name)
    nothing = ready_client.get("/sets?q=%25").text
    assert "Nothing matches the search or filter." in list_of(nothing)
    assert ready_client.get("/sets?status=bogus").status_code == 200


def test_sets_summary_in_german(ready_client):
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    two_states(ready_client)
    assert kpis_of(ready_client.get("/sets").text) == [(2, "Sets"), (1, "nicht lieferbar")]


def test_kpi_cards_skip_states_without_entries(ready_client):
    set_with(ready_client, "Starter Set", stock=2)
    assert kpis_of(ready_client.get("/sets").text) == [(1, "Sets")]


def test_sets_list_and_tiles(ready_client):
    set_id, _ = set_with(ready_client, "Starter Set", sku="S-100", stock=3)
    listing = list_of(ready_client.get("/sets").text)
    assert 'class="collection layout-auto"' in listing
    table = re.search(r"<table>.*?</table>", listing, re.S).group(0)
    assert f'<a href="/sets/{set_id}/edit">Starter Set</a>' in table and "S-100" in table
    assert '<td class="num">3</td>' in table and "<span>1 × Part of Starter Set</span>" in table
    tile = re.search(rf'<a class="tile" href="/sets/{set_id}/edit">.*?</a>', listing, re.S).group(0)
    assert "Starter Set" in tile and "S-100" in tile and '<span class="badge tone-ok">deliverable</span>' in tile
    assert "<strong>3</strong>" in tile
    assert re.search(r'class="tile-foot">.*1 × Part of Starter Set', tile, re.S)


def test_sets_head_and_buttons(ready_client):
    page = ready_client.get("/sets").text
    assert 'class="page-head"' in page and "#boxes" in page
    assert re.search(r'<a class="btn" href="/sets/new">.*?#plus.*?New set</a>', page, re.S)
    assert re.search(r'<a class="btn secondary" href="/sets\?view=archived">.*?#archive', page, re.S)
    archive = ready_client.get("/sets?view=archived").text
    assert re.search(r'<a class="btn secondary" href="/sets">.*?#archive.*?Show active', archive, re.S)


def test_archived_sets_show_the_archived_badge_and_no_filters(ready_client):
    set_id, _ = set_with(ready_client, "Old Set", stock=0)
    with sm_of(ready_client).begin() as s:
        s.get(ProductSet, set_id).archived_at = utcnow()
    page = ready_client.get("/sets?view=archived").text
    assert '<span class="badge tone-neutral">Archived</span>' in list_of(page)
    assert "not deliverable" not in list_of(page)
    assert 'data-live-region="filters"' not in page
    assert kpis_of(page) == [(1, "Archived sets")]


def test_sets_view_choice_is_its_own(ready_client):
    two_states(ready_client)
    ready_client.cookies.set("rb_layout_parts", "tiles")
    assert 'class="collection layout-auto"' in ready_client.get("/sets").text
    response = ready_client.get("/sets?layout=list")
    assert response.status_code == 303 and response.headers["location"] == "/sets"
    assert 'class="collection layout-list"' in ready_client.get("/sets").text


def test_set_list_follows_article_stock(ready_client):
    _, article_id = set_with(ready_client, "Starter Set", stock=0)
    page = ready_client.get("/sets").text
    for name in ("summary", "filters", "list"):
        assert f'data-live-region="{name}" data-live-topics="set article"' in page, name
    with sm_of(ready_client).begin() as s:
        stock.count_article(s, article_id, "stock", 4, "user")
    piece = ready_client.get("/sets", headers={regions.HEADER: "list"}).text
    assert '<span class="badge tone-ok">deliverable</span>' in piece


def test_empty_sets_list(ready_client):
    assert "No sets yet." in list_of(ready_client.get("/sets").text)


def test_set_edit_watches_its_set(ready_client):
    set_id, _ = set_with(ready_client, "Starter Set", stock=1)
    page = ready_client.get(f"/sets/{set_id}/edit").text
    assert f'data-live-region="form" data-live-topics="set:{set_id} article"' in page
    form = region_of(page, "form")
    assert re.search(rf'<form class="set-form" method="post" action="/sets/{set_id}/edit" data-live-watch="set:{set_id}" data-set-form>\s*<p class="live-stale" hidden', form)
    assert 'class="page-head"' in page


def test_new_set_form_offers_an_article_created_elsewhere(ready_client):
    page = ready_client.get("/sets/new").text
    assert 'data-live-region="form" data-live-topics="article"' in page
    assert "data-live-watch" not in page and 'class="page-head"' in page
    with sm_of(ready_client).begin() as s:
        make_article(s, name="Template Z")
    piece = ready_client.get("/sets/new", headers={regions.HEADER: "form"}).text
    assert piece.startswith('<div class="live-region" data-live-region="form"')
    assert ">Template Z</option>" in piece


def test_more_rows_page_has_no_region_source(ready_client):
    with sm_of(ready_client).begin() as s:
        a = make_article(s, name="Template A").id
    data = {"csrf_token": csrf_of(ready_client, "/sets/new"), "form_token": "x", "name": "Typed Set",
            "base_rows": "3", "base_0_qty": "1", "base_0_article": str(a), "action": "more_base"}
    response = ready_client.post("/sets/new", data=data)
    assert response.status_code == 200
    assert 'data-live-src=""' in response.text and 'value="Typed Set"' in response.text


_MARK = re.compile(r"<!--(/?)live:([^>]*?)-->")


def test_edit_pages_have_one_balanced_form_region(ready_client):
    """The edit pages need an id, so the shared structure test in test_regions.py cannot reach them."""
    set_id, _ = set_with(ready_client, "Starter Set", stock=1)
    with sm_of(ready_client).begin() as s:
        part_id = make_part(s).id
    for path in (f"/sets/{set_id}/edit", f"/parts/{part_id}/edit"):
        marks = _MARK.findall(ready_client.get(path).text)
        assert marks == [("", "form"), ("/", "form")], (path, marks)


def test_failed_set_edit_has_no_region_source(ready_client):
    set_id, article_id = set_with(ready_client, "Starter Set", stock=1)
    data = {"csrf_token": csrf_of(ready_client, f"/sets/{set_id}/edit"), "name": "Typed Name", "sku": "EX-TN",
            "base_rows": "3"}  # no article: invalid
    response = ready_client.post(f"/sets/{set_id}/edit", data=data)
    assert response.status_code == 200 and en("sets.too_few_articles") in response.text
    assert 'data-live-src=""' in response.text and 'value="Typed Name"' in response.text


def test_negative_available_is_red_in_the_tile(ready_client):
    set_with(ready_client, "Starter Set", stock=0, reserved=1)
    tile = re.search(r'<a class="tile".*?</a>', list_of(ready_client.get("/sets").text), re.S).group(0)
    assert '<strong class="neg">-1</strong>' in tile


def test_set_badge_sits_in_the_badges_wrapper_like_the_articles(ready_client):
    set_with(ready_client, "Starter Set", stock=1)
    listing = list_of(ready_client.get("/sets").text)
    assert listing.count('<span class="badges"><span class="badge tone-ok">deliverable</span></span>') == 2


def test_set_form_head_has_a_button_back_to_the_sets(ready_client):
    set_id, _ = set_with(ready_client, "Starter Set", stock=1)
    for path in (f"/sets/{set_id}/edit", "/sets/new"):
        head = re.search(r'<header class="page-head">.*?</header>\s*<div class="toolbar">.*?</div>\s*</div>',
                         ready_client.get(path).text, re.S)
        assert head, path
        assert re.search(r'<a class="btn secondary" href="/sets">.*?#boxes.*?Sets</a>', head.group(0), re.S), path


def test_head_has_subtitle_and_new_button(ready_client):
    page = ready_client.get("/sets").text
    head = page[page.index('<header class="page-head">') : page.index("</header>")]
    assert f'<p class="page-sub">{en("sets.subtitle")}</p>' in head
    assert en("sets.subtitle") == "Bundles of articles sold together"
    actions = head[head.index('<div class="page-actions">') :]
    assert 'href="/sets/new"' in actions and en("sets.new") in actions
    assert 'href="/sets/new"' not in page[page.index('<div class="toolbar">') : page.index("<!--live:list-->")]


def row_actions_of(page):
    block = list_of(page)
    return block[block.index('<td class="actions-col">') :].split("</td>")[0]


def row_token(page, set_id, verb="archive"):
    form = re.search(rf'<form class="icon-form"[^>]*action="/sets/{set_id}/{verb}">(.*?)</form>', page, re.S).group(1)
    return re.search(r'name="form_token" value="([^"]+)"', form).group(1)


def test_row_has_edit_and_archive_but_no_journal(ready_client):
    set_id, _ = set_with(ready_client, "Starter Set", stock=1)
    page = ready_client.get("/sets").text
    cell = row_actions_of(page)
    assert f'href="/sets/{set_id}/edit"' in cell and f'title="{en("sets.edit")}"' in cell
    assert f'<form class="icon-form" method="post" action="/sets/{set_id}/archive">' in cell
    assert 'name="csrf_token"' in cell and 'name="form_token"' in cell
    assert f'title="{en("catalog.archive")}"' in cell and "/restore" not in cell and "/journal" not in cell
    assert f'<span class="sr-only">{en("list.col.actions")}</span>' in list_of(page)
    assert list_of(page).count(f"/sets/{set_id}/archive") == 1  # tiles have no actions


def test_archived_row_has_only_restore(ready_client):
    set_id, _ = set_with(ready_client, "Old Set", stock=0)
    with sm_of(ready_client).begin() as s:
        s.get(ProductSet, set_id).archived_at = utcnow()
    cell = row_actions_of(ready_client.get("/sets?view=archived").text)
    assert f'action="/sets/{set_id}/restore"' in cell
    assert "/edit" not in cell and "/journal" not in cell and "/archive" not in cell.replace("archive-restore", "")


def test_archive_from_the_row_goes_back_to_the_list(ready_client):
    set_id, _ = set_with(ready_client, "Starter Set", stock=1)
    page = ready_client.get("/sets").text
    response = ready_client.post(
        f"/sets/{set_id}/archive",
        data={"csrf_token": csrf_of(ready_client, "/sets"), "form_token": row_token(page, set_id)},
    )
    assert response.status_code == 303 and response.headers["location"] == "/sets"
    with sm_of(ready_client).begin() as s:
        assert s.get(ProductSet, set_id).archived_at is not None


def test_summary_region_carries_cards_and_fresh_form_tokens(ready_client):
    set_id, _ = set_with(ready_client, "Starter Set", stock=0)
    response = ready_client.get("/sets", headers={regions.HEADER: "summary list"})
    assert 'data-live-region="summary"' in response.text and 'class="kpis"' in response.text
    token = row_token(response.text, set_id)
    again = ready_client.post(
        f"/sets/{set_id}/archive",
        data={"csrf_token": csrf_of(ready_client, "/sets"), "form_token": token},
    )
    assert again.status_code == 303  # the token of the refetched region is valid


def test_set_form_head_has_a_subtitle(ready_client):
    set_id, _ = set_with(ready_client, "Starter Set", stock=1)
    for path in (f"/sets/{set_id}/edit", "/sets/new"):
        head = re.search(r'<header class="page-head">.*?</header>', ready_client.get(path).text, re.S).group(0)
        assert f'<p class="page-sub">{en("sets.form_subtitle")}</p>' in head, path
    assert en("sets.form_subtitle") == "Name, number, base parts and choice parts of the set"


def variant_set(client):
    """Grundset-like: 1 × frame (4), choice parts A (2), B (0), C (1), suffixes -A, -B, -C."""
    with sm_of(client).begin() as s:
        frame = make_article(s, name="Frame", stock=4).id
        a, b, c = (make_article(s, name=f"Plate {x}", stock=n).id for x, n in (("A", 2), ("B", 0), ("C", 1)))
        return make_set(s, "Kit", [(1, frame)], [(1, [(a, "-A"), (b, "-B"), (c, "-C")])], sku="EX-S10").id


def test_list_shows_together_and_each_variant(ready_client):  # Spec 5.4, 10.1 (p340)
    set_id = variant_set(ready_client)
    table = re.search(r"<table>.*?</table>", list_of(ready_client.get("/sets").text), re.S).group(0)
    assert '<td class="num">3</td>' in table  # together at most 3
    assert "<span>1 × Frame</span>" in table
    assert '<span class="variant">EX-S10-A · <strong>2</strong></span>' in table
    assert '<span class="variant">EX-S10-B · <strong>0</strong></span>' in table
    assert f'<a href="/sets/{set_id}/edit">Kit</a>' in table


def test_tile_shows_variants(ready_client):
    set_id = variant_set(ready_client)
    tile = re.search(rf'<a class="tile" href="/sets/{set_id}/edit">.*?</a>', list_of(ready_client.get("/sets").text), re.S).group(0)
    assert "<strong>3</strong>" in tile and "EX-S10-C · <strong>1</strong>" in tile


def test_incomplete_set_has_grey_badge(ready_client):  # sets the update rebuilt (p340)
    with sm_of(ready_client).begin() as s:
        frame = make_article(s, name="Frame", stock=4).id
        a = make_article(s, name="Plate A", stock=2).id
        make_set(s, "Kit", [(1, frame)], [(1, [(a, None)])], sku="EX-S10")
    listing = list_of(ready_client.get("/sets").text)
    assert '<span class="badge tone-neutral">suffixes missing</span>' in listing
    assert '<span class="variant">Plate A · <strong>2</strong></span>' in listing  # no number yet: the article


def test_search_finds_a_set_by_variant_number(ready_client):
    variant_set(ready_client)
    assert "Kit" in list_of(ready_client.get("/sets?q=s10-b").text)
    assert "Kit" not in list_of(ready_client.get("/sets?q=s10-x").text)
