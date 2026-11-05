"""Translations: flat JSON files, English is the fallback."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"
FALLBACK = "en"


@lru_cache(maxsize=1)
def _read() -> dict[str, dict[str, str]]:
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(LOCALES_DIR.glob("*.json"))
    }


def load_locales() -> dict[str, dict[str, str]]:
    return _read()


def translate(lang: str, key: str, **params: object) -> str:
    """Text for `key` in `lang`; a key missing there falls back to English.

    A key missing in English raises `KeyError`: that is a programming error.
    """
    locales = load_locales()
    text = locales.get(lang, {}).get(key)
    if text is None:
        text = locales[FALLBACK][key]
    return text.format(**params) if params else text
