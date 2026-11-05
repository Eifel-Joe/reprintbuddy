import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from reprintbuddy import auth
from reprintbuddy import settings as st
from reprintbuddy.db import make_engine, make_sessionmaker, upgrade
from reprintbuddy.ops.export import (
    EXPORT_FORMAT,
    EXPORT_FORMAT_VERSION,
    SECRET_SETTING_KEYS,
    export_bytes,
    export_data,
    to_jsonable,
)

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
def env(tmp_path):
    engine = make_engine(tmp_path / "e.db")
    upgrade(engine)
    maker = make_sessionmaker(engine)
    with maker() as session:
        auth.set_password(session, "admin", "pw_EXPORTCHECK_9")
    return engine, maker


def raw_setting(engine, key, value):
    with engine.begin() as conn:
        conn.execute(
            text("INSERT OR REPLACE INTO settings (key, value) VALUES (:k, :v)"),
            {"k": key, "v": json.dumps(value)},
        )


def test_export_envelope(env):
    engine, _ = env
    with engine.connect() as conn:
        data = export_data(conn, now=NOW)
        revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert data["format"] == EXPORT_FORMAT == "reprintbuddy-export"
    assert data["format_version"] == EXPORT_FORMAT_VERSION == 1
    assert data["schema_revision"] == revision
    assert data["exported_at"] == "2026-11-05T12:00:00+00:00"
    from reprintbuddy import __version__

    assert data["app_version"] == __version__
    assert "settings" in data["tables"] and "users" in data["tables"]
    assert "alembic_version" not in data["tables"]


def test_export_omits_secrets(env):
    from conftest import make_tls_pair

    engine, maker = env
    cert, _ = make_tls_pair()
    with maker() as session:
        st.set_setting(session, "bambuddy_api_key", "bb_EXPORTCHECK_1")
        st.set_setting(session, "tls_cert", cert)
        secret = st.ensure_session_secret(session)
        from reprintbuddy.models import User

        pw_hash = session.query(User).one().password_hash
    raw_setting(engine, "tls_key", "-----BEGIN PRIVATE KEY-----EXPORTCHECK")
    with engine.connect() as conn:
        blob = export_bytes(conn, now=NOW).decode("utf-8")
    for forbidden in (
        "EXPORTCHECK",
        secret,
        pw_hash,
        "password_hash",
        "session_secret",
        "bambuddy_api_key",
        "tls_key",
    ):
        assert forbidden not in blob, forbidden


def test_export_excludes_every_sensitive_key(env):
    engine, _ = env
    for key in SECRET_SETTING_KEYS:
        raw_setting(engine, key, "x-" + key)
    with engine.connect() as conn:
        rows = export_data(conn, now=NOW)["tables"]["settings"]
    assert {row["key"] for row in rows}.isdisjoint(SECRET_SETTING_KEYS)


def test_secret_keys_cover_the_never_export_set():
    assert SECRET_SETTING_KEYS == st.SENSITIVE | {"session_secret"}


def test_export_keeps_plain_settings(env):
    engine, maker = env
    with maker() as session:
        st.set_setting(session, "language", "de")
        st.set_setting(session, "port", 8080)
    with engine.connect() as conn:
        data = export_data(conn, now=NOW)
    settings = {row["key"]: row["value"] for row in data["tables"]["settings"]}
    assert settings["language"] == "de"
    assert settings["port"] == 8080 and isinstance(settings["port"], int)
    assert data["tables"]["users"][0]["username"] == "admin"


def test_export_skips_login_sessions(env):
    engine, maker = env
    with maker() as session:
        auth.create_session(session)
    with engine.connect() as conn:
        assert "sessions" not in export_data(conn, now=NOW)["tables"]


def test_export_includes_later_tables(env):
    engine, _ = env
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE widgets (id INTEGER PRIMARY KEY, label TEXT)"))
        conn.execute(text("INSERT INTO widgets (id, label) VALUES (1, 'a')"))
    with engine.connect() as conn:
        assert export_data(conn, now=NOW)["tables"]["widgets"] == [{"id": 1, "label": "a"}]


def test_export_bytes_is_utf8_json(env):
    engine, _ = env
    raw_setting(engine, "bambuddy_username", "Müller")
    with engine.connect() as conn:
        blob = export_bytes(conn, now=NOW)
    assert "Müller" in blob.decode("utf-8")  # no \u escapes
    assert json.loads(blob)["format"] == "reprintbuddy-export"


def test_to_jsonable():
    assert to_jsonable(datetime(2026, 11, 5, 12, 0)) == "2026-11-05T12:00:00+00:00"
    assert to_jsonable(datetime(2026, 11, 5, 14, 0, tzinfo=UTC)) == "2026-11-05T14:00:00+00:00"
    assert to_jsonable(date(2026, 11, 5)) == "2026-11-05"
    assert to_jsonable(Decimal("1.50")) == "1.50"
    assert to_jsonable(b"\x00\x01") == "AAE="
    assert to_jsonable(5) == 5
