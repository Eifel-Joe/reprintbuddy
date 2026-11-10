"""What the push messages report and when (Spec 10.6 "Ereignisse", "Keine Flut", "Inhalt").

`detect` brings the marks up to date after a tick, `due` bundles what is to be sent into one message
per kind, `record` keeps the outcome. No network: these are functions over the database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from factories import (
    assign_sort,
    make_article,
    make_dispatch,
    make_entry,
    make_filament_settings,
    make_part,
    make_snapshot,
    make_sort,
)
from sqlalchemy import select

from reprintbuddy import settings as st
from reprintbuddy.domain.engine import EngineStatus
from reprintbuddy.models import NotifyMark, ReorderMark
from reprintbuddy.notify import events as ev

NOW = datetime(2026, 11, 9, 12, 0)  # naive UTC, as stored
SINCE = datetime(2026, 11, 9, 8, 0, tzinfo=UTC)
BASE = "http://192.0.2.7:8080"


def ctx(*, now=NOW, poll=False, status=None, filament_known=True, rights=None, base=BASE) -> ev.Context:
    return ev.Context(
        now=now,
        poll=poll,
        status=status or EngineStatus(configured=True, reachable=True),
        filament_known=filament_known,
        rights=rights or ev.Rights("ok", ()),
        base=base,
    )


@pytest.fixture
def db(db):
    """Messages switched on at `SINCE`, all events ticked, English, names on."""
    with db.begin() as s:
        st.set_setting(s, "ha_since", SINCE.isoformat(), commit=False)
    return db


def round_(db, context=None) -> list[ev.Outgoing]:
    context = context or ctx()
    with db.begin() as s:
        ev.detect(s, context)
        return ev.due(s, context, exclude=frozenset())


def deliver(db, outgoing, outcome="accepted", now=NOW) -> None:
    with db.begin() as s:
        for out in outgoing:
            ev.record(s, out, outcome, now)


def send_round(db, context=None, outcome="accepted") -> list[ev.Outgoing]:
    out = round_(db, context)
    deliver(db, out, outcome)
    return out


def kinds(outgoing) -> list[str]:
    return [o.kind for o in outgoing]


def finished(s, article, *, status="completed", at=NOW - timedelta(minutes=5), units=1):
    return make_dispatch(s, article.id, status=status, units=units, finished_at=at)


# ---- 1 print done -------------------------------------------------------------------------------


def test_a_finished_print_reports_once_with_name_units_and_link(db):
    with db.begin() as s:
        a = make_article(s, name="Bracket")
        finished(s, a, units=2)
    out = send_round(db)
    assert kinds(out) == ["print_done"]
    msg = out[0].message
    assert msg.title == "Ready for inspection"
    assert msg.text == "1 print waits for inspection: Bracket (2 pcs)"
    assert msg.link == f"{BASE}/inspection"
    assert send_round(db) == []  # once per print


def test_prints_of_one_round_go_as_one_message(db):
    with db.begin() as s:
        a = make_article(s, name="Bracket")
        b = make_article(s, name="Cover")
        finished(s, a)
        finished(s, b, units=3)
    out = send_round(db)
    assert len(out) == 1
    assert out[0].message.text == "2 prints wait for inspection: Bracket (1 pcs), Cover (3 pcs)"
    assert len(out[0].mark_ids) == 2


def test_without_names_only_kind_and_count(db):
    with db.begin() as s:
        st.set_setting(s, "ha_names", False, commit=False)
        a = make_article(s, name="Bracket")
        finished(s, a)
        finished(s, a)
    (out,) = send_round(db)
    assert out.message.text == "2 prints wait for inspection."
    assert "Bracket" not in out.message.title + out.message.text


def test_prints_before_switching_on_stay_quiet(db):
    with db.begin() as s:
        finished(s, make_article(s), at=SINCE.replace(tzinfo=None) - timedelta(minutes=1))
    assert send_round(db) == []


def test_prints_older_than_seven_days_stay_quiet(db):
    with db.begin() as s:
        st.set_setting(s, "ha_since", (SINCE - timedelta(days=30)).isoformat(), commit=False)
        finished(s, make_article(s), at=NOW - timedelta(days=8))
    assert send_round(db) == []


def test_without_a_switch_on_time_it_is_set_to_now_and_older_prints_stay_quiet(db):
    with db.begin() as s:
        st.set_setting(s, "ha_since", None, commit=False)
        finished(s, make_article(s))
    assert send_round(db) == []
    with db() as s:
        assert datetime.fromisoformat(st.get_setting(s, "ha_since")) == NOW.replace(tzinfo=UTC)


def test_a_print_that_could_not_be_reported_is_not_tried_again(db):
    with db.begin() as s:
        finished(s, make_article(s))
    assert kinds(send_round(db, outcome="unreachable")) == ["print_done"]
    assert send_round(db) == []  # Spec 10.6: events 1 and 2 are not caught up


def test_a_print_still_running_or_queued_is_no_event(db):
    with db.begin() as s:
        a = make_article(s)
        make_dispatch(s, a.id, status="printing")
        make_dispatch(s, a.id, status="queued")
    assert send_round(db) == []


# ---- 2 print failed -----------------------------------------------------------------------------


def test_a_failed_print_reports_once_and_links_the_article(db):
    with db.begin() as s:
        a = make_article(s, name="Bracket")
        finished(s, a, status="failed")
        article_id = a.id
    (out,) = send_round(db)
    assert out.kind == "print_failed"
    assert out.message.title == "Print failed"
    assert out.message.text == "1 print failed in Bambuddy, a replacement is queued: Bracket"
    assert out.message.link == f"{BASE}/articles/{article_id}"
    assert send_round(db) == []


def test_failures_of_two_articles_link_the_overview(db):
    with db.begin() as s:
        finished(s, make_article(s, name="A"), status="failed")
        finished(s, make_article(s, name="B"), status="failed")
    (out,) = send_round(db)
    assert out.message.link == f"{BASE}/"


# ---- 3 paused by the automation -----------------------------------------------------------------


def pause(s, article, reason):
    article.paused = True
    article.pause_reason = reason
    s.flush()


def test_an_automatic_pause_reports_once_with_its_reason(db):
    with db.begin() as s:
        pause(s, make_article(s, name="Bracket"), "two_failures")
    (out,) = send_round(db)
    assert out.kind == "paused"
    assert out.message.text == "The automation paused 1 article: Bracket (two failures in a row)"
    assert send_round(db) == []


def test_a_pause_the_user_caused_is_no_event(db):
    with db.begin() as s:
        pause(s, make_article(s), "queue_reduced")
    assert send_round(db) == []


def test_a_lifted_pause_that_comes_back_reports_again(db):
    with db.begin() as s:
        a = make_article(s)
        pause(s, a, "missing_in_bambuddy")
        article_id = a.id
    assert kinds(send_round(db)) == ["paused"]
    with db.begin() as s:
        a = s.get(type(a), article_id)
        a.paused, a.pause_reason = False, None
    assert send_round(db) == []
    with db.begin() as s:
        pause(s, s.get(type(a), article_id), "file_missing")
    assert kinds(send_round(db)) == ["paused"]


def test_a_state_that_could_not_be_reported_is_tried_again(db):
    with db.begin() as s:
        pause(s, make_article(s), "two_failures")
    assert kinds(send_round(db, outcome="unreachable")) == ["paused"]
    assert kinds(send_round(db, outcome="unclear")) == ["paused"]  # unclear counts as failed
    assert kinds(send_round(db)) == ["paused"]
    assert send_round(db) == []


def test_an_archived_article_drops_its_mark(db):
    with db.begin() as s:
        a = make_article(s)
        pause(s, a, "two_failures")
        a.archived_at = NOW
    assert send_round(db) == []


# ---- 4 below minimum ----------------------------------------------------------------------------


def test_below_minimum_without_reprint_reports(db):
    with db.begin() as s:
        a = make_article(s, name="Bracket", min_stock=2, stock=0)
        article_id = a.id
    (out,) = send_round(db)
    assert out.kind == "below_min"
    assert out.message.text == "1 article is below its minimum stock: Bracket"
    assert out.message.link == f"{BASE}/articles/{article_id}"


def test_low_with_a_reprint_on_its_way_is_no_event(db):
    with db.begin() as s:
        a = make_article(s, min_stock=2, stock=0)
        make_entry(s, a.id, prints=2)  # coverage reaches the minimum: "low" (yellow)
    assert send_round(db) == []


def test_overbooked_reports_and_says_so(db):
    with db.begin() as s:
        a = make_article(s, name="Bracket", min_stock=0, stock=1, reserved=3)
    (out,) = send_round(db)
    assert out.message.text == "1 article is below its minimum stock: Bracket (overbooked)"


def test_below_minimum_settles_when_stock_is_back(db):
    with db.begin() as s:
        a = make_article(s, min_stock=2, stock=0)
        article_id = a.id
    assert kinds(send_round(db)) == ["below_min"]
    with db.begin() as s:
        s.get(type(a), article_id).stock = 5
    assert send_round(db) == []
    with db.begin() as s:
        s.get(type(a), article_id).stock = 0
    assert kinds(send_round(db)) == ["below_min"]


# ---- 5 reorder suggestion -----------------------------------------------------------------------


def test_a_part_suggestion_reports_at_a_regular_query_only(db):
    with db.begin() as s:
        make_part(s, name="Insert M4", stock=0, min_stock=10)
    assert send_round(db, ctx(poll=False)) == []
    (out,) = send_round(db, ctx(poll=True))
    assert out.kind == "reorder"
    assert out.message.text == "1 new reorder suggestion: Insert M4"
    assert out.message.link == f"{BASE}/reorder"
    assert send_round(db, ctx(poll=True)) == []


def reorder_mark(s, ref, *, gone_since=None, first_seen_at=NOW - timedelta(days=20)):
    s.add(NotifyMark(kind="reorder", ref=ref, state="sent", first_seen_at=first_seen_at, gone_since=gone_since))


def test_a_suggestion_that_is_gone_settles_after_seven_days(db):
    with db.begin() as s:
        part = make_part(s, name="Insert M4", stock=0, min_stock=10)
        part_id = part.id
    send_round(db, ctx(poll=True))
    with db.begin() as s:
        s.get(type(part), part_id).stock = 100
    assert send_round(db, ctx(poll=True, now=NOW + timedelta(days=1))) == []
    with db.begin() as s:
        s.get(type(part), part_id).stock = 0
    # back within 7 days: not settled, no new message
    assert send_round(db, ctx(poll=True, now=NOW + timedelta(days=2))) == []
    with db.begin() as s:
        s.get(type(part), part_id).stock = 100
    send_round(db, ctx(poll=True, now=NOW + timedelta(days=3)))
    assert send_round(db, ctx(poll=True, now=NOW + timedelta(days=10))) == []  # 7 days gone: settled
    with db.begin() as s:
        s.get(type(part), part_id).stock = 0
    assert kinds(send_round(db, ctx(poll=True, now=NOW + timedelta(days=11)))) == ["reorder"]


def test_an_ordered_and_received_suggestion_settles_at_once(db):
    with db.begin() as s:
        part = make_part(s, name="Insert M4", stock=0, min_stock=10)
        part_id = part.id
    send_round(db, ctx(poll=True))
    with db.begin() as s:
        s.add(ReorderMark(item_type="part", item_id=part_id, qty=1, marked_at=NOW, received_at=None))
    assert send_round(db, ctx(poll=True, now=NOW + timedelta(days=9))) == []  # ordered: not settled
    with db.begin() as s:
        s.scalars(select(ReorderMark)).one().received_at = NOW + timedelta(days=10)
    # received, the stock did not reach the floor: a new suggestion, a new message
    assert kinds(send_round(db, ctx(poll=True, now=NOW + timedelta(days=10)))) == ["reorder"]


def test_an_order_taken_back_settles_nothing(db):
    """Spec 10.6 (p280): a mark taken back is closed, but nothing arrived; the suggestion that comes
    back is the same occasion and sends no new message."""
    with db.begin() as s:
        part = make_part(s, name="Insert M4", stock=0, min_stock=10)
        part_id = part.id
    send_round(db, ctx(poll=True))
    with db.begin() as s:
        s.add(ReorderMark(item_type="part", item_id=part_id, qty=1, marked_at=NOW, received_at=None))
    assert send_round(db, ctx(poll=True, now=NOW + timedelta(days=1))) == []
    with db.begin() as s:
        taken_back = s.scalars(select(ReorderMark)).one()
        taken_back.received_at, taken_back.withdrawn = NOW + timedelta(days=2), True
    assert send_round(db, ctx(poll=True, now=NOW + timedelta(days=2))) == []  # back again: no message
    with db() as s:
        assert s.scalars(select(NotifyMark).where(NotifyMark.ref == f"part:{part_id}")).one().gone_since is None


def test_filament_with_unknown_stock_settles_nothing(db):
    with db.begin() as s:
        sort = make_sort(s)
        make_snapshot(s, NOW.date(), 1, sort.id, 500.0)  # a known stock once the spool list is read
        reorder_mark(s, "filament:5")
    send_round(db, ctx(poll=True, filament_known=False, now=NOW + timedelta(days=30)))
    with db() as s:
        mark = s.scalars(select(NotifyMark).where(NotifyMark.ref == "filament:5")).one()
        assert mark.gone_since is None
    send_round(db, ctx(poll=True, filament_known=True))
    with db() as s:
        assert s.scalars(select(NotifyMark).where(NotifyMark.ref == "filament:5")).one().gone_since == NOW


def test_a_filament_suggestion_reports_with_its_sort(db):
    with db.begin() as s:
        sort = make_sort(s)
        assign_sort(s, sort)
        make_filament_settings(s, sort.id, safety_value=1000, safety_unit="grams")
        make_snapshot(s, NOW.date(), 1, sort.id, 100.0)
        label = sort.label
    (out,) = send_round(db, ctx(poll=True))
    assert out.message.text == f"1 new reorder suggestion: {label}"
    assert send_round(db, ctx(poll=True, filament_known=False)) == []


# ---- 6 trouble ----------------------------------------------------------------------------------


UNREACHABLE = EngineStatus(configured=True, reachable=False)


def test_a_trouble_reports_after_two_regular_queries_in_a_row(db):
    assert send_round(db, ctx(poll=True, status=UNREACHABLE)) == []
    assert send_round(db, ctx(poll=False, status=UNREACHABLE)) == []  # a short tick does not count
    (out,) = send_round(db, ctx(poll=True, status=UNREACHABLE))
    assert out.kind == "trouble"
    assert out.message.title == "Fault"
    assert out.message.text == "Bambuddy is not reachable."
    assert out.message.link == f"{BASE}/"
    assert send_round(db, ctx(poll=True, status=UNREACHABLE)) == []


def test_a_trouble_gone_at_one_query_starts_again(db):
    send_round(db, ctx(poll=True, status=UNREACHABLE))
    send_round(db, ctx(poll=True))  # reachable again: settled
    assert send_round(db, ctx(poll=True, status=UNREACHABLE)) == []
    assert kinds(send_round(db, ctx(poll=True, status=UNREACHABLE))) == ["trouble"]


def test_yellow_warnings_are_no_trouble(db):
    with db.begin() as s:
        st.set_setting(s, "queue_running", False, commit=False)  # "queue stopped" is yellow
    status = EngineStatus(configured=True, reachable=True, error="boom")  # tick error: yellow
    for _ in range(3):
        assert send_round(db, ctx(poll=True, status=status)) == []


# ---- 7 missing right ----------------------------------------------------------------------------


def test_a_missing_right_reports_once_and_names_it(db):
    missing = ev.Rights("missing", ("queue:create", "printers:read"))
    (out,) = send_round(db, ctx(rights=missing))
    assert out.kind == "rights"
    assert out.message.text == "Reprintbuddy's key in Bambuddy lacks: queue:create, printers:read"
    assert send_round(db, ctx(rights=missing)) == []
    assert send_round(db, ctx(rights=ev.Rights("unknown", ()))) == []  # not checked yet: unchanged
    assert send_round(db, ctx(rights=missing)) == []
    send_round(db, ctx(rights=ev.Rights("ok", ())))
    assert kinds(send_round(db, ctx(rights=missing))) == ["rights"]


# ---- tick boxes, language, link, order ---------------------------------------------------------


def test_an_unticked_event_sends_nothing_and_old_prints_stay_quiet_after_ticking(db):
    with db.begin() as s:
        st.set_setting(s, "ha_events", ["paused"], commit=False)
        a = make_article(s, min_stock=2)
        finished(s, a)
    assert send_round(db) == []
    with db.begin() as s:
        st.set_setting(s, "ha_events", list(st.HA_EVENTS), commit=False)
    assert kinds(send_round(db)) == ["below_min"]  # the state reports, the old print does not


def test_german_texts(db):
    with db.begin() as s:
        st.set_setting(s, "language", "de", commit=False)
        finished(s, make_article(s, name="Halter"), units=2)
        pause(s, make_article(s, name="Deckel"), "two_failures")
    out = send_round(db)
    assert [o.message.title for o in out] == ["Bereit zur Prüfung", "Artikel pausiert"]
    assert out[0].message.text == "1 Druck wartet auf Prüfung: Halter (2 Stk.)"
    assert out[1].message.text == "Die Automatik hat 1 Artikel pausiert: Deckel (zwei Fehlschläge hintereinander)"


def test_without_an_address_there_is_no_link(db):
    with db.begin() as s:
        finished(s, make_article(s))
    (out,) = send_round(db, ctx(base=None))
    assert out.message.link is None


def test_kinds_come_in_the_order_of_the_spec(db):
    with db.begin() as s:
        a = make_article(s, min_stock=2)
        pause(s, a, "two_failures")
        finished(s, a, status="failed")
        finished(s, a)
    assert kinds(send_round(db)) == ["print_done", "print_failed", "paused", "below_min"]


def test_excluded_marks_are_left_out(db):
    with db.begin() as s:
        finished(s, make_article(s))
    first = round_(db)
    with db.begin() as s:
        assert ev.due(s, ctx(), exclude=frozenset(first[0].mark_ids)) == []


def test_marks_of_old_prints_are_removed_after_thirty_days(db):
    with db.begin() as s:
        finished(s, make_article(s))
    send_round(db)
    send_round(db, ctx(now=NOW + timedelta(days=31)))
    with db() as s:
        assert s.scalars(select(NotifyMark)).all() == []


@pytest.mark.parametrize("lang", ["en", "de"])
def test_every_pause_reason_and_red_warning_has_a_short_text(lang):
    from reprintbuddy.domain.pauses import PAUSE_REASONS
    from reprintbuddy.i18n import load_locales
    from reprintbuddy.web.engine_view import BAD_WARNINGS

    texts = load_locales()[lang]
    for reason in PAUSE_REASONS:
        assert f"notify.reason.{reason}" in texts
    for key in BAD_WARNINGS:
        assert f"notify.trouble.{key.removeprefix('warning.')}" in texts


def test_red_articles_agree_with_the_overview(db):
    """`red_articles` reads only a few columns; its states must be the overview's (Spec 10.1)."""
    from reprintbuddy.domain import catalog

    with db.begin() as s:
        cases = [
            dict(min_stock=2, stock=0),  # below minimum
            dict(min_stock=0, stock=1, reserved=3),  # overbooked
            dict(min_stock=2, stock=0, units_per_print=2),  # low: one queued print brings 2
            dict(min_stock=3, stock=1, provisional=1, awaiting_assembly=1),  # low by what waits
            dict(min_stock=4, stock=1),  # below, one print in Bambuddy is not enough
            dict(min_stock=1, stock=5),  # ok
        ]
        made = [make_article(s, name=f"Case {i}", **fields) for i, fields in enumerate(cases)]
        make_entry(s, made[2].id, prints=1)
        make_dispatch(s, made[4].id, status="printing", units=1)
        archived = make_article(s, name="Archived", min_stock=5)
        archived.archived_at = NOW
    with db() as s:
        expected = {r.id: r.status for r in catalog.list_articles(s) if r.status in ("below_min", "overbooked")}
        assert ev.red_articles(s) == expected
        assert set(expected) == {made[0].id, made[1].id, made[4].id}
