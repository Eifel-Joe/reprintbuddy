from datetime import datetime

from reprintbuddy.web.formatting import format_local


def test_format_local_winter():
    assert format_local(datetime(2026, 1, 15, 12, 0), "de") == "15.01.2026 13:00"


def test_format_local_summer():
    assert format_local(datetime(2026, 7, 15, 12, 0), "de") == "15.07.2026 14:00"
    assert format_local(datetime(2026, 7, 15, 12, 0), "en") == "2026-07-15 14:00"


def test_format_local_unknown_language_uses_english_style():
    assert format_local(datetime(2026, 7, 15, 12, 0), "xx") == "2026-07-15 14:00"
