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


def test_export_holds_the_suppliers_of_a_part_and_the_mark_of_its_time(env):  # Spec 5.3, 9.4, 11.6
    from reprintbuddy.domain import catalog, reorder

    engine, maker = env
    with maker.begin() as s:
        part = catalog.create_part(s, catalog.PartInput(name="Insert M4", suppliers=[
            catalog.SupplierInput(name="Example Supplier", pack_size=50, is_default=True),
            catalog.SupplierInput(name="Example Supplier B", pack_size=10, lead_time_days=2),
        ]), 0, "user")
        second = catalog.part_suppliers(s, part.id)[1].id
        reorder.mark_ordered(s, "part", part.id, 2, supplier_id=second)
    with engine.connect() as conn:
        tables = export_data(conn, now=NOW)["tables"]
    assert [(r["name"], r["pack_size"], r["is_default"]) for r in tables["part_suppliers"]] == [
        ("Example Supplier", 50, True), ("Example Supplier B", 10, False),
    ]
    (mark,) = tables["reorder_marks"]
    assert (mark["qty"], mark["pack_size"], mark["supplier_name"]) == (2, 10, "Example Supplier B")


def test_export_holds_the_suppliers_of_a_spool_sort_and_an_order_taken_back(env):  # Spec 9.3, 9.4, 11.6
    from reprintbuddy.domain import filament, reorder
    from reprintbuddy.domain.suppliers import SupplierInput
    from reprintbuddy.models import FilamentSort

    engine, maker = env
    with maker.begin() as s:
        sort = FilamentSort(sort_key="brand a|pla||#000000", brand="Brand A", material="PLA",
                            color_hex="#000000", label="Brand A PLA Black")
        s.add(sort)
        s.flush()
        filament.save_settings(s, sort.id, filament.FilamentSettingsInput(suppliers=[
            SupplierInput(name="Example Shop", pack_size=1000, is_default=True),
            SupplierInput(name="Roll Shop", pack_size=500, lead_time_days=1),
        ]))
        second = filament.sort_suppliers(s, sort.id)[1].id
        mark = reorder.mark_ordered(s, "filament", sort.id, 2, supplier_id=second)
        reorder.withdraw_mark(s, mark.id, [])
    with engine.connect() as conn:
        tables = export_data(conn, now=NOW)["tables"]
    assert [(r["name"], r["pack_size"], r["is_default"]) for r in tables["filament_suppliers"]] == [
        ("Example Shop", 1000, True), ("Roll Shop", 500, False),
    ]
    (row,) = tables["reorder_marks"]
    assert (row["qty"], row["pack_size"], row["supplier_name"], row["withdrawn"]) == (2, 500, "Roll Shop", True)


def test_export_includes_assembled_tables_and_columns(env):  # 0.4.0, p341 (Spec 5.6, 11.6)
    from factories import make_article, make_assembled

    engine, maker = env
    with maker.begin() as s:
        segment = make_article(s, name="Segment")
        side = make_article(s, name="Side part L")
        socket = make_article(s, name="Socket L32")
        hose = make_assembled(s, name="Hose", sku="EX-H1", mode="order", base=[(segment.id, 12)],
                              fields=[[("-L32", [(side.id, 1), (socket.id, 1)])]])
        hose_id = hose.id
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO orders (id, reference, order_date) VALUES (1, 'R-1', '2026-11-11')"))
        conn.execute(text(
            "INSERT INTO order_lines (order_id, group_no, article_id, qty, set_qty, variant_sku, assembled_id)"
            f" VALUES (1, 1, {segment.id}, 12, 1, 'EX-H1-L32', {hose_id})"
        ))
    with engine.connect() as conn:
        tables = export_data(conn, now=NOW)["tables"]
    (option,) = tables["article_options"]
    assert (option["article_id"], option["field"], option["suffix"]) == (hose_id, 1, "-L32")
    assert sorted((r["field"], r["qty"]) for r in tables["article_components"]) == [(0, 12), (1, 1), (1, 1)]
    by_id = {r["id"]: r for r in tables["articles"]}
    assert (by_id[hose_id]["kind"], by_id[hose_id]["assembly_mode"]) == ("assembled", "order")
    variant = next(r for r in tables["articles"] if r["parent_id"] == hose_id)
    assert (variant["kind"], variant["sku"], variant["variant_key"]) == ("variant", "EX-H1-L32", str(option["id"]))
    assert by_id[segment.id]["sold_alone"] is True and by_id[segment.id]["kind"] == "print"
    (line,) = tables["order_lines"]
    assert (line["assembled_id"], line["variant_sku"]) == (hose_id, "EX-H1-L32")
