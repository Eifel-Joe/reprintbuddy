"""Settings of the push messages through Home Assistant (Spec 10.6, 11.5a, 11.6)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from reprintbuddy import settings as st
from reprintbuddy.db import make_engine, make_sessionmaker, upgrade
from reprintbuddy.ops import fullbackup as fb
from reprintbuddy.ops.export import export_data

TOKEN = "eyJhbGciOiJIUzI1NiJ9.TOKENCHECK.sig"
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


@pytest.fixture
def maker(tmp_path):
    engine = make_engine(tmp_path / "data" / "reprintbuddy.db")
    upgrade(engine)
    return make_sessionmaker(engine)


@pytest.fixture
def session(maker):
    with maker() as s:
        yield s


def test_defaults_mean_off_and_nothing_entered(session):
    assert st.get_setting(session, "ha_enabled") is False
    assert st.get_setting(session, "ha_url") is None
    assert st.get_setting(session, "ha_token") is None
    assert st.get_setting(session, "ha_service") is None
    assert st.get_setting(session, "ha_names") is True  # Spec 10.6: names on by default
    assert st.get_setting(session, "ha_events") == list(st.HA_EVENTS)
    assert st.get_setting(session, "ha_since") is None
    assert st.ha_configured(session) is False


def test_the_seven_events_in_their_order():
    assert st.HA_EVENTS == (
        "print_done",
        "print_failed",
        "paused",
        "below_min",
        "reorder",
        "trouble",
        "rights",
    )


def test_token_is_sensitive_and_never_exported():
    assert "ha_token" in st.SENSITIVE
    assert "ha_token" in st.NEVER_EXPORT
    assert st.display_value("ha_token", TOKEN) == st.MASK
    assert st.display_value("ha_token", None) == ""


@pytest.mark.parametrize(
    "given,stored",
    [
        ("http://homeassistant.test:8123", "http://homeassistant.test:8123"),
        ("  https://ha.test/  ", "https://ha.test"),
        ("http://192.0.2.5:8123", "http://192.0.2.5:8123"),
        ("http://proxy.test/ha", "http://proxy.test/ha"),  # a path in front, behind a proxy
    ],
)
def test_address_is_normalised(given, stored):
    assert st.validate("ha_url", given) == stored


@pytest.mark.parametrize(
    "bad",
    [
        "homeassistant.test:8123",
        "ftp://ha.test",
        "http://user:pw@ha.test:8123",
        "http://ha.test/?x=1",
        "http://ha.test/#frag",
        "http://ha.test/a b",
        "http://ha.test:0",
        "",
    ],
)
def test_address_refuses_what_is_no_plain_web_address(bad):
    with pytest.raises(ValueError):
        st.validate("ha_url", bad)


@pytest.mark.parametrize(
    "given,stored",
    [
        ("notify.mobile_app_my_phone", "notify.mobile_app_my_phone"),
        ("  notify.persistent_notification ", "notify.persistent_notification"),
        ("notify.all_phones", "notify.all_phones"),
    ],
)
def test_service_of_the_notify_domain_is_accepted(given, stored):
    assert st.validate("ha_service", given) == stored


@pytest.mark.parametrize(
    "bad",
    [
        "notify.send_message",  # knows no link (Spec 10.6)
        "light.turn_on",
        "lock.unlock",
        "notify",
        "notify.",
        "mobile_app_phone",
        "notify/mobile_app_phone",
        "notify.../x",
        "notify.Mobile",
        "notify.a b",
        "notify.a\nb",
        "",
    ],
)
def test_service_outside_the_notify_domain_is_refused(bad):
    with pytest.raises(ValueError):
        st.validate("ha_service", bad)


def test_token_is_stripped_and_must_be_visible_ascii():
    assert st.validate("ha_token", f"  {TOKEN}\n") == TOKEN
    for bad in ("", "   ", "abc def", "tök", "a​b"):
        with pytest.raises(ValueError) as info:
            st.validate("ha_token", bad)
        assert bad.strip() == "" or bad not in str(info.value)


def test_events_are_kept_once_in_their_order():
    assert st.validate("ha_events", ["rights", "print_done", "rights"]) == ["print_done", "rights"]
    assert st.validate("ha_events", []) == []
    with pytest.raises(ValueError):
        st.validate("ha_events", ["print_started"])
    with pytest.raises(ValueError):
        st.validate("ha_events", "print_done")


def test_since_is_a_time_with_zone():
    assert st.validate("ha_since", "2026-11-09T12:00:00+00:00") == "2026-11-09T12:00:00+00:00"
    assert st.validate("ha_since", None) is None
    for bad in ("yesterday", "2026-11-09T12:00:00", 5):
        with pytest.raises(ValueError):
            st.validate("ha_since", bad)


def test_configured_needs_switch_address_token_and_service(session):
    st.set_setting(session, "ha_url", "http://ha.test:8123")
    st.set_setting(session, "ha_token", TOKEN)
    st.set_setting(session, "ha_service", "notify.mobile_app_phone")
    assert st.ha_configured(session) is False  # switched off
    st.set_setting(session, "ha_enabled", True)
    assert st.ha_configured(session) is True
    st.set_setting(session, "ha_token", None)
    assert st.ha_configured(session) is False


def test_export_leaves_the_token_out(maker):
    with maker() as session:
        st.set_setting(session, "ha_token", TOKEN)
        st.set_setting(session, "ha_service", "notify.mobile_app_phone")
    engine = maker.kw["bind"]
    with engine.connect() as conn:
        data = export_data(conn, now=NOW)
    text = json.dumps(data)
    assert "TOKENCHECK" not in text
    keys = {row["key"] for row in data["tables"]["settings"]}
    assert "ha_service" in keys and "ha_token" not in keys


def test_full_backup_holds_the_token(maker, tmp_path):
    with maker() as session:
        st.set_setting(session, "ha_token", TOKEN)
    db_path = tmp_path / "data" / "reprintbuddy.db"
    work = tmp_path / "work"
    work.mkdir()
    created = fb.create_backup(db_path, work_dir=work, password=None, now=NOW)
    manifest = fb.read_manifest(created.path)
    target = work / "candidate.db"
    fb.extract_database(created.path, manifest, None, target)
    con = sqlite3.connect(target)
    try:
        (value,) = con.execute("SELECT value FROM settings WHERE key = 'ha_token'").fetchone()
    finally:
        con.close()
    assert json.loads(value) == TOKEN
