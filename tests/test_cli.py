import os
import subprocess
import sys

import pytest
import uvicorn

from reprintbuddy import auth
from reprintbuddy import settings as st
from reprintbuddy.cli import main
from reprintbuddy.db import make_engine, make_sessionmaker, upgrade

CERT = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
KEY = "-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----\n"


@pytest.fixture
def db(tmp_path):
    return tmp_path / "cli.db"


def open_session(db):
    engine = make_engine(db)
    upgrade(engine)  # a rejected command may not have created the database
    return make_sessionmaker(engine)()


def feed_passwords(monkeypatch, *answers):
    """getpass reads the terminal, not stdin, on Windows: replace it."""
    replies = iter(answers)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(replies))


def test_migrate_creates_db(db):
    assert main(["migrate", "--db", str(db)]) == 0
    assert db.exists()
    with open_session(db) as s:
        assert st.get_setting(s, "language") == "en"
        assert not auth.has_user(s)


def test_init_sets_language(db):
    assert main(["init", "--language", "de", "--db", str(db)]) == 0
    with open_session(db) as s:
        assert st.get_setting(s, "language") == "de"


def test_init_rejects_unknown_language(db):
    with pytest.raises(SystemExit) as info:
        main(["init", "--language", "fr", "--db", str(db)])
    assert info.value.code == 2


def test_set_password(db, monkeypatch):
    feed_passwords(monkeypatch, "new-password-1", "new-password-1")
    assert main(["set-password", "--username", "admin", "--db", str(db)]) == 0
    with open_session(db) as s:
        assert auth.authenticate(s, "admin", "new-password-1") is not None


def test_set_password_replaces_existing_account(db, monkeypatch):
    feed_passwords(monkeypatch, "first-password", "first-password")
    main(["set-password", "--username", "admin", "--db", str(db)])
    feed_passwords(monkeypatch, "second-password", "second-password")
    assert main(["set-password", "--username", "admin", "--db", str(db)]) == 0
    with open_session(db) as s:
        assert auth.authenticate(s, "admin", "first-password") is None
        assert auth.authenticate(s, "admin", "second-password") is not None


def test_set_password_mismatch(db, monkeypatch, capsys):
    feed_passwords(monkeypatch, "new-password-1", "different-pass-2")
    assert main(["set-password", "--username", "admin", "--db", str(db)]) == 2
    assert "do not match" in capsys.readouterr().err
    with open_session(db) as s:
        assert not auth.has_user(s)


def test_set_password_short(db, monkeypatch, capsys):
    feed_passwords(monkeypatch, "short", "short")
    assert main(["set-password", "--username", "admin", "--db", str(db)]) == 2
    assert "at least 8" in capsys.readouterr().err
    with open_session(db) as s:
        assert not auth.has_user(s)


def test_set_port(db):
    assert main(["set-port", "9090", "--db", str(db)]) == 0
    with open_session(db) as s:
        assert st.get_setting(s, "port") == 9090


@pytest.mark.parametrize("bad", ["0", "70000", "abc", "-5"])
def test_set_port_invalid(db, bad, capsys):
    assert main(["set-port", bad, "--db", str(db)]) == 2
    assert "port" in capsys.readouterr().err.lower()
    with open_session(db) as s:
        assert st.get_setting(s, "port") == 8080


def test_serve_uses_stored_host_and_port(db, monkeypatch):
    main(["set-port", "9191", "--db", str(db)])
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    assert main(["serve", "--db", str(db)]) == 0
    (app, kwargs), = calls
    assert kwargs["host"] == "0.0.0.0"
    assert kwargs["port"] == 9191
    assert kwargs.get("ssl_certfile") is None and kwargs.get("ssl_keyfile") is None
    assert app.title == "Reprintbuddy"


def test_serve_writes_tls_to_private_temp_files_and_removes_them(db, monkeypatch):
    main(["migrate", "--db", str(db)])
    with open_session(db) as s:
        st.set_setting(s, "tls_cert", CERT)
        st.set_setting(s, "tls_key", KEY)
    seen = {}

    def fake_run(app, **kw):
        cert, key = kw["ssl_certfile"], kw["ssl_keyfile"]
        seen["paths"] = (cert, key)
        seen["content"] = (open(cert).read(), open(key).read())
        if os.name == "posix":
            seen["modes"] = (os.stat(cert).st_mode & 0o777, os.stat(key).st_mode & 0o777)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    assert main(["serve", "--db", str(db)]) == 0
    assert seen["content"] == (CERT, KEY)
    if os.name == "posix":
        assert seen["modes"] == (0o600, 0o600)
    assert not any(os.path.exists(p) for p in seen["paths"])


def test_serve_removes_tls_files_even_if_server_crashes(db, monkeypatch):
    main(["migrate", "--db", str(db)])
    with open_session(db) as s:
        st.set_setting(s, "tls_cert", CERT)
        st.set_setting(s, "tls_key", KEY)
    paths = []

    def crashing_run(app, **kw):
        paths.extend([kw["ssl_certfile"], kw["ssl_keyfile"]])
        raise RuntimeError("boom")

    monkeypatch.setattr(uvicorn, "run", crashing_run)
    with pytest.raises(RuntimeError):
        main(["serve", "--db", str(db)])
    assert paths and not any(os.path.exists(p) for p in paths)


def test_python_dash_m_runs_main(db):
    result = subprocess.run(
        [sys.executable, "-m", "reprintbuddy", "migrate", "--db", str(db)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert db.exists()


def test_set_host(db):
    assert main(["set-host", "127.0.0.1", "--db", str(db)]) == 0
    with open_session(db) as s:
        assert st.get_setting(s, "host") == "127.0.0.1"


@pytest.mark.parametrize("bad", ["203.0.113.77", "no-such-host.invalid", "", "  "])
def test_set_host_refuses_an_address_that_cannot_be_bound(db, bad, capsys):
    assert main(["set-host", bad, "--db", str(db)]) == 2
    assert "host" in capsys.readouterr().err.lower()
    with open_session(db) as s:
        assert st.get_setting(s, "host") == "0.0.0.0"


def test_serve_does_not_use_tls_with_a_certificate_alone(db, monkeypatch):
    main(["migrate", "--db", str(db)])
    with open_session(db) as s:
        st.set_setting(s, "tls_cert", CERT)
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(kw))
    assert main(["serve", "--db", str(db)]) == 0
    assert calls[0].get("ssl_certfile") is None


def test_set_password_trims_the_user_name(db, monkeypatch):
    feed_passwords(monkeypatch, "new-password-1", "new-password-1")
    assert main(["set-password", "--username", "  admin ", "--db", str(db)]) == 0
    with open_session(db) as s:
        assert auth.authenticate(s, "admin", "new-password-1") is not None


@pytest.mark.parametrize("blank", ["", "   "])
def test_set_password_refuses_an_empty_user_name(db, monkeypatch, capsys, blank):
    feed_passwords(monkeypatch, "new-password-1", "new-password-1")
    assert main(["set-password", "--username", blank, "--db", str(db)]) == 2
    assert "user name" in capsys.readouterr().err.lower()
    with open_session(db) as s:
        assert not auth.has_user(s)


def test_set_password_asks_nothing_when_the_name_is_empty(db, monkeypatch):
    def must_not_ask(prompt=""):
        raise AssertionError("the password was asked although the user name is empty")

    monkeypatch.setattr("getpass.getpass", must_not_ask)
    assert main(["set-password", "--username", " ", "--db", str(db)]) == 2


def test_set_port_below_1024_is_allowed_but_warned_about(db, capsys):
    assert main(["set-port", "80", "--db", str(db)]) == 0
    output = capsys.readouterr()
    assert "1024" in output.out + output.err and "install script" in output.out + output.err
    assert "does not run as root" not in output.out + output.err
    with open_session(db) as s:
        assert st.get_setting(s, "port") == 80


def test_set_port_above_1023_has_no_warning(db, capsys):
    assert main(["set-port", "8080", "--db", str(db)]) == 0
    assert "1024" not in capsys.readouterr().out


def test_serve_passes_effective_server(db_path, monkeypatch):
    import uvicorn

    from reprintbuddy.cli import main
    from reprintbuddy.ops.server import EffectiveServer

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: seen.update(app=app, **kwargs))
    assert main(["serve", "--db", str(db_path)]) == 0
    assert seen["app"].state.ops.effective == EffectiveServer("0.0.0.0", 8080, None)


def test_disable_https(db, capsys):
    main(["migrate", "--db", str(db)])
    with open_session(db) as s:
        st.set_setting(s, "tls_cert", CERT)
        st.set_setting(s, "tls_key", KEY)
    assert main(["disable-https", "--db", str(db)]) == 0
    with open_session(db) as s:
        assert st.get_setting(s, "tls_cert") is None
        assert st.get_setting(s, "tls_key") is None
    assert "restart" in capsys.readouterr().out.lower()


def test_cli_version(capsys):
    with pytest.raises(SystemExit) as stop:
        main(["--version"])
    assert stop.value.code == 0
    assert capsys.readouterr().out.strip() == "reprintbuddy 0.3.3"


@pytest.mark.parametrize("state,code", [("succeeded", 0), ("rolled_back", 1), ("failed", 1), ("running", 3)])
def test_cli_update_exit_codes(db, monkeypatch, state, code):
    from reprintbuddy.ops.status import UpdateStatus

    main(["migrate", "--db", str(db)])
    calls = []

    def fake(db_path, **kwargs):
        calls.append((db_path, kwargs))
        return UpdateStatus(state=state, step="done", message="m")

    monkeypatch.setattr("reprintbuddy.cli.run_update", fake)
    assert main(["update", "--db", str(db), "--target", "v0.2.0"]) == code
    assert calls[0][0] == db
    assert calls[0][1]["target"] == "v0.2.0"


def test_cli_update_missing_db(db, capsys):
    assert main(["update", "--db", str(db)]) == 1
    assert not db.exists()


def test_cli_url_local(db, capsys):
    main(["migrate", "--db", str(db)])
    assert main(["url", "--db", str(db)]) == 0
    assert capsys.readouterr().out.strip() == "http://127.0.0.1:8080"


def test_cli_url_next_fresh(db, capsys):
    main(["migrate", "--db", str(db)])
    assert main(["url", "--db", str(db), "--host", "203.0.113.10", "--next"]) == 0
    assert capsys.readouterr().out.strip() == "http://203.0.113.10:8080/setup"


def test_cli_url_next_after_setup(db, capsys):
    main(["migrate", "--db", str(db)])
    with open_session(db) as s:
        auth.set_password(s, "admin", "password123")
    assert main(["url", "--db", str(db), "--host", "203.0.113.10", "--next"]) == 0
    assert capsys.readouterr().out.strip() == "http://203.0.113.10:8080/"


def test_cli_url_tls(db, capsys):
    main(["migrate", "--db", str(db)])
    with open_session(db) as s:
        st.set_setting(s, "tls_cert", CERT)
        st.set_setting(s, "tls_key", KEY)
        st.set_setting(s, "port", 8443)
    assert main(["url", "--db", str(db)]) == 0
    assert capsys.readouterr().out.strip() == "https://127.0.0.1:8443"


def test_cli_url_uses_the_saved_host(db, capsys):
    main(["migrate", "--db", str(db)])
    with open_session(db) as s:
        st.set_setting(s, "host", "203.0.113.5")  # stored directly: set-host would check it
    main(["url", "--db", str(db)])
    assert capsys.readouterr().out.strip() == "http://203.0.113.5:8080"


def test_cli_url_missing_db(db, capsys):
    assert main(["url", "--db", str(db)]) == 1
    assert not db.exists()


@pytest.mark.parametrize("with_tls", [False, True])
def test_serve_limits_the_graceful_shutdown(db, monkeypatch, with_tls):
    """Open live streams must not hold the stop back: Uvicorn waits forever without a limit."""
    main(["migrate", "--db", str(db)])
    if with_tls:
        with open_session(db) as s:
            st.set_setting(s, "tls_cert", CERT)
            st.set_setting(s, "tls_key", KEY)
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append(kw))
    assert main(["serve", "--db", str(db)]) == 0
    (kwargs,) = calls
    assert (kwargs.get("ssl_certfile") is not None) == with_tls
    assert kwargs["timeout_graceful_shutdown"] == 3
    assert "workers" not in kwargs  # one process (Spec 10.5.8)
