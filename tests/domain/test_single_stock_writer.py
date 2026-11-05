"""Spec 13: stock numbers change in one place in the code, `domain/stock.py`."""

import re
from pathlib import Path

import reprintbuddy

ASSIGN = re.compile(r"\.(stock|reserved|provisional|awaiting_assembly)\s*[-+]?=(?!=)")
VALUES = re.compile(r"\.values\([^)]*\b(stock|reserved|provisional|awaiting_assembly)\s*=", re.S)


def test_only_stock_module_changes_counters():
    root = Path(reprintbuddy.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "stock.py" and path.parent.name == "domain":
            continue
        text = path.read_text(encoding="utf-8")
        if ASSIGN.search(text) or VALUES.search(text):
            offenders.append(str(path.relative_to(root)))
    assert offenders == []
