"""Who saved it (Spec 10.7 "Von wem"): a request is the user with the device's address, a command on
the server is the user with a note, everything else is the system."""

from __future__ import annotations

from sqlalchemy import select

from reprintbuddy.cli import main
from reprintbuddy.db import make_engine, make_sessionmaker, upgrade
from reprintbuddy.models import ActivityEntry
from conftest import csrf_of, sm_of


def lines_of(maker) -> list[ActivityEntry]:
    with maker() as s:
        return list(s.scalars(select(ActivityEntry).order_by(ActivityEntry.id)))


def test_the_setup_logs_the_account_with_the_address(client):
    from conftest import finish_admin_setup

    finish_admin_setup(client)
    created = [line for line in lines_of(sm_of(client)) if line.kind == "account_created"]
    assert len(created) == 1
    assert created[0].actor == "user"
    assert created[0].address == "testclient"  # what the server sees of the test client
    assert not [line for line in lines_of(sm_of(client)) if line.kind == "settings_changed"]


def test_a_setting_saved_on_the_page_is_the_user(ready_client):
    admin_client = ready_client
    token = csrf_of(admin_client, "/settings?tab=general")
    response = admin_client.post("/settings/language", data={"csrf_token": token, "language": "de"})
    assert response.status_code == 303
    line = [line for line in lines_of(sm_of(admin_client)) if line.kind == "settings_changed"][-1]
    assert line.actor == "user"
    assert line.via is None
    assert line.details == {"settings": [{"setting": "language", "old": "en", "new": "de"}]}


def test_a_command_on_the_server_is_the_user_with_a_note(tmp_path, monkeypatch):
    path = tmp_path / "cli.db"
    assert main(["set-port", "--db", str(path), "8090"]) == 0
    replies = iter(["a-long-password-1", "a-long-password-1"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(replies))
    assert main(["set-password", "--username", "admin", "--db", str(path)]) == 0
    engine = make_engine(path)
    upgrade(engine)
    lines = lines_of(make_sessionmaker(engine))
    assert [(line.kind, line.actor, line.via) for line in lines] == [
        ("settings_changed", "user", "cli"),
        ("account_created", "user", "cli"),
    ]
    assert lines[0].details == {"settings": [{"setting": "port", "old": 8080, "new": 8090}]}
    engine.dispose()


def test_without_a_note_it_is_the_system(db):
    from reprintbuddy import settings as st

    with db.begin() as s:
        st.set_setting(s, "automation", True, commit=False)
    [line] = lines_of(db)
    assert line.actor == "system"
