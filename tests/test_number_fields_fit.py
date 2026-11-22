"""p781: a number field of the settings shows its largest value in full.

"Reserve per sort" showed 500 as "50": the box of the field (`.setting-control`) shrank next to a long
hint and took the field's width with it, so the last digit lay behind the spin arrows. The measurement
runs in a real Chromium browser (Edge or Chrome, as the tool for the pictures of the documentation) at
1024 px and 375 px; without such a browser the test is skipped. The same tool measures the whole
interface: `tools/measure_number_fields.py`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def tool(monkeypatch):
    from reprintbuddy.web import app as web_app
    from reprintbuddy.web.routes import ops as ops_routes

    monkeypatch.setattr(web_app, "__version__", web_app.__version__)  # the tool pins a stand-in version
    monkeypatch.setattr(ops_routes, "__version__", ops_routes.__version__)
    spec = importlib.util.spec_from_file_location("measure_number_fields_under_test", ROOT / "tools" / "measure_number_fields.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setitem(sys.modules, "docs_screenshots", module.shots)
    try:
        module.shots.find_browser()
    except SystemExit:
        pytest.skip("no Chromium browser (Edge or Chrome) on this machine")
    module.shots.freeze_application_clock()
    return module


def test_no_number_field_of_the_settings_cuts_its_largest_value(tool):
    rows = tool.measure_all("en", settings_only=True)
    names = {r["id"] for r in rows if r["visible"]}
    assert {"plan-reserve-grams", "queue-cap", "poll-minutes"} <= names  # the three fields of the finding were measured
    bad = [(r["page"], r["width"], r["id"], r["value"], r["rest"], r["focus"]) for r in tool.cut_fields(rows)]
    assert bad == []
