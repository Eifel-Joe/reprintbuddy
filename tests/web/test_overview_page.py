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
        make_dispatch(s, ok.id, status="completed", units=5)  # finished: neither planned nor in progress
        make_dispatch(s, ok.id, status="failed", units=5)
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
    assert "Reprint running" not in row  # its prints in Bambuddy are finished or failed
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
    assert en("overview.reprint_running", n=1) in row
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


def test_to_check_leads_to_assembly_when_only_assembly_waits(ready_client):
    with sm_of(ready_client).begin() as s:
        make_article(s, name="Kit", awaiting_assembly=2)
    assert card(region(ready_client.get("/").text, "summary"), "overview.kpi.to_check")[0] == "/assembly"


# ---- what the page asks of Bambuddy and the database ---------------------------------------


def spool_calls(monkeypatch):
    from reprintbuddy.web.routes import overview as route

    calls = []

    async def counted(request):
        calls.append(request.url.path)
        return None

    monkeypatch.setattr(route, "spools_or_none", counted)
    return calls


def test_only_the_key_figures_ask_for_the_spools(ready_client, monkeypatch):
    status = ready_client.app.state.ticker.status
    status.configured, status.reachable = True, True
    calls = spool_calls(monkeypatch)
    ready_client.get("/")
    assert len(calls) == 1
    for name in ("status", "warnings", "list"):
        ready_client.get("/", headers={regions.HEADER: name})
    assert len(calls) == 1
    ready_client.get("/", headers={regions.HEADER: "summary"})
    assert len(calls) == 2


def test_no_spool_query_while_bambuddy_is_down_or_not_set_up(ready_client, monkeypatch):
    """The start page does not wait for a Bambuddy that is known not to answer: the count uses the
    recorded stock, as it would after the timeout."""
    status = ready_client.app.state.ticker.status
    calls = spool_calls(monkeypatch)
    status.configured, status.reachable = True, False
    seed(ready_client)
    page = ready_client.get("/").text
    status.configured, status.reachable = False, None
    ready_client.get("/")
    assert calls == []
    assert card(region(page, "summary"), "overview.kpi.reorder")[1] == 1  # the part still counts


def test_nothing_is_written_while_writes_are_paused(ready_client, monkeypatch):
    """During a restore or an update the overview counts read-only (the rule of the activity log)."""
    from reprintbuddy.domain import reorder

    seed(ready_client)
    monkeypatch.setattr(ready_client.app.state.activity, "may_write", lambda: False)

    def no_write(*args, **kwargs):
        raise AssertionError("the overview wrote while writes are paused")

    monkeypatch.setattr(reorder, "current", no_write)
    calls = spool_calls(monkeypatch)
    page = ready_client.get("/")
    assert page.status_code == 200
    assert calls == []
    assert card(region(page.text, "summary"), "overview.kpi.reorder")[1] == 1
