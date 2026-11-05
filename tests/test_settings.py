import pytest

from lagerbuddy import settings as st
from lagerbuddy.db import make_engine, make_sessionmaker, upgrade


@pytest.fixture
def session(tmp_path):
    engine = make_engine(tmp_path / "t.db")
    upgrade(engine)
    with make_sessionmaker(engine)() as s:
        yield s


EXPECTED_DEFAULTS = {
    "language": "en",
    "poll_minutes": 15,
    "queue_cap": 10,
    "insert_next": False,
    "automation": False,
    "queue_running": True,
    "bambuddy_url": None,
    "bambuddy_username": None,
    "bambuddy_api_key": None,
    "bambuddy_review_confirmed": False,
    "host": "0.0.0.0",
    "port": 8080,
    "tls_cert": None,
    "tls_key": None,
}


def test_defaults(session):
    assert st.DEFAULTS == EXPECTED_DEFAULTS
    for key, value in EXPECTED_DEFAULTS.items():
        assert st.get_setting(session, key) == value


def test_sensitive_keys():
    assert st.SENSITIVE == frozenset({"bambuddy_api_key", "tls_key"})


def test_roundtrip(session):
    st.set_setting(session, "queue_cap", 25)
    assert st.get_setting(session, "queue_cap") == 25


def test_invalid_language(session):
    with pytest.raises(ValueError):
        st.set_setting(session, "language", "fr")


@pytest.mark.parametrize("port", [0, 70000])
def test_invalid_port(session, port):
    with pytest.raises(ValueError):
        st.set_setting(session, "port", port)
    assert st.get_setting(session, "port") == 8080


def test_url_normalized(session):
    st.set_setting(session, "bambuddy_url", "http://h:8000/")
    assert st.get_setting(session, "bambuddy_url") == "http://h:8000"


def test_url_invalid(session):
    with pytest.raises(ValueError):
        st.set_setting(session, "bambuddy_url", "h:8000")


def test_unknown_key(session):
    with pytest.raises(KeyError):
        st.set_setting(session, "nope", 1)
    with pytest.raises(KeyError):
        st.get_setting(session, "nope")


def test_session_secret_stable(session):
    first = st.ensure_session_secret(session)
    second = st.ensure_session_secret(session)
    assert first == second
    assert len(first) >= 40


def test_session_secret_not_settable(session):
    with pytest.raises(KeyError):
        st.set_setting(session, "session_secret", "x" * 50)


def test_display_value_masks():
    assert st.display_value("bambuddy_api_key", "bb_secret") == "********"
    assert st.display_value("bambuddy_api_key", None) == ""
    assert st.display_value("port", 8080) == "8080"


def test_bool_fields_reject_non_bool(session):
    with pytest.raises(ValueError):
        st.set_setting(session, "automation", 1)


@pytest.mark.parametrize(
    "key,value",
    [
        ("poll_minutes", 0),
        ("poll_minutes", 1441),
        ("queue_cap", 0),
        ("queue_cap", 1000),
        ("host", ""),
        ("bambuddy_username", ""),
        ("bambuddy_api_key", ""),
        ("tls_cert", "not a pem"),
        ("tls_key", "not a pem"),
    ],
)
def test_invalid_values(session, key, value):
    with pytest.raises(ValueError):
        st.set_setting(session, key, value)


def test_secret_value_not_in_error_message(session):
    with pytest.raises(ValueError) as info:
        st.set_setting(session, "tls_key", "LEAKCHECK-not-a-pem")
    assert "LEAKCHECK" not in str(info.value)


def test_bambuddy_configured(session):
    st.set_setting(session, "bambuddy_url", "http://h:8000")
    st.set_setting(session, "bambuddy_username", "lagerbuddy")
    st.set_setting(session, "bambuddy_api_key", "bb_key")
    assert st.bambuddy_configured(session) is False  # checkbox missing
    st.set_setting(session, "bambuddy_review_confirmed", True)
    assert st.bambuddy_configured(session) is True


def test_validate_returns_normalised_value():
    assert st.validate("bambuddy_url", "http://h:8000/") == "http://h:8000"
    with pytest.raises(ValueError):
        st.validate("port", 0)
    with pytest.raises(KeyError):
        st.validate("session_secret", "x")
