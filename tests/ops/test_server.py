from reprintbuddy import settings as st
from reprintbuddy.ops.server import (
    EffectiveServer,
    address_changed,
    base_url,
    connect_host,
    effective_from_settings,
    tls_digest,
)


def test_connect_host():
    assert connect_host("0.0.0.0") == "127.0.0.1"
    assert connect_host("::") == "127.0.0.1"
    assert connect_host("") == "127.0.0.1"
    assert connect_host("203.0.113.10") == "203.0.113.10"


def test_base_url():
    assert base_url("203.0.113.10", 8080, False) == "http://203.0.113.10:8080"
    assert base_url("::1", 8443, True) == "https://[::1]:8443"


def test_tls_digest():
    assert tls_digest(None, "k") is None
    assert tls_digest("c", None) is None
    assert tls_digest("c", "k") == tls_digest("c", "k")
    assert tls_digest("c", "k") != tls_digest("c", "other")
    assert "k" not in (tls_digest("c", "k") or "")


def test_effective_defaults(db_path):
    from reprintbuddy.db import make_engine, make_sessionmaker, upgrade

    engine = make_engine(db_path)
    upgrade(engine)
    with make_sessionmaker(engine)() as session:
        assert effective_from_settings(session) == EffectiveServer("0.0.0.0", 8080, None)
        st.set_setting(session, "port", 9090)
        assert effective_from_settings(session).port == 9090


def test_effective_tls_property():
    assert EffectiveServer("0.0.0.0", 8080, "abc").tls
    assert not EffectiveServer("0.0.0.0", 8080, None).tls


def test_address_changed():
    base = EffectiveServer("0.0.0.0", 8080, None)
    assert address_changed(EffectiveServer("0.0.0.0", 9090, None), base)
    assert address_changed(EffectiveServer("127.0.0.1", 8080, None), base)
    assert address_changed(EffectiveServer("0.0.0.0", 8080, "abc"), base)  # HTTP -> HTTPS
    assert not address_changed(base, base)
    # another certificate alone does not change the address
    assert not address_changed(
        EffectiveServer("0.0.0.0", 8080, "new"), EffectiveServer("0.0.0.0", 8080, "old")
    )
