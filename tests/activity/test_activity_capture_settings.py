"""Settings and the account in the activity log, taken where they are saved (Spec 10.7, groups 1–2).

One line per saving with the changed settings; sensitive data only as "changed"/"removed"; internal
values never; a rolled back change leaves no line; a line that cannot be built is missing, the change
is saved anyway.
"""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import select

from reprintbuddy import auth
from reprintbuddy import settings as st
from reprintbuddy.activity import capture
from reprintbuddy.models import ActivityEntry, Setting


def lines(db) -> list[ActivityEntry]:
    with db() as s:
        return list(s.scalars(select(ActivityEntry).order_by(ActivityEntry.id)))


def test_one_line_per_saving_with_old_and_new(db):
    with db.begin() as s:
        st.set_setting(s, "automation", True, commit=False)
        st.set_setting(s, "poll_minutes", 10, commit=False)
        st.set_setting(s, "queue_cap", 10, commit=False)  # the default again: no change
    [line] = lines(db)
    assert (line.area, line.kind, line.actor) == ("settings", "settings_changed", "system")
    assert line.details == {
        "settings": [
            {"setting": "poll_minutes", "old": 15, "new": 10},
            {"setting": "automation", "old": False, "new": True},
        ]
    }
    assert line.address is None


def test_a_second_saving_compares_with_the_stored_value(db):
    with db.begin() as s:
        st.set_setting(s, "poll_minutes", 10, commit=False)
    with db.begin() as s:
        st.set_setting(s, "poll_minutes", 10, commit=False)  # unchanged
    with db.begin() as s:
        st.set_setting(s, "poll_minutes", 20, commit=False)
    assert [line.details["settings"] for line in lines(db)] == [
        [{"setting": "poll_minutes", "old": 15, "new": 10}],
        [{"setting": "poll_minutes", "old": 10, "new": 20}],
    ]


def test_set_setting_with_its_own_commit_is_logged(db):
    with db() as s:
        st.set_setting(s, "language", "de")
    [line] = lines(db)
    assert line.details == {"settings": [{"setting": "language", "old": "en", "new": "de"}]}


def test_sensitive_values_only_as_changed_or_removed(db):
    with db.begin() as s:
        st.set_setting(s, "ha_token", "SECRET-TOKEN-1", commit=False)
        st.set_setting(s, "tls_cert", "-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----", commit=False)
        st.set_setting(s, "tls_key", "-----BEGIN PRIVATE KEY-----\nSECRET-KEY\n-----END PRIVATE KEY-----", commit=False)
    with db.begin() as s:
        st.set_setting(s, "ha_token", None, commit=False)
    first, second = lines(db)
    assert first.details["settings"] == [
        {"setting": "https_certificate", "change": "uploaded"},
        {"setting": "https_key", "change": "changed"},
        {"setting": "ha_access_token", "change": "changed"},
    ]
    assert second.details["settings"] == [{"setting": "ha_access_token", "change": "removed"}]
    assert "SECRET" not in repr([line.details for line in lines(db)])


def test_an_address_only_with_scheme_host_and_port(db):
    with db.begin() as s:
        st.set_setting(s, "bambuddy_url", "http://user:pw@bambuddy.test:8000/x?key=SECRET#y", commit=False)
    [line] = lines(db)
    assert line.details["settings"] == [{"setting": "bambuddy_url", "old": None, "new": "http://bambuddy.test:8000"}]


def test_the_bambuddy_connection_names_the_owner_of_the_key(db):
    with db.begin() as s:
        st.set_setting(s, "bambuddy_url", "http://bambuddy.test:8000", commit=False)
        st.set_setting(s, "bambuddy_username", "reprintbuddy", commit=False)
        st.set_setting(s, "bambuddy_api_key", "KEY-SECRET", commit=False)
        st.set_setting(s, "bambuddy_review_confirmed", True, commit=False)  # internal
    [line] = lines(db)
    assert line.details["owner"] == "reprintbuddy"
    assert [change["setting"] for change in line.details["settings"]] == [
        "bambuddy_url", "bambuddy_username", "bambuddy_key",
    ]
    assert "KEY-SECRET" not in repr(line.details)


def test_internal_values_are_never_logged(db):
    with db() as s:
        st.ensure_session_secret(s)  # commits by itself
    with db.begin() as s:
        st.set_setting(s, "ha_since", "2026-11-09T12:00:00+00:00", commit=False)
        st.set_setting(s, "bambuddy_review_confirmed", True, commit=False)
    assert lines(db) == []


def test_the_queue_switch_is_shown_as_on_the_page(db):
    with db.begin() as s:
        st.set_setting(s, "queue_running", False, commit=False)
    [line] = lines(db)
    assert line.details["settings"] == [{"setting": "queue_running", "old": False, "new": True}]


def test_a_rolled_back_change_leaves_no_line(db):
    with pytest.raises(RuntimeError):
        with db.begin() as s:
            st.set_setting(s, "automation", True, commit=False)
            raise RuntimeError("refused")
    assert lines(db) == []
    with db.begin() as s:
        st.set_setting(s, "queue_cap", 5, commit=False)
    [line] = lines(db)
    assert line.details["settings"] == [{"setting": "queue_cap", "old": 10, "new": 5}]


def test_a_line_that_cannot_be_built_is_missing_and_the_change_stays(db, monkeypatch, caplog):
    def broken(*_args, **_kwargs):
        raise ValueError("broken")

    monkeypatch.setattr(capture.values, "shown_change", broken)
    with caplog.at_level(logging.ERROR, logger="reprintbuddy.activity.capture"):
        with db.begin() as s:
            st.set_setting(s, "automation", True, commit=False)
    assert lines(db) == []
    with db() as s:
        assert st.get_setting(s, "automation") is True
    assert "activity log" in caplog.text


def test_who_and_where_come_from_the_session(db):
    with db.begin() as s:
        s.info[capture.ACTOR] = "user"
        s.info[capture.ADDRESS] = "192.0.2.7"
        st.set_setting(s, "automation", True, commit=False)
    [line] = lines(db)
    assert line.actor == "user"
    assert line.address is None  # the address belongs to sign-in events only (Spec 10.7)


def test_the_account_is_created_with_its_language_as_one_line(db):
    with db.begin() as s:
        s.info[capture.ACTOR] = "user"
        s.info[capture.ADDRESS] = "192.0.2.7"
        auth.create_admin(s, "admin", "a-long-password-1", commit=False)
        st.set_setting(s, "language", "de", commit=False)
    [line] = lines(db)
    assert (line.area, line.kind, line.actor, line.address) == ("login", "account_created", "user", "192.0.2.7")
    assert line.details is None


def test_a_new_password_is_logged_without_it(db):
    with db.begin() as s:
        auth.create_admin(s, "admin", "a-long-password-1", commit=False)
    with db() as s:
        s.info[capture.ACTOR] = "user"
        s.info[capture.ADDRESS] = "192.0.2.7"
        auth.set_password(s, "admin", "another-password-2")
    line = lines(db)[-1]
    assert (line.kind, line.actor, line.address) == ("credentials_changed", "user", "192.0.2.7")
    assert "password" not in repr(line.details).lower()


def test_a_command_on_the_server_is_the_user_with_a_note(db):
    with db.begin() as s:
        s.info[capture.ACTOR] = "user"
        s.info[capture.VIA] = "cli"
        st.set_setting(s, "port", 8081, commit=False)
    [line] = lines(db)
    assert (line.actor, line.via) == ("user", "cli")
    assert line.details["settings"] == [{"setting": "port", "old": 8080, "new": 8081}]


def test_writing_a_setting_row_directly_is_covered_too(db):
    """Not only `set_setting`: whatever writes a settings row through a session is seen."""
    with db.begin() as s:
        s.add(Setting(key="queue_cap", value="3"))
    [line] = lines(db)
    assert line.details["settings"] == [{"setting": "queue_cap", "old": 10, "new": 3}]


def test_a_failing_save_keeps_its_own_error_and_logs_no_secret(db, tmp_path, caplog):
    """Review finding 1: the save of the change itself fails (a locked database). Its own error goes
    through unchanged, and nothing of the log writes the statement's values into the system log."""
    import sqlite3

    from sqlalchemy.exc import OperationalError

    engine = db.kw["bind"]
    path = engine.url.database
    engine.dispose()
    lock = sqlite3.connect(path, timeout=0)
    lock.execute("BEGIN IMMEDIATE")
    try:
        with db() as s:
            s.connection().exec_driver_sql("PRAGMA busy_timeout=50")
            st.set_setting(s, "bambuddy_api_key", "SUPERSECRETKEY", commit=False)
            with caplog.at_level(logging.DEBUG):
                with pytest.raises(OperationalError):
                    s.commit()
    finally:
        lock.rollback()
        lock.close()
    assert "SUPERSECRETKEY" not in caplog.text
    assert "activity log" not in caplog.text  # not the log's fault


def test_a_new_key_with_the_same_owner_names_the_owner(db):
    """Review finding 2."""
    with db.begin() as s:
        st.set_setting(s, "bambuddy_username", "reprintbuddy", commit=False)
    with db.begin() as s:
        st.set_setting(s, "bambuddy_username", "reprintbuddy", commit=False)  # unchanged
        st.set_setting(s, "bambuddy_api_key", "KEY-2", commit=False)
    line = lines(db)[-1]
    assert line.details == {"settings": [{"setting": "bambuddy_key", "change": "changed"}], "owner": "reprintbuddy"}


def test_a_line_that_cannot_be_written_leaves_the_change(db, caplog):
    """Review finding 7: an actor nobody knows breaks the line's insert, never the change."""
    with caplog.at_level(logging.WARNING):
        with db.begin() as s:
            s.info[capture.ACTOR] = "intruder"
            st.set_setting(s, "automation", True, commit=False)
    with db() as s:
        assert st.get_setting(s, "automation") is True
    [line] = lines(db)
    assert line.actor == "system"  # an unknown actor counts as the system


def test_an_address_changed_only_behind_host_and_port_says_changed(db):
    """Review finding 13: a new path must not read "http://x -> http://x"."""
    with db.begin() as s:
        st.set_setting(s, "bambuddy_url", "http://bambuddy.test:8000", commit=False)
    with db.begin() as s:
        st.set_setting(s, "bambuddy_url", "http://bambuddy.test:8000/proxy", commit=False)
    assert lines(db)[-1].details["settings"] == [{"setting": "bambuddy_url", "change": "changed"}]


def test_a_database_without_the_log_table_keeps_its_change(tmp_path, caplog):
    """A database of an older version, written before its migration: the change counts, its line is
    missing (Spec 10.7 "die Änderung selbst gilt trotzdem")."""
    from reprintbuddy.db import known_revisions, make_engine, make_sessionmaker, upgrade

    engine = make_engine(tmp_path / "old.db")
    upgrade(engine, revision=known_revisions()[-2])
    maker = make_sessionmaker(engine)
    with caplog.at_level(logging.WARNING):
        with maker() as s:
            st.set_setting(s, "queue_cap", 9)
    with maker() as s:
        assert st.get_setting(s, "queue_cap") == 9
    engine.dispose()
