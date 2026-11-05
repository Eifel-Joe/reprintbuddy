from reprintbuddy import auth
from reprintbuddy.db import make_engine, make_sessionmaker, upgrade
import pytest


@pytest.fixture
def session(tmp_path):
    engine = make_engine(tmp_path / "a.db")
    upgrade(engine)
    with make_sessionmaker(engine)() as s:
        yield s


def test_set_password_trims_and_refuses_empty_names(session):
    auth.set_password(session, "  admin ", "password123")
    assert auth.authenticate(session, "admin", "password123") is not None
    for blank in ("", "   "):
        with pytest.raises(ValueError):
            auth.set_password(session, blank, "password123")
    assert auth.authenticate(session, "admin", "password123") is not None  # unchanged


def test_create_admin_refuses_an_empty_name(session):
    with pytest.raises(ValueError):
        auth.create_admin(session, "  ", "password123")
    assert not auth.has_user(session)
