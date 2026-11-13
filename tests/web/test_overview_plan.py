"""The print plan on the overview (Spec 10.2, 0.4.2), with the Bambuddy stand-in and a real regular query.

Printers of the stand-in (conftest): Printer 1 and 2 (A1), Printer 3 (P1S).
"""

import re
import time
from pathlib import Path

from markupsafe import escape

from conftest import run_tick, sm_of
from factories import make_article, make_entry
from texts import en_html as en
from reprintbuddy import settings as st
from reprintbuddy.i18n import translate_count
from reprintbuddy.models import utcnow
from reprintbuddy.web import printplan_view, regions

CSS = Path(__file__).resolve().parents[2] / "src/reprintbuddy/web/static/css/reprintbuddy.css"


def plan_part(client):
    found = re.search(r"<!--live:plan-->(.*?)<!--/live:plan-->", client.get("/").text, re.S)
    assert found, "no region plan"
    return found.group(1)


def bars(part):
    """[(classes, text)] of the bars, lane after lane."""
    return [(cls, re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", inner)).strip())
            for cls, inner in re.findall(r'<li class="(plan-bar[^"]*)"[^>]*>(.*?)</li>', part, re.S)]


def lanes(part):
    return re.findall(r'<span class="plan-name">([^<]*)', part)


def own_user(client, fake):
    with sm_of(client).begin() as s:
        st.set_setting(s, "bambuddy_username", fake.username, commit=False)


def query(client):
    run_tick(client.app.state.ticker, poll=True)


def test_three_kinds_of_bars(bb_client, fake_bambuddy):
    fake = fake_bambuddy
    own_user(bb_client, fake)
    fake.set_printer_status(1, state="RUNNING", progress=63.0, remaining_min=70, subtask_name="Clip")
    fake.add_foreign_item("Lid.gcode.3mf", printer_id=2, print_time_s=1800)
    fake.add_foreign_item("Clip.gcode.3mf", created_by=fake.username, target_model="A1", print_time_s=3000)
    fake.add_foreign_item("Spare part.gcode.3mf", target_model="A1", print_time_s=2400)
    query(bb_client)
    part = plan_part(bb_client)
    assert lanes(part)[:3] == ["Printer 1", "Printer 2", "Printer 3"]
    found = bars(part)
    kinds = [cls.split()[1] for cls, _ in found]
    assert kinds.count("plan-running") == 1 and kinds.count("plan-fixed") == 1 and kinds.count("plan-planned") == 2
    running = next(t for c, t in found if "plan-running" in c)
    assert "63 % · 1 h 10 left" in running
    assert "plan-foreign" in next(c for c, t in found if "Spare part" in t)
    assert "plan-foreign" not in next(c for c, t in found if "plan-planned" in c and "Clip" in t)
    for cls, inner in found:  # never colour alone: the kind in words in every bar
        assert any(en(key) in inner for key in
                   ("overview.plan.kind_running", "overview.plan.kind_fixed", "overview.plan.kind_planned"))
    assert en("overview.plan.foreign") in part


# The end of the plan is a clock time, after midnight with the short weekday in front ("Sat 00:15",
# Spec 10.2). The tests run at the real time of day in Berlin time, so late in the evening the end falls on
# the next day: they failed every evening from about 23:15 (seen on the CI of v0.4.11).
FINISH_TIME = r"(?:[A-Za-z]+ )?\d\d:\d\d"


def test_finish_line(bb_client, fake_bambuddy):
    fake_bambuddy.add_foreign_item("Spare part.gcode.3mf", target_model="A1", print_time_s=2400)
    query(bb_client)
    part = plan_part(bb_client)
    assert re.search(re.escape(en("overview.plan.finish", time="@")).replace("@", FINISH_TIME), part)


def test_unknown_duration_says_at_least(bb_client, fake_bambuddy):
    fake_bambuddy.add_foreign_item("Spare part.gcode.3mf", target_model="A1", print_time_s=None)
    query(bb_client)
    part = plan_part(bb_client)
    assert re.search(re.escape(en("overview.plan.finish_minimum", time="@")).replace("@", FINISH_TIME), part)
    assert en("overview.plan.unknown_duration") in part


def test_reprintbuddys_own_queue_only_counted(bb_client):
    with sm_of(bb_client).begin() as s:
        a = make_article(s, name="Bracket")
        make_entry(s, a.id, prints=3, held=True)
    query(bb_client)
    part = plan_part(bb_client)
    assert str(escape(translate_count("en", "overview.plan.waiting_here", 3))) in part
    assert "Bracket" not in part


def test_manual_and_unplaced_are_counted(bb_client, fake_bambuddy):
    item = fake_bambuddy.add_foreign_item("Later.gcode.3mf", target_model="A1")
    fake_bambuddy.set_manual_start(item.id, True)
    fake_bambuddy.add_foreign_item("Other model.gcode.3mf", target_model="H2D")
    query(bb_client)
    part = plan_part(bb_client)
    assert str(escape(translate_count("en", "overview.plan.manual", 1))) in part
    assert str(escape(translate_count("en", "overview.plan.unplaced", 1))) in part


def test_out_of_service_lane(bb_client, fake_bambuddy):
    fake_bambuddy.set_printer_status(3, connected=False)
    fake_bambuddy.add_foreign_item("Guide.gcode.3mf", printer_id=3)
    fake_bambuddy.add_foreign_item("Spare part.gcode.3mf", target_model="A1")
    query(bb_client)
    part = plan_part(bb_client)
    assert en("overview.plan.condition_offline") in part
    assert en("overview.plan.waiting", items="Guide") in part
    assert en("overview.plan.without", names="Printer 3") in part


def test_unknown_printer_state(ready_client):
    part = plan_part(ready_client)
    assert en("overview.plan.unknown") in part
    assert "plan-lane" not in part


def test_no_prints_planned(bb_client):
    query(bb_client)
    part = plan_part(bb_client)
    assert en("overview.plan.none") in part
    assert lanes(part) == ["Printer 1", "Printer 2", "Printer 3"]


def test_plan_leads_to_the_queue(bb_client):
    query(bb_client)
    assert re.search(r'<a class="plan-main" href="/queue">', plan_part(bb_client))


def test_plan_region_does_not_ask_bambuddy(bb_client, fake_bambuddy):
    query(bb_client)
    fake_bambuddy.calls.clear()
    response = bb_client.get("/", headers={regions.HEADER: "plan"})
    assert response.status_code == 200 and "plan-lane" in response.text
    assert fake_bambuddy.calls == []


def test_long_queue_renders(bb_client, fake_bambuddy):
    for n in range(60):
        fake_bambuddy.add_foreign_item(f"Part {n}.gcode.3mf", target_model="A1", print_time_s=5400)
    query(bb_client)
    part = plan_part(bb_client)
    assert len(bars(part)) == 60
    assert "→" in part  # the bars beyond 24 hours end at the edge


def best_plan_time(client, runs: int = 7) -> float:
    """The shortest of `runs` builds of the plan view from the last query, in seconds.

    Only the plan itself is timed (not the login, the middleware or the rest of the page, which a loaded
    runner can stretch by a factor of twenty), and the shortest run counts: a stall of the machine lengthens
    one run, a slow plan lengthens all of them (p536)."""
    ticker = client.app.state.ticker
    shortest = float("inf")
    for _ in range(runs):
        with sm_of(client)() as db:
            started = time.perf_counter()
            printplan_view.plan_view(db, ticker.board, ticker.status.bambuddy_queue, utcnow(), "en", known=True)
            shortest = min(shortest, time.perf_counter() - started)
    return shortest


def test_long_queue_is_planned_quickly(bb_client, fake_bambuddy):
    """Two things a slow plan would show, neither of which depends on the speed of the machine much:
    the time grows no faster than the queue does (a quadratic loop, a query per print: four times the
    prints must not cost sixteen times the time), and 60 prints stay far below the 5 ms of a normal run
    (a bound with a wide margin, for a plan that became slow for every queue)."""
    added = 0

    def queue_of(count):
        nonlocal added
        while added < count:
            fake_bambuddy.add_foreign_item(f"Part {added}.gcode.3mf", target_model="A1", print_time_s=5400)
            added += 1
        query(bb_client)
        return best_plan_time(bb_client)

    sixty = queue_of(60)
    assert sixty < 0.15
    assert queue_of(240) < 8 * sixty  # linear is about 2, quadratic about 16


def test_mobile_css_turns_lanes_into_a_list():
    css = CSS.read_text(encoding="utf-8")
    blocks = re.findall(r"@media \(max-width: 699px\) \{(.*?)\n\}", css, re.S)
    mobile = next((block for block in blocks if ".plan-track" in block), None)
    assert mobile, "no phone rules for the print plan"
    assert re.search(r"\.plan-track \{[^}]*display: block", mobile)
    # relative, not static: the progress fill of a running print stays inside its own row (review v0.4.2, 5)
    assert re.search(r"\.plan-bar \{[^}]*position: relative", mobile)
    assert re.search(r"\.plan-beyond \{[^}]*display: flex", mobile)  # the list shows every print
    assert re.search(r"\.ov-tiles \{[^}]*grid-template-columns: minmax\(0, 1fr\)", mobile)
    # on a wide screen the bar sits on the axis
    assert re.search(r"\.plan-bar \{[^}]*position: absolute;[^}]*left: var\(--start\); width: var\(--width\)", css)


def test_tile_bars_are_hidden_from_screen_readers(bb_client):
    with sm_of(bb_client).begin() as s:
        make_article(s, name="Bracket", stock=8, min_stock=4)
    page = bb_client.get("/").text
    for bar in re.findall(r'<div class="ov-bar"[^>]*>', page):
        assert 'aria-hidden="true"' in bar


def test_refused_queue_read_makes_the_plan_unknown(bb_client, fake_bambuddy):
    """Without a current read of Bambuddy's queue the plan would show an old or empty queue as current
    (review v0.4.2, 3): it says the state is unknown instead."""
    from reprintbuddy.bambuddy.client import BambuddyRejected

    fake_bambuddy.add_foreign_item("Spare part.gcode.3mf", target_model="A1")
    query(bb_client)
    assert "plan-lane" in plan_part(bb_client)
    fake_bambuddy.fail_next("list_queue", BambuddyRejected("Forbidden", status=403))
    query(bb_client)
    part = plan_part(bb_client)
    assert en("overview.plan.unknown") in part and "plan-lane" not in part


def test_the_whole_plan_leads_to_the_queue_css():
    """The stretched link of the plan lies above the lanes and the axis, which have no links of their own
    (Spec 10.2: the whole plan leads to the queue; review v0.4.2, 4)."""
    css = CSS.read_text(encoding="utf-8")
    assert re.search(r"\.plan-main::after \{[^}]*z-index: 1", css)


def test_bars_beyond_the_axis_are_left_out_on_a_wide_screen(bb_client, fake_bambuddy):
    """A print that starts after the end of the axis (24 hours) has no place on it: hidden on a wide screen,
    listed on a phone; every bar carries its whole text as a tooltip (review v0.4.2, 8)."""
    for n in range(40):
        fake_bambuddy.add_foreign_item(f"Part {n}.gcode.3mf", target_model="A1", print_time_s=5400)
    query(bb_client)
    part = plan_part(bb_client)
    tags = re.findall(r'<li class="(plan-bar[^"]*)"[^>]*title="([^"]+)"', part)
    assert len(tags) == 40
    beyond = [cls for cls, _title in tags if "plan-beyond" in cls]
    assert beyond and len(beyond) < 40
    css = CSS.read_text(encoding="utf-8")
    assert re.search(r"\.plan-beyond \{ display: none; \}", css)


def test_no_bars_still_names_the_printers_out_of_service(bb_client, fake_bambuddy):
    """Only an entry waiting for a printer that is offline: no bar, but the head says which printer
    (review v0.4.2, 11)."""
    fake_bambuddy.set_printer_status(3, connected=False)
    fake_bambuddy.add_foreign_item("Guide.gcode.3mf", printer_id=3)
    query(bb_client)
    part = plan_part(bb_client)
    assert en("overview.plan.none") in part
    assert en("overview.plan.without", names="Printer 3") in part
