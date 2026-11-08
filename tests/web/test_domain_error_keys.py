"""Every key a domain function can raise or label must have a text in both languages."""

import re
from pathlib import Path

import reprintbuddy
from reprintbuddy.domain import stock
from reprintbuddy.models import Article
from reprintbuddy.web import i18n

ROOT = Path(reprintbuddy.__file__).parent
SCANNED = [ROOT / "domain", ROOT / "web"]
ERROR_KEY = re.compile(
    r"""(?:Validation|Conflict|NotFound|StockLimit|Domain)Error\(\s*["']([a-z_]+(?:\.[a-z_]+)+)["']"""
)
FIELD_KEY = re.compile(r"""["']([a-z_]+\.field\.[a-z_]+)["']""")


def test_domain_error_and_field_keys_have_texts():
    en, de = i18n.load_locales()["en"], i18n.load_locales()["de"]
    used = set()
    for path in (p for folder in SCANNED for p in folder.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        used |= set(ERROR_KEY.findall(text)) | set(FIELD_KEY.findall(text))
    # The counters of an article that a booking can push below 0 (`StockLimitError(f"stock.limit.{field}")`
    # in `domain/stock.py`): the CHECK constraints name them. A part has no such error (v0.3.10 p129).
    counters = {
        m.group(1)
        for c in Article.__table__.constraints
        if (m := re.fullmatch(r"(\w+) >= 0", str(getattr(c, "sqltext", ""))))
    }
    assert {"stock", "reserved", "provisional", "awaiting_assembly"} <= counters, counters
    used |= {f"stock.limit.{f}" for f in counters}
    assert len(used) > 20, "scan found too little: the patterns are wrong"
    assert used <= set(en), f"missing in en: {sorted(used - set(en))}"
    assert used <= set(de), f"missing in de: {sorted(used - set(de))}"


def test_every_journal_kind_has_a_label():
    en, de = i18n.load_locales()["en"], i18n.load_locales()["de"]
    for kind in stock.KINDS:
        assert f"journal.kind.{kind}" in en and f"journal.kind.{kind}" in de, kind
