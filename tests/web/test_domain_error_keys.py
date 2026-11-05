"""Every key a domain function can raise or label must have a text in both languages."""

import re
from pathlib import Path

import reprintbuddy
from reprintbuddy.domain import stock
from reprintbuddy.web import i18n

DOMAIN = Path(reprintbuddy.__file__).parent / "domain"
ERROR_KEY = re.compile(
    r"""(?:Validation|Conflict|NotFound|StockLimit|Domain)Error\(\s*["']([a-z_]+(?:\.[a-z_]+)+)["']"""
)
FIELD_KEY = re.compile(r"""["']([a-z_]+\.field\.[a-z_]+)["']""")


def test_domain_error_and_field_keys_have_texts():
    en, de = i18n.load_locales()["en"], i18n.load_locales()["de"]
    used = set()
    for path in DOMAIN.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        used |= set(ERROR_KEY.findall(text)) | set(FIELD_KEY.findall(text))
    used |= {f"stock.limit.{f}" for f in ("stock", "reserved", "provisional", "awaiting_assembly", "part")}
    assert len(used) > 20, "scan found too little: the patterns are wrong"
    assert used <= set(en), f"missing in en: {sorted(used - set(en))}"
    assert used <= set(de), f"missing in de: {sorted(used - set(de))}"


def test_every_journal_kind_has_a_label():
    en, de = i18n.load_locales()["en"], i18n.load_locales()["de"]
    for kind in stock.KINDS:
        assert f"journal.kind.{kind}" in en and f"journal.kind.{kind}" in de, kind
