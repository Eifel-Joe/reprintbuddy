"""The overview page (Spec 10.2, "Was ansteht" since 0.4.1): head line, warnings, seven cards of what is due.

No lists and no numbers per article: each card is a number with a small line and leads to its page.
`test_overview_view.py` checks what the cards make of the counts, `test_overview_head.py` the head line;
this file checks that the page counts with the functions of the pages behind the cards.
"""

import re
from datetime import date
from types import SimpleNamespace

from markupsafe import escape

from conftest import sm_of
from factories import make_article, make_assembled, make_dispatch, make_entry, make_part
from reprintbuddy import settings as st
from reprintbuddy.domain import orders
from reprintbuddy.domain.orders import ArticleItem, AssembledItem
from reprintbuddy.models import Inspection
from reprintbuddy.web import regions
from reprintbuddy.web.i18n import translate


def en(key, **params):
    return str(escape(translate("en", key, **params)))


def region(page, name):
    found = re.search(rf"<!--live:{name}-->(.*?)<!--/live:{name}-->", page, re.S)
    assert found, f"no region {name}"
    return found.group(1)


def card(part, key):
    """The card with the label of `key`: (tone, href, value, small line as text, links in the small line)."""
    found = re.search(
        r'<div class="kpi todo-card"><span class="kpi-icon tone-([a-z]+)">(?:(?!</div>).)*?'
        r'<a class="todo-main" href="([^"]+)"><span class="kpi-value">([^<]*)</span>'
        rf'<span class="kpi-label">{re.escape(en(key))}</span></a>(?:<span class="kpi-note">(.*?)</span></div>)?',
        part, re.S,
    )
    assert found, f"no card {key}"
    note = found.group(4) or ""
    links = re.findall(r'href="([^"]+)"', note)
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", note)).strip()
    value = found.group(3)
    return found.group(1), found.group(2), int(value) if value.isdigit() else value, text, links


def st_now():
    from reprintbuddy.models import utcnow

    return utcnow()


def seed(client):
    """Something on every card except filament (its stock is not known here)."""
    with sm_of(client).begin() as s:
        ok = make_article(s, name="Bracket", sku="BR-1", stock=8, reserved=0, min_stock=4, buffer=2)
        below = make_article(s, name="Clip", stock=1, min_stock=3, buffer=1, units_per_print=2)
        over = make_article(s, name="Hook", stock=0, reserved=2, min_stock=1, buffer=1, awaiting_assembly=1,
                            paused=True, pause_reason="two_failures")
        make_article(s, name="Dowel", stock=0, min_stock=2)  # nothing planned: below the minimum
        make_article(s, name="Old", stock=0, min_stock=5, archived_at=st_now())
        make_entry(s, below.id, prints=2)
        make_entry(s, ok.id, prints=1, held=True)
        make_dispatch(s, below.id, status="printing", units=2)
        make_dispatch(s, over.id, status="queued", units=1)
        done = make_dispatch(s, ok.id, status="completed", units=3)
        s.add(Inspection(dispatch_id=done.id, article_id=ok.id, units=3))  # a print waiting for the check
        make_dispatch(s, ok.id, status="failed", units=5)
        make_part(s, name="Insert", stock=0, min_stock=10)  # a part below its minimum: one suggestion
        # Assembly: built on order, 2 ordered; a variant on stock below its target is no assembly due.
        segment = make_article(s, name="Segment")
        hose = make_assembled(s, name="Hose", sku="EX-H1", mode="order", base=[(segment.id, 2)])
        make_assembled(s, name="Box", sku="EX-B1", mode="stock", base=[(segment.id, 1)], min_stock=2, buffer=1)
        shipped_soon = orders.create_order(
            s, reference="R-1", order_date=date(2026, 11, 11), items=[ArticleItem(ok.id, 1)], actor="user")
        st.set_setting(s, "orders_seen_id", shipped_soon.id, commit=False)  # R-1 seen, R-2 new
        orders.create_order(
            s, reference="R-2", order_date=date(2026, 11, 11), items=[AssembledItem(hose.id, 2)], actor="user")


def test_seven_cards_count_what_is_due(ready_client):
    from reprintbuddy.domain import assembled

    seed(ready_client)
    with sm_of(ready_client).begin() as s:
        assert assembled.list_buildable(s)  # the stock variant waits there, and still does not count
    part = region(ready_client.get("/").text, "summary")
    assert card(part, "overview.todo.orders") == (
        "warn", "/orders", 2, f'{en("overview.todo.new", n=1)} · {en("overview.todo.orders_ready", n=1)}',
        ["/orders?status=ready"],
    )
    assert card(part, "overview.todo.inspection") == ("warn", "/inspection", 1, en("overview.todo.pieces", n=3), [])
    assert card(part, "overview.todo.assembly") == ("warn", "/assembly", 1 + 2, "", [])
    assert card(part, "overview.todo.reorder")[:3] == ("warn", "/reorder", 1)
    # 1 printing; waiting: 2 + 1 (held) in Reprintbuddy's queue, 1 handed over and not started
    assert card(part, "overview.todo.prints") == ("warn", "/queue", 1, en("overview.todo.waiting", n=4), [])
    # red: Dowel, the box on stock below its target; overbooked: Hook, Segment (R-2 reserves 4 of 0)
    assert card(part, "overview.todo.below_min") == (
        "bad", "/articles?status=below_min", 4, en("overview.todo.overbooked", n=2), ["/articles?status=overbooked"],
    )


def test_the_cards_follow_what_they_count(ready_client):
    page = ready_client.get("/").text
    assert ('data-live-region="summary" '
            'data-live-topics="article order queue inspection bambuddy filament reorder part journal settings"' in page)
    assert "data-live-region=\"list\"" not in page
    only = ready_client.get("/", headers={regions.HEADER: "summary"})
    assert 'class="todo-grid"' in only.text


def test_no_lists_and_no_numbers_per_article(ready_client):
    seed(ready_client)
    page = ready_client.get("/").text
    assert "<table" not in page
    assert "Clip" not in page and "Bracket" not in page


def test_filament_short_counts_the_spool_sorts_with_a_suggestion(ready_client, monkeypatch):
    from reprintbuddy.domain import reorder

    found = [SimpleNamespace(item_type="filament", item_id=1), SimpleNamespace(item_type="filament", item_id=2),
             SimpleNamespace(item_type="part", item_id=1)]
    monkeypatch.setattr(reorder, "current", lambda *a, **k: reorder.Current({}, None, found))
    part = region(ready_client.get("/").text, "summary")
    assert card(part, "overview.todo.filament") == ("warn", "/filament", 2, "", [])
    assert card(part, "overview.todo.reorder")[2] == 3
    monkeypatch.setattr(reorder, "current", lambda *a, **k: reorder.Current(None, None, found[2:]))
    part = region(ready_client.get("/").text, "summary")
    assert card(part, "overview.todo.filament") == ("neutral", "/filament", en("overview.todo.stock_unknown"), "", [])


def test_new_suggestions_since_the_reorder_page_was_loaded(ready_client):
    with sm_of(ready_client).begin() as s:
        make_part(s, name="Insert", stock=0, min_stock=10)
    ready_client.get("/reorder")
    with sm_of(ready_client).begin() as s:
        make_part(s, name="Nut", stock=0, min_stock=10)
    part = region(ready_client.get("/").text, "summary")
    assert card(part, "overview.todo.reorder")[2:4] == (2, en("overview.todo.new", n=1))
    ready_client.get("/")  # looking at the overview clears nothing
    assert card(region(ready_client.get("/").text, "summary"), "overview.todo.reorder")[3] == en("overview.todo.new", n=1)


def test_empty_database(ready_client):
    page = ready_client.get("/").text
    part = region(page, "summary")
    assert card(part, "overview.todo.orders") == ("neutral", "/orders", 0, "", [])
    assert en("overview.no_articles") in page
    assert 'href="/articles/new"' in page


def test_only_archived_articles_count_as_empty(ready_client):
    with sm_of(ready_client).begin() as s:
        make_article(s, name="Old", archived_at=st_now())
    assert en("overview.no_articles") in ready_client.get("/").text


def test_with_an_article_no_empty_start(ready_client):
    with sm_of(ready_client).begin() as s:
        make_article(s, name="New")
    assert en("overview.no_articles") not in ready_client.get("/").text


def test_german(ready_client):
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    page = ready_client.get("/").text
    for text in ("Was ansteht", "Bestellungen", "Zu prüfen", "Montage", "Bestellvorschläge", "Drucke", "Filament",
                 "Unter Mindestbestand"):
        assert text in page


# ---- the spools: only the cards need them, never while Bambuddy is known to be down ------------------


def spool_calls(monkeypatch):
    from reprintbuddy.web import filament_view as route  # the overview asks through `page_spools`

    calls = []

    async def counted(request):
        calls.append(request.url.path)
        return None

    monkeypatch.setattr(route, "spools_or_none", counted)
    return calls


def test_only_the_cards_ask_for_the_spools(ready_client, monkeypatch):
    status = ready_client.app.state.ticker.status
    status.configured, status.reachable = True, True
    calls = spool_calls(monkeypatch)
    ready_client.get("/")
    assert len(calls) == 1
    for name in ("status", "warnings", "empty"):
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
    assert card(region(page, "summary"), "overview.todo.reorder")[2] == 1  # the part still counts


def test_nothing_is_written_while_writes_are_paused(ready_client, monkeypatch):
    """During a restore or an update the overview counts read-only (the rule of the activity log)."""
    from reprintbuddy.domain import reorder

    seed(ready_client)
    monkeypatch.setattr(ready_client.app.state.activity, "may_write", lambda: False)
    real = reorder.current

    def read_only(*args, **kwargs):
        assert kwargs.get("write") is False, "the overview wrote while writes are paused"
        return real(*args, **kwargs)

    monkeypatch.setattr(reorder, "current", read_only)
    calls = spool_calls(monkeypatch)
    page = ready_client.get("/")
    assert page.status_code == 200
    assert calls == []
    assert card(region(page.text, "summary"), "overview.todo.reorder")[2] == 1
