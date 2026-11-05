import pytest

from reprintbuddy import settings as st
from reprintbuddy.db import make_engine, make_sessionmaker, upgrade


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
    st.set_setting(session, "bambuddy_username", "reprintbuddy")
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


@pytest.mark.parametrize(
    "a,b,same",
    [
        ("http://h:8000", "http://h:8000/", True),
        ("http://H:8000", "http://h:8000", True),
        ("http://h", "http://h:80", True),
        ("https://h", "https://h:443", True),
        ("http://h:8000", "http://h:9000", False),
        ("http://h:8000", "https://h:8000", False),
        ("http://h:8000", "http://other:8000", False),
        ("http://h:8000", None, False),
        (None, None, False),
    ],
)
def test_same_address(a, b, same):
    assert st.same_address(a, b) is same


def test_api_key_is_stripped():
    assert st.validate("bambuddy_api_key", "  bb_key\n") == "bb_key"


@pytest.mark.parametrize("bad", ["bb key", "bb\tkey", "bb\x00key", "bb\x7fkey", "bbükey", "bb​key", "   "])
def test_api_key_with_unusable_characters_is_refused(bad):
    with pytest.raises(ValueError):
        st.validate("bambuddy_api_key", bad)


def test_never_export_covers_secrets_and_internal_values():
    assert st.SENSITIVE <= st.NEVER_EXPORT
    assert "session_secret" in st.NEVER_EXPORT
    assert "language" not in st.NEVER_EXPORT


def test_set_setting_commits_by_default(session):
    st.set_setting(session, "queue_cap", 25)
    with make_sessionmaker(session.get_bind())() as other:
        assert st.get_setting(other, "queue_cap") == 25


def test_set_setting_without_commit_leaves_the_transaction_to_the_caller(session):
    st.set_setting(session, "queue_cap", 25, commit=False)
    assert st.get_setting(session, "queue_cap") == 25  # visible inside the transaction
    session.rollback()
    assert st.get_setting(session, "queue_cap") == 10  # and gone with it


def test_tls_is_enabled_only_with_certificate_and_key(session):
    cert = "-----BEGIN CERTIFICATE-----\nabc"
    key = "-----BEGIN PRIVATE KEY-----\nabc"
    assert st.tls_enabled(session) is False
    st.set_setting(session, "tls_cert", cert)
    assert st.tls_enabled(session) is False
    st.set_setting(session, "tls_key", key)
    assert st.tls_enabled(session) is True
    st.set_setting(session, "tls_cert", None)
    assert st.tls_enabled(session) is False


@pytest.mark.parametrize("bad", ["http://h:99999", "http://h:abc", "http://h:0", "http://h:-1", "http://:8000", "http://", "https://[::1", "http://h:8000x"])
def test_url_with_a_bad_host_or_port_is_refused(bad):
    with pytest.raises(ValueError):
        st.validate("bambuddy_url", bad)


@pytest.mark.parametrize("good", ["http://h:1", "http://h:65535", "https://[::1]:8000", "http://h"])
def test_url_with_a_good_port_is_accepted(good):
    assert st.validate("bambuddy_url", good) == good


def test_same_address_does_not_fail_on_bad_input():
    assert st.same_address("http://h:99999", "http://h:80") is False
    assert st.same_address("http://h:80", "http://h:abc") is False
