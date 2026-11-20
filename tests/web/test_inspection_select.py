"""The choice in "To check" is a tick box, not a switch (v0.3.8 p117).

A switch means "setting on/off"; here a row is only picked for "Selected all good". So the row
boxes are plain tick boxes, a script adds a box in the table head ("select all", with a half state) and
keeps "Selected all good" off while nothing is picked. Without the script the page works as before:
no head box, the button is on, the server says "nothing is selected".
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

import reprintbuddy.web
from test_inspection_page import PAGE, finished

STATIC = Path(reprintbuddy.web.__file__).parent / "static"
CSS = (STATIC / "css" / "reprintbuddy.css").read_text(encoding="utf-8")


def test_row_boxes_are_not_in_a_switch_label(ready_client):
    finished(ready_client)
    page = ready_client.get(PAGE).text
    label = re.search(r'<label class="([^"]*inspect-select[^"]*)">', page).group(1)
    assert "check" not in label.split()  # `label.check` draws a switch
    # one box per article row, carrying all its prints (p713)
    assert re.search(r'<input type="checkbox" name="group" value="\d+" form="inspect-selected" aria-label="Select the prints of Template A">', page)


def test_the_script_counts_the_article_rows():
    source = (STATIC / "js" / "inspection.js").read_text(encoding="utf-8")
    assert '[name="group"][form="inspect-selected"]' in source


def test_the_head_has_a_place_for_the_script_and_is_not_hidden_from_readers(ready_client):
    finished(ready_client)
    page = ready_client.get(PAGE).text
    head = re.search(r'<div class="line-head[^"]*"([^>]*)>(.*?)</div>', page, re.S)
    assert "aria-hidden" not in head.group(1)  # a box inside could not be reached otherwise
    assert '<span class="inspect-head-select" data-label="Select all"></span>' in head.group(2)
    assert "<input" not in head.group(2)  # the box comes from the script, never from the server


def test_without_the_script_the_button_is_on_and_the_script_is_loaded_by_the_page(ready_client):
    finished(ready_client)
    page = ready_client.get(PAGE).text
    button = re.search(r'<form id="inspect-selected".*?<button([^>]*)>', page, re.S).group(1)
    assert "disabled" not in button
    assert '<script src="/static/js/inspection.js' in page
    assert ready_client.get("/static/js/inspection.js").status_code == 200


def test_the_text_of_the_head_box_is_translated(ready_client):
    from conftest import sm_of
    from reprintbuddy import settings as st

    finished(ready_client)
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
    assert 'data-label="Alle auswählen"' in ready_client.get(PAGE).text


def test_the_stylesheet_keeps_the_native_tick_box_for_the_choice():
    # no rule turns a box in .inspect-select into a switch
    for match in re.finditer(r"([^{}]*inspect-[^{}]*)\{([^}]*)\}", re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)):
        assert "appearance: none" not in match.group(2), match.group(1)
    assert re.search(r"\.inspect-select input\[type=\"checkbox\"\][^{]*\{[^}]*width: 1\.25rem", CSS)
    assert ".inspect-all" in CSS
    assert re.search(r"\.inspect-toolbar \.btn:disabled \{[^}]*opacity", CSS)  # a button that is off looks off


NODE_CASES = [
    # (rows, ticked) -> [head ticked, head half, button off]
    ((0, 0), [False, False, True]),
    ((3, 0), [False, False, True]),
    ((3, 1), [False, True, False]),
    ((3, 2), [False, True, False]),
    ((3, 3), [True, False, False]),
    ((1, 1), [True, False, False]),
]


def test_script_decides_head_box_and_button_from_the_count():
    try:
        done = subprocess.run(
            [
                "node",
                "-e",
                "const s=require(process.argv[1]).state;"
                "console.log(JSON.stringify(JSON.parse(process.argv[2]).map(a=>{const r=s(a[0],a[1]);return [r.checked,r.half,r.none]})))",
                str(STATIC / "js" / "inspection.js"),
                json.dumps([list(args) for args, _ in NODE_CASES]),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        pytest.skip("node is not installed")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [expected for _, expected in NODE_CASES]


def test_script_holds_the_list_while_the_head_box_has_the_focus_and_lines_the_box_up():
    source = (STATIC / "js" / "inspection.js").read_text(encoding="utf-8")
    assert 'setAttribute("data-live-busy"' in source and 'new CustomEvent("live-idle")' in source
    assert 'holder.className = "inspect-all-box"' in source
    assert re.search(r"\.inspect-all-box \{[^}]*width: 2\.75rem", CSS)  # the same column width as the row boxes


# ---- unfolded rows stay open (p713 F8) -------------------------------------------------------


def node_eval(script, *args):
    try:
        done = subprocess.run(
            ["node", "-e", script, str(STATIC / "js" / "inspection.js"), *args],
            capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        pytest.skip("node is not installed")
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_script_remembers_the_open_rows():
    result = node_eval(
        "const m=require(process.argv[1]);"
        "let s=null;"
        "s=m.remember(s,'7',true); s=m.remember(s,'9',true); s=m.remember(s,'7',true);"
        "const both=m.openIds(s);"
        "s=m.remember(s,'7',false);"
        "console.log(JSON.stringify([both,m.openIds(s),m.openIds(null),m.openIds('{broken'),m.openIds('{\"a\":1}'),m.openIds('[1,\"x\",null]')]))"
    )
    assert result == [["9", "7"], ["9"], [], [], [], ["x"]]  # opened again: the newest, last


def test_script_keeps_at_most_fifty_rows():
    result = node_eval(
        "const m=require(process.argv[1]);let s=null;"
        "for(let i=0;i<60;i++){s=m.remember(s,String(i),true);}"
        "const ids=m.openIds(s);console.log(JSON.stringify([ids.length,ids[0],ids[49]]))"
    )
    assert result == [50, "10", "59"]


def test_script_reads_and_writes_the_tab_store_only_inside_try():
    source = (STATIC / "js" / "inspection.js").read_text(encoding="utf-8")
    uses = [m.start() for m in re.finditer(r"sessionStorage", source)]
    assert uses, "the open rows are kept in the tab's store"
    for at in uses:
        before = source[:at]
        assert before.rfind("try {") > before.rfind("}"), "every use of sessionStorage sits in a try block"
    assert '"toggle"' in source and "details.inspect-prints" in source
