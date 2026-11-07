"""Translations for the web interface: the neutral `reprintbuddy.i18n`, re-exported.

The functions live outside the web package so the domain can translate without importing
it. Patch `reprintbuddy.i18n` (not this module) to change what `translate` reads.
"""

from reprintbuddy.i18n import FALLBACK, LOCALES_DIR, _read, load_locales, translate, translate_count

__all__ = ["FALLBACK", "LOCALES_DIR", "_read", "load_locales", "translate", "translate_count"]
