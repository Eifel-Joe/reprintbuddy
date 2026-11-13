"""The texts the tests look for, from the language files (refactoring before 1.0.0, R2).

Before, each of about sixty test files defined its own `en` (and some a `de`). `en_html` is the
text as a page carries it: escaped, so an apostrophe reads `&#39;`.
"""

from __future__ import annotations

from markupsafe import escape

from reprintbuddy.web.i18n import translate


def en(key: str, **params: object) -> str:
    return translate("en", key, **params)


def en_html(key: str, **params: object) -> str:
    return str(escape(translate("en", key, **params)))


def de(key: str, **params: object) -> str:
    return translate("de", key, **params)


def de_html(key: str, **params: object) -> str:
    return str(escape(translate("de", key, **params)))
