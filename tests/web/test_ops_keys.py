"""Every `ops.*` text key used in a route exists in both languages."""

import re
from pathlib import Path

from reprintbuddy.web import i18n

ROUTES = Path(i18n.__file__).parent / "routes"


def test_ops_keys_in_routes_exist():
    literal = re.compile(r"""["'](ops\.[a-z_]+(?:\.[a-z_]+)*)["']""")
    used = {
        m.group(1)
        for path in ROUTES.glob("*.py")
        for m in literal.finditer(path.read_text(encoding="utf-8"))
    }
    assert "ops.tls.invalid" in used, "scan found nothing: the pattern is wrong"
    locales = i18n.load_locales()
    for lang in ("en", "de"):
        assert used <= set(locales[lang]), (lang, sorted(used - set(locales[lang])))
