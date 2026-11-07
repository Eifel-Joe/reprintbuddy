"""The factory builds the Bambuddy client from the stored settings (B2 Task 8, Decision 15)."""

from sqlalchemy import event

from reprintbuddy import settings as st
from reprintbuddy.bambuddy.client import HttpBambuddyClient
from reprintbuddy.bambuddy.connection import ConnectionCheck
from reprintbuddy.bambuddy.factory import make_http_factory

OK = ConnectionCheck(ok=True, reason="ok", owner="reprintbuddy")


def _http():
    import httpx

    return httpx.AsyncClient()


def _store(db, url="http://bambuddy.test:8000", key="bb_testkey"):
    with db() as session:
        st.apply_check(session, url, key, OK)


def test_factory_none_until_configured(db):
    factory = make_http_factory(db, _http)
    assert factory() is None
    _store(db)
    client = factory()
    assert isinstance(client, HttpBambuddyClient)
    assert "bb_testkey" not in repr(client)


def test_factory_reads_current_settings(db):
    factory = make_http_factory(db, _http)
    _store(db)
    assert factory()._base_url == "http://bambuddy.test:8000"
    _store(db, url="http://other.test:9000", key="bb_other")
    client = factory()
    assert client._base_url == "http://other.test:9000"
    assert client._api_key == "bb_other"


def test_factory_does_not_hold_a_session(db):
    engine = db.kw["bind"]
    out = {"n": 0}
    event.listen(engine, "checkout", lambda *a: out.__setitem__("n", out["n"] + 1))
    event.listen(engine, "checkin", lambda *a: out.__setitem__("n", out["n"] - 1))
    factory = make_http_factory(db, _http)
    _store(db)
    out["n"] = 0
    assert factory() is not None
    assert out["n"] == 0
    # The same when nothing is configured.
    with db.begin() as session:
        st.set_setting(session, "bambuddy_api_key", None, commit=False)
    out["n"] = 0
    assert factory() is None
    assert out["n"] == 0
