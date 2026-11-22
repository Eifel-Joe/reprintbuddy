"""Measures whether the number fields of the interface show their largest value in full (p781).

A number field that is too narrow cuts the last digit off: the value is stored right, but the field
shows "50" for 500 (the digit lies behind the spin arrows). The script starts the application the way
`docs_screenshots.py` does (same made-up scenario, in-memory Bambuddy, local Chromium), visits the pages
that hold number fields at a window width of 1024 px and of 375 px, writes the largest value a field
allows into each (its `max`; a field without one gets 9999) and compares `scrollWidth` with `clientWidth`.
A field where the first is bigger cuts its text. The fields are measured twice, once at rest and once
focused (a browser shows the spin arrows then and takes room from the text).

Call (from the project folder, on Windows `.venv/Scripts/python.exe`)::

    .venv/bin/python tools/measure_number_fields.py [--json FILE]

Exit code 1 when a field cuts its text. Needs the same things as `docs_screenshots.py`.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("docs_screenshots", ROOT / "tools" / "docs_screenshots.py")
shots = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = shots  # the dataclasses of the script look themselves up there
spec.loader.exec_module(shots)

WIDTHS = (1024, 375)
UNBOUNDED_VALUE = "9999"

MEASURE = """
(()=>{
  const out = [];
  document.querySelectorAll('details').forEach(d => d.open = true);
  const fields = document.querySelectorAll('input[type=number], input[inputmode=numeric]');
  fields.forEach(el => {
    if (el.type === 'hidden' || el.type === 'checkbox') return;
    const max = el.getAttribute('max');
    const value = max !== null && max !== '' ? max : %(unbounded)s;
    el.value = value;
    const rest = {c: el.clientWidth, s: el.scrollWidth};
    el.focus();
    const focus = {c: el.clientWidth, s: el.scrollWidth};
    el.blur();
    out.push({id: el.id || el.name || el.className, name: el.name, value, rest, focus,
              visible: !!(el.offsetWidth || el.offsetHeight)});
  });
  return out;
})()
"""


def pages(scn, settings_only: bool = False) -> list[tuple[str, str]]:
    settings = [
        ("settings-general", "/settings?tab=general"),
        ("settings-automation", "/settings?tab=automation"),
        ("settings-labels", "/settings?tab=labels"),
        ("settings-notify", "/settings?tab=notify"),
    ]
    if settings_only:
        return settings
    a = scn.articles
    return settings + [
        ("queue", "/queue"),
        ("inspection", "/inspection"),
        ("assembly", "/assembly"),
        ("reorder", "/reorder"),
        ("bookings", "/bookings"),
        ("articles-list", "/articles"),
        ("article-detail", f"/articles/{a['cable_guide']}"),
        ("assembled-detail", f"/articles/{a['hose']}"),
        ("assembled-form", f"/articles/{a['hose']}/assembled"),
        ("set-form", f"/sets/{scn.sets['Bracket kit']}/edit"),
        ("filament-settings", f"/filament/{scn.sorts['petg_black']}"),
    ]


def measure_all(lang: str, settings_only: bool = False) -> list[dict]:
    work = Path(tempfile.mkdtemp(prefix="reprintbuddy-measure-"))
    shots.LANGUAGE["now"] = lang
    shots.CLOCK.set(shots.at(2, 8))
    server = shots.Server(work, lang)
    scn = shots.Scenario()
    shots.fill_bambuddy(server.fake, scn)
    server.start()
    browser = shots.Browser(work, lang)
    rows: list[dict] = []
    try:
        shot = shots.Shooter(browser, server, work / "pictures", lang, {"none"})  # wants no picture
        shots.setup_flow(shot, scn)
        if not settings_only:  # the pages of the settings need no articles, orders or prints
            shots.build_catalog(server, scn)
            shots.build_history(server, scn)
            shots.run_prints(server, scn)
            shots.prepare_settings(server)
            shots.CLOCK.set(shots.at(9, 11, 30))
            server.app.state.printer_board.at = shots.CLOCK()
        for width in WIDTHS:
            browser.viewport(width, 900, mobile=width < 768)
            for name, path in pages(scn, settings_only):
                browser.goto(server.url + path, settle=0.3)
                for field in browser.evaluate(MEASURE % {"unbounded": json.dumps(UNBOUNDED_VALUE)}):
                    field.update(page=name, width=width, lang=lang)
                    rows.append(field)
    finally:
        browser.close()
        server.stop()
        shutil.rmtree(work, ignore_errors=True)
    return rows


def cut_fields(rows: list[dict]) -> list[dict]:
    return [r for r in rows if cut(r)]


def cut(row: dict) -> bool:
    return row["visible"] and any(row[state]["s"] > row[state]["c"] for state in ("rest", "focus"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--json", type=Path, help="write all measurements here")
    parser.add_argument("--lang", choices=("en", "de"), default="en")
    parser.add_argument("--settings-only", action="store_true", help="only the pages of the settings (quick)")
    args = parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True)
    shots.freeze_application_clock()
    rows = measure_all(args.lang, args.settings_only)
    if args.json:
        args.json.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    for row in rows:
        mark = "CUT " if cut(row) else "ok  "
        if not row["visible"]:
            mark = "hid "
        print(f"{mark}{row['page']:<18} {row['width']:>4}px {row['id']:<22} value {row['value']:>5}  "
              f"rest {row['rest']['s']}/{row['rest']['c']}  focus {row['focus']['s']}/{row['focus']['c']}")
    bad = [r for r in rows if cut(r)]
    print(f"{len(rows)} fields, {len(bad)} cut their text")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
