"""The overview page (Spec 10.2): key figures and all articles as a table, live regions, an empty start.

The numbers come from the article list's rows (`catalog.list_articles`), the queue and the reorder count;
`test_overview_view.py` checks the counting, this file what the page shows of it.
"""

import re

from markupsafe import escape

from conftest import sm_of
from factories import make_article, make_dispatch, make_entry, make_part
from reprintbuddy import settings as st
from reprintbuddy.web import regions
from reprintbuddy.web.i18n import translate


def en(key, **params):
    return str(escape(translate("en", key, **params)))


def region(page, name):
    found = re.search(rf"<!--live:{name}-->(.*?)<!--/live:{name}-->", page, re.S)
    assert found, f"no region {name}"
    return found.group(1)


def card(part, key):
    """The key figure card with the label of `key`: (href, value, note)."""
    found = re.search(
        r'<a class="kpi kpi-link" href="([^"]+)">(?:(?!</a>).)*?<span class="kpi-value">(\d+)</span>'
        rf'<span class="kpi-label">{re.escape(en(key))}</span>(?:<span class="kpi-note">([^<]*)</span>)?',
        part, re.S,
    )
    assert found, f"no card {key}"
    return found.group(1), int(found.group(2)), found.group(3) or ""


def table_row(part, name):
    found = re.search(rf'<tr[^>]*>(?:(?!</tr>).)*?>{re.escape(name)}</a>.*?</tr>', part, re.S)
    assert found, f"no row {name}"
    return found.group(0)


def cells(row_html):
    return [re.sub(r"<[^>]+>", " ", c).split() for c in re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S)]


def seed(client):
    with sm_of(client).begin() as s:
        ok = make_article(s, name="Bracket", sku="BR-1", stock=8, reserved=1, min_stock=4, buffer=2)
        below = make_article(s, name="Clip", stock=1, min_stock=3, buffer=1, units_per_print=2)
        over = make_article(s, name="Hook", stock=0, reserved=2, min_stock=1, buffer=1, provisional=3,
                            awaiting_assembly=1, paused=True, pause_reason="two_failures")
        make_article(s, name="Dowel", stock=0, min_stock=2)  # nothing planned: below the minimum
        make_article(s, name="Old", stock=0, min_stock=5, archived_at=st_now())
        make_entry(s, below.id, prints=2)
        make_entry(s, ok.id, prints=1, held=True)
        make_dispatch(s, below.id, status="printing", units=2)
        make_dispatch(s, over.id, status="queued", units=1)
        make_part(s, name="Insert", stock=0, min_stock=10)  # a part below its minimum: one suggestion
        return ok.id, below.id, over.id


def st_now():
    from reprintbuddy.models import utcnow

    return utcnow()


# ---- key figures ---------------------------------------------------------------------------


def test_four_key_figures_with_data(ready_client):
    seed(ready_client)
    part = region(ready_client.get("/").text, "summary")
    assert card(part, "overview.kpi.below_min") == ("/articles", 2, en("overview.kpi.overbooked_note", n=1))
    # 2 + 1 prints in the queue (the held entry counts), 2 open prints in Bambuddy
    assert card(part, "overview.kpi.planned") == ("/queue", 5, en("overview.kpi.planned_note", queued=3, open=2))
    assert card(part, "overview.kpi.to_check") == ("/inspection", 4, en("overview.kpi.to_check_note", check=3, assembly=1))
    assert card(part, "overview.kpi.reorder") == ("/reorder", 1, "")


def test_summary_region_follows_what_it_counts(ready_client):
    page = ready_client.get("/").text
    assert ('data-live-region="summary" data-live-topics="article queue inspection bambuddy filament reorder part journal"'
            in page)
    only = ready_client.get("/", headers={regions.HEADER: "summary"})
    assert 'class="kpis"' in only.text and "<table" not in only.text


# ---- the table -----------------------------------------------------------------------------


def test_table_has_the_columns_of_the_spec(ready_client):
    seed(ready_client)
    part = region(ready_client.get("/").text, "list")
    heads = re.findall(r"<th(?:\s[^>]*)?>(.*?)</th>", part, re.S)
    assert heads == [
        en("articles.col.name"), en("articles.col.available"), en("overview.col.reserved"),
        en("overview.col.min_target"), en("overview.col.in_progress"), en("overview.col.to_check"),
        en("overview.col.fill"), en("list.col.status"),
    ]


def test_table_rows_show_the_numbers(ready_client):
    ok, below, over = seed(ready_client)
    part = region(ready_client.get("/").text, "list")
    assert part.index(">Bracket<") < part.index(">Clip<") < part.index(">Dowel<") < part.index(">Hook<")  # by name
    assert ">Old<" not in part  # archived
    row = table_row(part, "Bracket")
    assert f'href="/articles/{ok}"' in row and "BR-1" in row
    assert cells(row)[1:7] == [["7"], ["1"], ["4", "/", "6"], ["0"], ["0"], []]
    assert 'aria-valuenow="100"' in row  # 7 of 6
    assert en("status.article.ok") in row
    row = table_row(part, "Clip")
    # available 1 < 3, coverage 1 + 4 queued + 2 in progress = 7: yellow
    assert cells(row)[1:6] == [["1"], ["0"], ["3", "/", "4"], ["2"], ["0"]]
    assert en("status.article.low") in row
    assert en("overview.reprint_running", n=1) in row
    assert 'aria-valuenow="25"' in row
    row = table_row(part, "Hook")
    assert 'class="num neg"' in row  # available -2 in red
    assert cells(row)[1:6] == [["-2"], ["2"], ["1", "/", "2"], ["1"], ["4"]]
    assert en("status.article.overbooked") in row and en("status.paused") in row
    assert 'aria-valuenow="0"' in row
    row = table_row(part, "Dowel")
    assert en("status.article.below_min") in row
    assert en("overview.reprint_running", n=1).split("(")[0] not in row  # nothing in Bambuddy


def test_list_region_follows_articles_queue_and_inspection(ready_client):
    seed(ready_client)
    page = ready_client.get("/").text
    assert 'data-live-region="list" data-live-topics="article queue inspection"' in page
    only = ready_client.get("/", headers={regions.HEADER: "list"})
    assert ">Clip<" in only.text and 'class="kpis"' not in only.text


def test_each_cell_has_its_label_for_the_phone(ready_client):
    """At phone width each row is a card; the cells carry their column name (Spec 10.1: one column)."""
    seed(ready_client)
    row = table_row(region(ready_client.get("/").text, "list"), "Clip")
    labels = re.findall(r'<td[^>]*data-label="([^"]*)"', row)
    assert labels == [
        en("articles.col.available"), en("overview.col.reserved"), en("overview.col.min_target"),
        en("overview.col.in_progress"), en("overview.col.to_check"), en("overview.col.fill"),
    ]


# ---- a fresh database ----------------------------------------------------------------------


def test_empty_database(ready_client):
    page = ready_client.get("/").text
    summary = region(page, "summary")
    for key in ("overview.kpi.below_min", "overview.kpi.planned", "overview.kpi.to_check", "overview.kpi.reorder"):
        assert card(summary, key)[1] == 0
    part = region(page, "list")
    assert "<table" not in part
    assert en("overview.no_articles") in part
    assert '<a class="btn" href="/articles/new">' in part


def test_only_archived_articles_count_as_empty(ready_client):
    with sm_of(ready_client).begin() as s:
        make_article(s, name="Old", archived_at=st_now())
    assert en("overview.no_articles") in region(ready_client.get("/").text, "list")


def test_german(ready_client):
    seed(ready_client)
    with sm_of(ready_client)() as s:
        st.set_setting(s, "language", "de")
    page = ready_client.get("/").text
    assert "Unter Mindestbestand" in page and "Geplant und in Arbeit" in page
    assert "Nachdruck läuft (1)" in page
    assert "Füllstand" in page
