"""The column "Verbrauch" of the filament page can be switched (Spec 9.3, p717).

Every view is in the page; only the choice is shown. Without JavaScript Ø 30 days applies and no switch is
offered; `filament_usage.js` shows the switch and keeps the choice in the browser (localStorage). The head
names the choice; an average says over how many days while the record is shorter than its window.
"""

import re
from datetime import timedelta
from pathlib import Path

from conftest import sm_of
from factories import assign_sort, make_snapshot, make_sort
from texts import de, en, en_html
from reprintbuddy import settings as st
from reprintbuddy.domain import filament
from reprintbuddy.models import utcnow

JS = Path(__file__).resolve().parents[2] / "src" / "reprintbuddy" / "web" / "static" / "js" / "filament_usage.js"


def _sort_with_record(client, days: int, per_day: float, **fields) -> int:
    today = utcnow().date()
    with sm_of(client).begin() as s:
        sort = make_sort(s, **fields)
        assign_sort(s, sort, name=f"Uses {sort.id}")
        for back in range(days + 1):
            make_snapshot(s, today - timedelta(days=back), sort.id * 100, sort.id, 5000.0 - per_day * (days - back))
        return sort.id


def _row(html: str, sort_id: int) -> str:
    (row,) = [r for r in re.findall(r"<tr>(.*?)</tr>", html, re.S) if f'href="/filament/{sort_id}"' in r]
    return row


def _shown(fragment: str, attribute: str) -> dict[str, bool]:
    """view -> shown (no `hidden`) for the spans carrying `attribute`."""
    return {
        view: "hidden" not in rest
        for view, rest in re.findall(rf'<span {attribute}="(\w+)"([^>]*)>', fragment)
    }


def _cell(row: str, view: str) -> str:
    """The text of one view in the row's usage cell, tags removed."""
    inner = re.search(rf'<span data-usage="{view}"[^>]*>(.*?)(?=<span data-usage="|</td>)', row, re.S).group(1)
    return " ".join(re.sub(r"<[^>]+>", " ", inner).split())


def test_without_javascript_the_default_is_shown_and_no_switch(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA")
    sort = _sort_with_record(bb_client, 40, 10.0)
    page = bb_client.get("/filament").text
    row = _row(page, sort)
    assert _shown(row, "data-usage") == {view: view == "avg30" for view in filament.USAGE_VIEWS}
    head = re.search(r"<thead>(.*?)</thead>", page, re.S).group(1)
    assert _shown(head, "data-usage-head") == {view: view == "avg30" for view in filament.USAGE_VIEWS}
    # the switch is there for the script, hidden without it
    assert re.search(r'<p class="usage-choice" data-usage-choice hidden>', page)
    assert 'src="/static/js/filament_usage.js' in page


def test_every_view_is_in_the_row(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA")
    sort = _sort_with_record(bb_client, 400, 10.0)
    row = _row(bb_client.get("/filament").text, sort)
    assert {view: _cell(row, view) for view in filament.USAGE_VIEWS} == {
        "avg30": "10.0 g", "avg7": "10.0 g", "today": "10 g", "yesterday": "10 g",
        "week": "70 g", "month": "300 g", "year": "3650 g", "total": "4000 g",  # whole grams like the stock column
    }


def test_the_head_names_the_choice(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA")
    _sort_with_record(bb_client, 40, 10.0)
    page = bb_client.get("/filament").text
    head = re.search(r"<thead>(.*?)</thead>", page, re.S).group(1)
    for view in filament.USAGE_VIEWS:
        title = en("filament.col.per_day" if view in filament.USAGE_AVERAGES else "filament.usage.head_sum")
        part = re.search(rf'<span data-usage-head="{view}"[^>]*>(.*?)</span></span>', head, re.S).group(1)
        assert title in part and en_html(f"filament.usage.view.{view}") in part
    assert ", so far" not in head  # 40 days of record: longer than every window but the year


def test_an_average_says_its_days_while_the_record_is_short(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA")
    _sort_with_record(bb_client, 6, 12.0)
    page = bb_client.get("/filament").text
    head = re.search(r"<thead>(.*?)</thead>", page, re.S).group(1)
    assert en_html("filament.usage.so_far", view=en("filament.usage.view.avg30"), days=6) in head
    assert en_html("filament.usage.so_far", view=en("filament.usage.view.avg7"), days=6) in head
    assert "Ø 30 days, so far 6" in head


def test_the_average_days_end_with_a_full_window(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA")
    _sort_with_record(bb_client, 9, 12.0)
    head = re.search(r"<thead>(.*?)</thead>", bb_client.get("/filament").text, re.S).group(1)
    assert "Ø 30 days, so far 9" in head
    assert en_html("filament.usage.so_far", view=en("filament.usage.view.avg7"), days=7) not in head


def test_a_sort_with_a_shorter_record_says_its_days_in_the_row(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA")
    fake_bambuddy.add_spool("PETG", color_hex="#FFFFFF")
    long = _sort_with_record(bb_client, 20, 10.0)
    short = _sort_with_record(bb_client, 3, 10.0, material="PETG", color_hex="#FFFFFF", color_name="White")
    page = bb_client.get("/filament").text
    assert "Ø 30 days, so far 20" in page
    assert en("filament.usage.cell_so_far", days=3) in _cell(_row(page, short), "avg30")
    assert "so far" not in _cell(_row(page, long), "avg30")


def test_a_view_without_record_says_so(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA")
    with sm_of(bb_client).begin() as s:
        sort = make_sort(s)
        assign_sort(s, sort)
        make_snapshot(s, utcnow().date(), 1, sort.id, 800.0)
    row = _row(bb_client.get("/filament").text, sort.id)
    for view in filament.USAGE_VIEWS:
        assert en("filament.no_history") in _cell(row, view)


def test_german_texts(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA")
    _sort_with_record(bb_client, 6, 12.0)
    with sm_of(bb_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    page = bb_client.get("/filament").text
    assert "Ø 30 Tage, bisher 6" in page
    for view in ("today", "yesterday", "week", "month", "year", "total"):
        assert de(f"filament.usage.view.{view}") in page
    assert de("filament.usage.choose") in page


def test_the_options_follow_the_views(bb_client, fake_bambuddy):
    fake_bambuddy.add_spool("PLA")
    _sort_with_record(bb_client, 6, 12.0)
    page = bb_client.get("/filament").text
    options = re.findall(r'<option value="(\w+)"( selected)?>', page)
    assert [value for value, _ in options] == list(filament.USAGE_VIEWS)
    assert [value for value, chosen in options if chosen] == ["avg30"]


def test_the_script_keeps_the_choice_in_the_browser_and_survives_blocked_storage():
    script = JS.read_text(encoding="utf-8")
    assert "localStorage" in script
    # every use of the storage sits in a try block: blocked storage must not stop the switch
    for line_no, line in enumerate(script.splitlines()):
        if "localStorage" in line:
            before = "\n".join(script.splitlines()[max(0, line_no - 3): line_no])
            assert "try" in before, line
    assert "rb_usage_view" in script
