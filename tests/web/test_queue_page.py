"""The queue page (Spec 7.2, 7.4, 7.8, 10.3; Design 2026-11-06 sections 4 and 5; plan B4 Task 12).

Reprintbuddy's own queue (drag handle, row menu that works without JavaScript), manual prints,
prints in progress, a read-only view of Bambuddy's queue and the warnings, all as live regions.
"""

import re
from datetime import datetime
from pathlib import Path

from markupsafe import escape
from sqlalchemy import select

from conftest import csrf_of, form_token_of, sm_of
from factories import make_article, make_dispatch, make_entry
from reprintbuddy import __version__
from reprintbuddy import settings as st
from reprintbuddy.domain import workqueue
from reprintbuddy.models import Article, WorkQueueEntry
from reprintbuddy.web import i18n, regions
from reprintbuddy.web.i18n import translate

STATIC = Path(i18n.__file__).parent / "static"
DRAG_SCRIPT = STATIC / "js" / "queue-drag.js"
LIVE_SCRIPT = STATIC / "js" / "live.js"
CSS = STATIC / "css" / "reprintbuddy.css"
FETCH = {"X-Requested-With": "fetch"}


def en(key, **params):
    return translate("en", key, **params)


def de(key, **params):
    return translate("de", key, **params)


def esc(text):
    """As the templates escape a text (markupsafe: also quotes)."""
    return str(escape(text))


def region(page: str, name: str) -> str:
    found = regions.extract(page, [name])
    assert name in found, f"no region {name}"
    return found[name]


def article(client, name="Template A", **fields) -> int:
    with sm_of(client).begin() as s:
        return make_article(s, name=name, **fields).id


def entry(client, article_id, prints=1, origin="rule", **fields) -> int:
    with sm_of(client).begin() as s:
        return make_entry(s, article_id, prints, origin=origin, **fields).id


def order(client) -> list[int]:
    with sm_of(client)() as s:
        return list(
            s.scalars(select(WorkQueueEntry.id).order_by(WorkQueueEntry.position, WorkQueueEntry.id))
        )


def rows(client) -> list[tuple[int, int, str, bool]]:
    with sm_of(client)() as s:
        return [
            (e.article_id, e.prints, e.origin, e.held)
            for e in s.scalars(select(WorkQueueEntry).order_by(WorkQueueEntry.position, WorkQueueEntry.id))
        ]


def manual(client, **fields):
    data = {
        "csrf_token": csrf_of(client, "/queue"),
        "form_token": form_token_of(client, "/queue", "/queue/manual"),
    }
    data.update(fields)
    return client.post("/queue/manual", data=data)


def act(client, entry_id, action, **fields):
    path = f"/queue/{entry_id}/{action}"
    data = {"csrf_token": csrf_of(client, "/queue"), "form_token": form_token_of(client, "/queue", path)}
    data.update(fields)
    return client.post(path, data=data)


def move(client, entry_id, to, *, fetch=False):
    csrf = csrf_of(client, "/queue")
    if fetch:
        return client.post(
            f"/queue/{entry_id}/move", data={"to": to}, headers={**FETCH, "X-CSRF-Token": csrf}
        )
    return client.post(f"/queue/{entry_id}/move", data={"to": to, "csrf_token": csrf})


# ---- the page ----------------------------------------------------------------------------


def test_queue_empty(bb_client):
    response = bb_client.get("/queue")
    assert response.status_code == 200
    assert en("queue.empty") in response.text
    assert "<title>" + en("queue.title") in response.text


def test_nav_queue_active(bb_client):
    page = bb_client.get("/queue").text
    active = re.findall(r'<a class="nav-link active" href="([^"]+)"', page)
    assert active and set(active) == {"/queue"}
    overview = bb_client.get("/").text
    assert re.search(r'<a class="nav-link" href="/queue" title="Queue">', overview)


def test_page_head_and_main_button(bb_client):
    page = bb_client.get("/queue").text
    assert '<header class="page-head">' in page
    assert f"<h1>{en('queue.title')}</h1>" in page
    head = re.search(r'<header class="page-head">.*?</header>', page, re.S).group(0)
    assert 'href="#manual"' in head and en("queue.manual") in head
    assert 'id="manual"' in page


def test_queue_regions(bb_client):
    page = bb_client.get("/queue").text
    expected = {
        "summary": "queue article bambuddy",
        "warnings": "bambuddy queue article settings notify",
        "entries": "queue article",
        "manual": "queue article",
        "in-progress": "queue",
        "bambuddy": "bambuddy queue",
    }
    found = dict(re.findall(r'data-live-region="([^"]+)" data-live-topics="([^"]+)"', page))
    assert found == expected


def test_kpis(bb_client):
    a = article(bb_client, "Template A")
    b = article(bb_client, "Template B", paused=True, pause_reason="queue_reduced")
    entry(bb_client, a, 3)
    entry(bb_client, a, 2, held=True)
    entry(bb_client, b, 4)
    with sm_of(bb_client).begin() as s:
        make_dispatch(s, a, status="printing", bambuddy_item_id=5)
        make_dispatch(s, a, status="completed", bambuddy_item_id=6)
    summary = region(bb_client.get("/queue").text, "summary")
    cards = dict(
        (label, int(value))
        for value, label in re.findall(
            r'<span class="kpi-value">(\d+)</span><span class="kpi-label">([^<]+)</span>', summary
        )
    )
    assert cards == {
        en("queue.kpi.waiting"): 3,
        en("queue.kpi.in_bambuddy"): 1,
        en("queue.kpi.held"): 1,
        en("queue.kpi.paused"): 1,
    }


def test_waiting_card_is_not_yellow(bb_client):
    """One colour, one meaning (Design 3): the row badge "waiting" is green, so is its card."""
    entry(bb_client, article(bb_client), 2)
    summary = region(bb_client.get("/queue").text, "summary")
    cards = re.findall(r'<div class="kpi"><span class="kpi-icon tone-(\w+)">.*?</div></div>', summary, re.S)
    labels = re.findall(r'<span class="kpi-label">([^<]+)</span>', summary)
    card = dict(zip(labels, cards, strict=True))
    assert card[en("queue.kpi.waiting")] == "accent"


def test_print_limit_comes_from_the_domain(bb_client, monkeypatch):
    """The prints fields (row menu and manual form) take their maximum from workqueue.MAX_PRINTS."""
    entry(bb_client, article(bb_client), 2)
    monkeypatch.setattr(workqueue, "MAX_PRINTS", 7)
    page = bb_client.get("/queue").text
    fields = re.findall(r'<input type="number"[^>]*name="prints"[^>]*>', page)
    assert len(fields) == 2
    assert all('max="7"' in field for field in fields), fields


def test_kpis_show_held_and_paused_only_when_present(bb_client):
    entry(bb_client, article(bb_client), 2)
    summary = region(bb_client.get("/queue").text, "summary")
    assert en("queue.kpi.waiting") in summary and en("queue.kpi.in_bambuddy") in summary
    assert en("queue.kpi.held") not in summary and en("queue.kpi.paused") not in summary


# ---- entries -----------------------------------------------------------------------------


def test_entry_rows_have_handle_and_menu(bb_client):
    a = article(bb_client)
    e = entry(bb_client, a, 2)
    page = bb_client.get("/queue").text
    entries = region(page, "entries")
    assert '<tbody class="queue-list"' in entries
    assert re.search(rf'<tr class="queue-row[^"]*" data-entry-id="{e}" data-move-url="/queue/{e}/move"', entries)
    handle = re.search(r'<button type="button" class="drag-handle[^"]*"[^>]*>', entries).group(0)
    assert f'aria-label="{en("queue.drag")}: Template A"' in handle  # p68: the row's article is named
    assert "#grip-vertical" in entries
    assert '<details class="row-menu">' in entries
    menu = entries[entries.index('<details class="row-menu">') :]
    for to in ("top", "bottom"):
        assert re.search(
            rf'<form method="post" action="/queue/{e}/move">.*?name="to" value="{to}"', menu, re.S
        ), to
    for action in ("hold", "prints", "delete"):
        assert f'action="/queue/{e}/{action}"' in menu, action
    assert en("queue.to_top") in menu and en("queue.to_bottom") in menu
    for arrow in ("▲", "▼", "⤒"):
        assert arrow not in page
    assert f'data-csrf="{csrf_of(bb_client, "/queue")}"' in entries


def test_own_queue_is_a_table_with_the_columns(bb_client):
    """p45: header cells in the order position, picture, article, prints, origin, added, state, menu."""
    a = article(bb_client)
    entry(bb_client, a, 7)
    entries = region(bb_client.get("/queue").text, "entries")
    assert '<table class="queue-table"' in entries
    head = re.search(r"<thead>(.*?)</thead>", entries, re.S).group(1)
    cells = re.findall(r"<th[^>]*>(.*?)</th>", head, re.S)
    assert len(cells) == 8
    texts = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
    expected = [en("queue.col.position"), en("queue.col.image"), en("queue.col.article"),
                en("queue.col.prints"), en("queue.col.origin"), en("queue.col.created"),
                en("queue.col.state"), en("queue.col.menu")]
    assert texts == expected


def test_prints_cell_has_its_own_class_and_big_digits(bb_client):
    a = article(bb_client)
    entry(bb_client, a, 12)
    entries = region(bb_client.get("/queue").text, "entries")
    cell = re.search(r'<td class="queue-prints[^"]*"[^>]*>(.*?)</td>', entries, re.S).group(1)
    assert re.search(r'<span class="queue-prints-n">12</span>', cell)
    css = CSS.read_text(encoding="utf-8")
    rule = re.search(r"\.queue-prints-n \{[^}]*\}", css).group(0)
    assert "font-weight: 700" in rule
    size = float(re.search(r"font-size: ([\d.]+)rem", rule).group(1))
    assert size >= 1.5


def test_every_row_has_its_handle_and_menu(bb_client):
    a = article(bb_client)
    ids = [entry(bb_client, a, n) for n in (1, 2, 3)]
    entries = region(bb_client.get("/queue").text, "entries")
    assert entries.count('class="queue-row') == 3
    assert entries.count('<button type="button" class="drag-handle') == 3
    assert entries.count('<details class="row-menu">') == 3
    for e in ids:
        assert f'data-move-url="/queue/{e}/move"' in entries


def test_table_stacks_on_a_phone_without_overflow():
    css = CSS.read_text(encoding="utf-8")
    media = css[css.index("@media (max-width: 599px)") :]
    row = re.search(r"\.queue-row \{[^}]*\}", media).group(0)
    assert "display: grid" in row
    assert "display: none" in re.search(r"\.queue-table thead \{[^}]*\}", media).group(0)
    handle = re.search(r"\.icon-btn \{[^}]*\}", css).group(0)
    assert "min-width: 2.75rem" in handle and "min-height: 2.75rem" in handle


def test_entry_row_shows_origin_prints_and_link(bb_client):
    a = article(bb_client)
    entry(bb_client, a, 3, origin="scrap")
    entries = region(bb_client.get("/queue").text, "entries")
    assert f'href="/articles/{a}"' in entries and "Template A" in entries
    assert en("queue.origin.scrap") in entries
    assert '<span class="queue-prints-n">3</span>' in entries
    assert en("queue.state.waiting") in entries


def test_paused_article_badge_carries_the_reason(bb_client):
    a = article(bb_client, paused=True, pause_reason="queue_reduced")
    entry(bb_client, a)
    entries = region(bb_client.get("/queue").text, "entries")
    badge = re.search(r'<span class="badge tone-neutral"[^>]*>[^<]*</span>', entries).group(0)
    assert en("queue.article_paused") in badge
    assert f'title="{esc(en("pause.reason.queue_reduced"))}"' in badge
    # a paused article's entry does not wait for the hand-over: no green "waiting" next to it
    assert en("queue.state.waiting") not in entries


def test_entry_actions(bb_client):
    a = article(bb_client)
    rule = entry(bb_client, a, 3)
    other = entry(bb_client, a, 1, origin="manual")
    # hold: the badge "held"; release takes it away again
    assert act(bb_client, rule, "hold").status_code == 303
    assert rows(bb_client)[0][3] is True
    entries = region(bb_client.get("/queue").text, "entries")
    assert 'class="badge tone-warn badge-held"' in entries and en("queue.state.held") in entries
    assert f'action="/queue/{rule}/release"' in entries
    assert act(bb_client, rule, "release").status_code == 303
    assert rows(bb_client)[0][3] is False
    assert "badge-held" not in region(bb_client.get("/queue").text, "entries")
    # fewer prints on a rule entry pauses the article, with the reason as message
    response = act(bb_client, rule, "prints", prints="1")
    assert response.status_code == 303 and response.headers["location"] == "/queue"
    assert rows(bb_client)[0][1] == 1
    with sm_of(bb_client)() as s:
        assert s.get(Article, a).paused is True
    # the message element itself (the paused badge carries the same text in its title)
    flash = f'<p class="alert" role="alert">{esc(en("pause.reason.queue_reduced"))}</p>'
    assert flash in bb_client.get("/queue").text
    # delete
    assert act(bb_client, other, "delete").status_code == 303
    assert order(bb_client) == [rule]


def test_entry_prints_invalid(bb_client):
    e = entry(bb_client, article(bb_client), 2)
    for bad in ("0", "abc", "9" * 20, "1001"):
        response = act(bb_client, e, "prints", prints=bad)
        assert response.status_code == 200, bad
        message = en("form.invalid_number", field=en("queue.field.prints"), min=1, max=1000)
        assert esc(message) in response.text, bad
    assert rows(bb_client)[0][1] == 2


def test_entry_action_on_a_gone_entry(bb_client):
    e = entry(bb_client, article(bb_client))
    data = {"csrf_token": csrf_of(bb_client, "/queue"), "form_token": form_token_of(bb_client, "/queue", f"/queue/{e}/hold")}
    with sm_of(bb_client).begin() as s:
        s.delete(s.get(WorkQueueEntry, e))
    response = bb_client.post(f"/queue/{e}/hold", data=data)
    assert response.status_code == 200
    assert en("queue.entry_not_found") in response.text


def test_entry_action_double_submit(bb_client):
    e = entry(bb_client, article(bb_client), origin="manual")
    data = {
        "csrf_token": csrf_of(bb_client, "/queue"),
        "form_token": form_token_of(bb_client, "/queue", f"/queue/{e}/delete"),
    }
    assert bb_client.post(f"/queue/{e}/delete", data=data).status_code == 303
    again = bb_client.post(f"/queue/{e}/delete", data=data)
    assert again.status_code == 303 and again.headers["location"] == "/queue"
    assert esc(en("form.duplicate")) in bb_client.get("/queue").text
    assert order(bb_client) == []


# ---- moving ------------------------------------------------------------------------------


def three(client) -> list[int]:
    a = article(client)
    return [entry(client, a, n) for n in (1, 2, 3)]


def test_move_forms_top_bottom(bb_client):
    e1, e2, e3 = three(bb_client)
    response = move(bb_client, e3, "top")
    assert response.status_code == 303 and response.headers["location"] == "/queue"
    assert order(bb_client) == [e3, e1, e2]
    assert move(bb_client, e3, "bottom").status_code == 303
    assert order(bb_client) == [e1, e2, e3]


def test_move_before_by_fetch_204(bb_client):
    e1, e2, e3 = three(bb_client)
    response = move(bb_client, e3, f"before:{e1}", fetch=True)
    assert response.status_code == 204
    assert order(bb_client) == [e3, e1, e2]
    assert move(bb_client, e3, "bottom", fetch=True).status_code == 204
    assert order(bb_client) == [e1, e2, e3]


def test_move_by_form_before(bb_client):
    e1, e2, e3 = three(bb_client)
    assert move(bb_client, e1, f"before:{e3}").status_code == 303
    assert order(bb_client) == [e2, e1, e3]


def test_move_target_gone(bb_client):
    e1, e2, e3 = three(bb_client)
    response = move(bb_client, e1, "before:999", fetch=True)
    assert response.status_code == 409
    assert response.text == en("queue.entry_not_found")
    assert move(bb_client, 999, "top", fetch=True).status_code == 409
    page = move(bb_client, 999, "top")
    assert page.status_code == 200 and en("queue.entry_not_found") in page.text
    assert order(bb_client) == [e1, e2, e3]


def test_move_rejects_a_bad_target(bb_client):
    """Only top, bottom and before:<id> reach the domain; anything else is a message, no 500."""
    e1, e2, e3 = three(bb_client)
    for bad in ("", "up", "TOP", "before:", "before:x", "before:-1", "before:" + "9" * 30, "top "):
        page = move(bb_client, e1, bad)
        assert page.status_code == 200, bad
        assert esc(en("queue.invalid_move")) in page.text, bad
        assert move(bb_client, e1, bad, fetch=True).status_code == 400, bad
    assert order(bb_client) == [e1, e2, e3]


def test_move_needs_the_csrf_token(bb_client):
    e1, _, _ = three(bb_client)
    assert bb_client.post(f"/queue/{e1}/move", data={"to": "bottom"}, headers=FETCH).status_code == 403


# ---- manual prints (Spec 7.4) ------------------------------------------------------------


def test_manual_print_default_end(bb_client):
    a = article(bb_client, min_stock=0)
    first = entry(bb_client, a, 1)
    data = {
        "csrf_token": csrf_of(bb_client, "/queue"),
        "form_token": form_token_of(bb_client, "/queue", "/queue/manual"),
        "article_id": str(a),
        "prints": "2",
    }
    response = bb_client.post("/queue/manual", data=data)
    assert response.status_code == 303 and response.headers["location"] == "/queue"
    assert rows(bb_client) == [(a, 1, "rule", False), (a, 2, "manual", False)]
    assert order(bb_client)[0] == first
    again = bb_client.post("/queue/manual", data=data)
    assert again.status_code == 303
    assert len(rows(bb_client)) == 2
    page = bb_client.get("/queue").text
    assert esc(en("form.duplicate")) in page


def test_manual_print_flash(bb_client):
    a = article(bb_client)
    assert manual(bb_client, article_id=str(a), prints="2", where="end").status_code == 303
    assert esc(en("queue.created", name="Template A", prints=2)) in bb_client.get("/queue").text


def test_manual_print_start_and_before(bb_client):
    a = article(bb_client)
    e1, e2 = entry(bb_client, a, 1), entry(bb_client, a, 1)
    assert manual(bb_client, article_id=str(a), prints="1", where="start").status_code == 303
    start = order(bb_client)[0]
    assert start not in (e1, e2)
    assert manual(bb_client, article_id=str(a), prints="1", where=f"before:{e2}").status_code == 303
    now = order(bb_client)
    assert now[:2] == [start, e1] and now[3] == e2 and len(now) == 4


def test_manual_form_offers_the_positions(bb_client):
    a = article(bb_client)
    article(bb_client, "Template B")
    article(bb_client, "Old", archived_at=datetime(2026, 1, 1))
    e1 = entry(bb_client, a)
    manual_region = region(bb_client.get("/queue").text, "manual")
    assert 'action="/queue/manual"' in manual_region and 'name="form_token"' in manual_region
    assert '<option value="end" selected>' in manual_region
    assert '<option value="start">' in manual_region
    assert f'<option value="before:{e1}">' in manual_region
    assert "Template B" in manual_region and "Old" not in manual_region
    assert re.search(r'name="prints"[^>]*value="1"', manual_region)


def test_manual_print_invalid(bb_client):
    a = article(bb_client)
    old = article(bb_client, "Old", archived_at=datetime(2026, 1, 1))
    prints_message = en("form.invalid_number", field=en("queue.field.prints"), min=1, max=1000)
    for bad in ("0", "abc", "9" * 20):
        response = manual(bb_client, article_id=str(a), prints=bad, where="end")
        assert response.status_code == 200, bad
        assert esc(prints_message) in response.text, bad
    response = manual(bb_client, article_id=str(old), prints="1", where="end")
    assert response.status_code == 200
    assert esc(en("queue.article_archived", name="Old")) in response.text
    response = manual(bb_client, article_id=str(a), prints="1", where="before:999")
    assert response.status_code == 200 and en("queue.entry_not_found") in response.text
    for bad_where in ("sideways", "before:x", "END"):
        response = manual(bb_client, article_id=str(a), prints="1", where=bad_where)
        assert response.status_code == 200 and esc(en("queue.invalid_where")) in response.text, bad_where
    for bad_article in ("", "x", "999"):
        response = manual(bb_client, article_id=bad_article, prints="1", where="end")
        assert response.status_code == 200, bad_article
    assert rows(bb_client) == []


def test_manual_print_keeps_what_was_typed(bb_client):
    a = article(bb_client)
    response = manual(bb_client, article_id=str(a), prints="abc", where="start")
    manual_region = region(response.text, "manual")
    assert 'value="abc"' in manual_region
    assert '<option value="start" selected>' in manual_region
    assert f'<option value="{a}" selected>' in manual_region


# ---- in progress -------------------------------------------------------------------------


def test_in_progress_table(bb_client):
    a = article(bb_client)
    with sm_of(bb_client).begin() as s:
        make_dispatch(s, a, status="printing", bambuddy_item_id=5, origin="manual")
        make_dispatch(s, a, status="completed", bambuddy_item_id=77)
    part = region(bb_client.get("/queue").text, "in-progress")
    assert "Template A" in part and en("dispatch.status.printing") in part
    assert en("queue.origin.manual") in part
    assert ">5<" in part and "77" not in part
    assert "<form" not in part


def test_every_dispatch_state_has_a_text():
    for lang in ("en", "de"):
        for state in (
            "sending", "unclear", "queued", "printing", "completed", "failed", "cancelled", "missing", "returned",
        ):  # fmt: skip
            assert translate(lang, f"dispatch.status.{state}")


# ---- Bambuddy's queue (Spec 7.8) ---------------------------------------------------------


def test_bambuddy_queue_view_only(bb_client, fake_bambuddy):
    a = article(bb_client)
    fake_bambuddy.add_foreign_item("Other print")
    own = fake_bambuddy.add_foreign_item("Template A.gcode.3mf / plate 1", created_by="someone")
    with sm_of(bb_client).begin() as s:
        make_dispatch(s, a, status="queued", bambuddy_item_id=own.id)
    part = region(bb_client.get("/queue").text, "bambuddy")
    assert "Other print" in part and "Template A.gcode.3mf / plate 1" in part
    lines = re.findall(r"<tr>.*?</tr>", part, re.S)
    other = next(line for line in lines if "Other print" in line)
    mine = next(line for line in lines if "Template A.gcode.3mf" in line)
    assert "badge-own" not in other
    assert 'class="badge tone-info badge-own"' in mine and en("queue.own_badge") in mine
    assert "<form" not in part
    assert 'href="http://bambuddy.test:8000"' in part and en("queue.open_in_bambuddy") in part
    assert en("queue.bambuddy_title") in part


def test_bambuddy_queue_marks_own_by_creator(bb_client, fake_bambuddy):
    fake_bambuddy.add_foreign_item("Template B", created_by="reprintbuddy")
    part = region(bb_client.get("/queue").text, "bambuddy")
    line = next(line for line in re.findall(r"<tr>.*?</tr>", part, re.S) if "Template B" in line)
    assert "badge-own" in line


def test_bambuddy_unavailable_on_page(bb_client, fake_bambuddy, ready_client):
    fake_bambuddy.unavailable = True
    response = bb_client.get("/queue")
    assert response.status_code == 200
    assert esc(en("queue.bambuddy_unavailable")) in region(response.text, "bambuddy")


def test_no_bambuddy_client_on_page(ready_client):
    response = ready_client.get("/queue")
    assert response.status_code == 200
    assert esc(en("queue.bambuddy_unavailable")) in region(response.text, "bambuddy")


def test_bambuddy_stale_list_is_kept(bb_client, fake_bambuddy):
    fake_bambuddy.add_foreign_item("Other print")
    assert "Other print" in region(bb_client.get("/queue").text, "bambuddy")
    fake_bambuddy.unavailable = True
    part = region(bb_client.get("/queue").text, "bambuddy")
    assert "Other print" in part
    stale_start = esc(en("queue.bambuddy_stale", at="\x00").split("\x00")[0])
    assert stale_start in part


def stale_start():
    return esc(en("queue.bambuddy_stale", at="\x00").split("\x00")[0])


def test_region_after_a_good_read_is_not_stale(bb_client, fake_bambuddy):
    """Final review 4: freshness follows the last read of the list, not `reachable`."""
    fake_bambuddy.add_foreign_item("Other print")
    bb_client.get("/queue")
    status = bb_client.app.state.ticker.status
    assert status.bambuddy_queue_ok is True
    status.configured = True  # as the first tick finds it (the fake is set after the start)
    status.reachable = False  # e.g. a hand-over without an answer

    part = bb_client.get("/queue", headers={regions.HEADER: "bambuddy"}).text

    assert "Other print" in part
    assert stale_start() not in part


def test_region_after_a_failed_read_is_stale(bb_client, fake_bambuddy):
    fake_bambuddy.add_foreign_item("Other print")
    bb_client.get("/queue")
    fake_bambuddy.unavailable = True
    bb_client.get("/queue")
    status = bb_client.app.state.ticker.status
    assert status.bambuddy_queue_ok is False
    status.configured = True
    status.reachable = True

    part = bb_client.get("/queue", headers={regions.HEADER: "bambuddy"}).text

    assert "Other print" in part
    assert stale_start() in part


def test_full_page_stores_the_list_in_the_status(bb_client, fake_bambuddy):
    item = fake_bambuddy.add_foreign_item("Other print")
    bb_client.get("/queue")
    status = bb_client.app.state.ticker.status
    assert [i.id for i in status.bambuddy_queue] == [item.id]
    assert status.bambuddy_queue_at is not None


def test_region_fetch_does_not_call_bambuddy(bb_client, fake_bambuddy):
    fake_bambuddy.add_foreign_item("Other print")
    bb_client.get("/queue")
    fake_bambuddy.calls.clear()
    response = bb_client.get("/queue", headers={regions.HEADER: "bambuddy entries"})
    assert response.status_code == 200 and response.headers[regions.HEADER] == "bambuddy entries"
    assert "Other print" in response.text
    assert "list_queue" not in fake_bambuddy.calls
    bb_client.get("/queue")
    assert "list_queue" in fake_bambuddy.calls


def test_full_page_publishes_bambuddy_only_on_change(bb_client, fake_bambuddy):
    live = bb_client.app.state.live
    published = []
    original = live.publish

    def record(topics):
        published.append(frozenset(topics))
        original(topics)

    live.publish = record
    bb_client.get("/queue")  # nothing known before: the empty list is news
    assert published == [frozenset({"bambuddy"})]
    bb_client.get("/queue")
    assert published == [frozenset({"bambuddy"})]
    fake_bambuddy.add_foreign_item("Other print")
    bb_client.get("/queue", headers={regions.HEADER: "bambuddy"})  # a region never asks
    assert len(published) == 1
    bb_client.get("/queue")
    assert published == [frozenset({"bambuddy"})] * 2
    fake_bambuddy.unavailable = True
    bb_client.get("/queue")  # the list is no longer current: told once
    assert len(published) == 3
    bb_client.get("/queue")  # a second failed call changes nothing
    assert len(published) == 3


# ---- warnings ----------------------------------------------------------------------------


def test_warnings_shown(bb_client):
    with sm_of(bb_client)() as s:
        st.set_setting(s, "queue_running", False)
    part = region(bb_client.get("/queue").text, "warnings")
    assert esc(en("warning.queue_stopped")) in part
    assert en("queue.never_polled") in part


def test_warning_tones_and_last_poll(bb_client):
    status = bb_client.app.state.ticker.status
    status.configured = True
    status.reachable = False
    status.last_poll_at = datetime(2026, 3, 1, 12, 0)
    with sm_of(bb_client)() as s:
        st.set_setting(s, "queue_running", False)
    part = region(bb_client.get("/queue").text, "warnings")
    bad = re.search(r'<div class="warning-card tone-bad"[^>]*>.*?</div>', part, re.S).group(0)
    assert esc(en("warning.bambuddy_unreachable")) in bad
    warn = re.search(r'<div class="warning-card tone-warn"[^>]*>.*?</div>', part, re.S).group(0)
    assert esc(en("warning.queue_stopped")) in warn
    assert en("queue.never_polled") not in part
    assert en("queue.last_poll", at="\x00").split("\x00")[0] in part


def test_overview_shows_warnings(ready_client):
    with sm_of(ready_client)() as s:
        st.set_setting(s, "queue_running", False)
    page = ready_client.get("/").text
    part = region(page, "warnings")
    assert esc(en("warning.queue_stopped")) in part
    assert 'data-live-region="warnings" data-live-topics="bambuddy queue article settings notify"' in page
    assert en("overview.no_articles") in page


# ---- language ----------------------------------------------------------------------------


def test_queue_german(bb_client):
    with sm_of(bb_client)() as s:
        st.set_setting(s, "language", "de")
    entry(bb_client, article(bb_client))
    page = bb_client.get("/queue").text
    for word in ("Warteschlange", "Von Hand drucken", "Zurückhalten", "In Arbeit", "nur Anzeige",
                 "Ganz nach oben", "Ganz nach unten", "Ziehen zum Verschieben"):  # fmt: skip
        assert word in page, word


# ---- the drag script and live.js (Design 5; the browser itself is checked by hand) --------


def test_drag_script_is_loaded_on_the_queue_page_only(bb_client):
    tag = f'<script src="/static/js/queue-drag.js?v={__version__}" defer></script>'
    page = bb_client.get("/queue").text
    assert page.count("queue-drag.js") == 1 and tag in page
    assert "queue-drag.js" not in bb_client.get("/").text
    assert bb_client.get(f"/static/js/queue-drag.js?v={__version__}").status_code == 200


def test_drag_script_uses_pointer_events_and_the_pages_names():
    text = DRAG_SCRIPT.read_text(encoding="utf-8")
    for name in (
        "pointerdown", "pointermove", "pointerup", "pointercancel", "setPointerCapture",
        '"Escape"', "X-Requested-With", "X-CSRF-Token", "data-csrf", "data-move-url",
        "data-entry-id", "data-live-busy", "data-live-region", '"live-idle"', '"live-refresh"',
        "queue-drag-message", '"before:"', '"bottom"',
    ):  # fmt: skip
        assert name in text, name
    for word in ("http", "/queue", "Drag", "Ziehen", "nach oben"):
        assert word not in text, word


def test_small_targets_are_44_px():
    css = CSS.read_text(encoding="utf-8")
    for selector in (r"\.queue-name", r'\.menu-prints input\[type="number"\]'):
        rules = re.findall(selector + r" \{[^}]*\}", css)
        assert any("min-height: 2.75rem" in rule for rule in rules), selector


def test_manual_fields_and_progress_link_are_44_px(bb_client):
    """Final review 5: the manual form's fields and the article link in progress."""
    css = CSS.read_text(encoding="utf-8")
    for selector in (r"\.manual-form select", r'\.manual-form input\[type="number"\]', r"\.progress-name"):
        rules = re.findall(selector + r" \{[^}]*\}", css)
        assert any("min-height: 2.75rem" in rule for rule in rules), selector
    link = re.search(r"\.progress-name \{[^}]*\}", css).group(0)
    assert "display: inline-flex" in link and "align-items: center" in link
    article_id = article(bb_client)
    with sm_of(bb_client).begin() as s:
        make_dispatch(s, article_id, status="queued", bambuddy_item_id=5)
    part = region(bb_client.get("/queue").text, "in-progress")
    assert f'<a class="progress-name" href="/articles/{article_id}">' in part


def test_drag_handle_does_not_scroll_the_page():
    css = CSS.read_text(encoding="utf-8")
    rule = re.search(r"\.drag-handle \{[^}]*\}", css).group(0)
    assert "touch-action: none" in rule
    assert "touch-action" not in re.search(r"\.queue-row \{[^}]*\}", css).group(0)


def test_drag_message_is_outside_the_regions(bb_client):
    page = bb_client.get("/queue").text
    assert re.search(r'<p id="queue-drag-message" class="alert" role="alert" hidden></p>', page)
    for name in ("entries", "summary", "warnings", "manual", "in-progress", "bambuddy"):
        assert "queue-drag-message" not in region(page, name)


def function_body(text, name):
    match = re.search(r"\n  function " + name + r"\([^)]*\) \{\n(.*?)\n  \}\n", text, re.S)
    assert match, f"function {name} missing"
    return match.group(1)


def test_live_script_leaves_a_busy_marked_region_alone():
    text = LIVE_SCRIPT.read_text(encoding="utf-8")
    assert 'hasAttribute("data-live-busy")' in function_body(text, "busy")
    assert 'addEventListener("live-idle", fetchStaleSoon)' in text
    refresh = re.search(r'addEventListener\("live-refresh", function \(event\) \{(.*?)\n  \}\);', text, re.S)
    assert refresh and "want(" in refresh.group(1)

