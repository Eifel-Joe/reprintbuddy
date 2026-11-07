"""The state of Bambuddy's live channel on Settings → Bambuddy (v0.3.6 p86).

The page says in plain words whether the channel is connected; when it is not, it says that
everything then runs through the regular query, and for `no_right` which right is missing.
The state follows the listener live: a change is published as topic `live-channel`.
"""

import pytest

from conftest import sm_of
from reprintbuddy import settings as st
from reprintbuddy.web import regions
from reprintbuddy.web.i18n import translate

TAB = "/settings?tab=bambuddy"


def channel_region(page: str) -> str:
    found = regions.extract(page, ["channel"])
    assert "channel" in found, "no region 'channel'"
    return found["channel"]


def set_state(client, state: str) -> None:
    client.app.state.live_listener.state = state


@pytest.mark.parametrize("lang", ["en", "de"])
@pytest.mark.parametrize("state", ["connecting", "connected", "waiting", "no_right"])
def test_each_state_has_its_own_text(ready_client, state, lang):
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", lang)
    set_state(ready_client, state)
    page = channel_region(ready_client.get(TAB).text)
    assert translate(lang, f"settings.channel.{state}", minutes=15, poll=15) in page


def test_connected_shows_no_fallback_hint(ready_client):
    set_state(ready_client, "connected")
    page = channel_region(ready_client.get(TAB).text)
    assert "settings.channel" not in page  # no raw key
    assert translate("en", "settings.channel.fallback", poll=15) not in page
    assert "websocket:connect" not in page


@pytest.mark.parametrize("state", ["connecting", "waiting", "no_right"])
def test_not_connected_says_the_regular_query_takes_over(ready_client, state):
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "poll_minutes", 20)
    set_state(ready_client, state)
    page = channel_region(ready_client.get(TAB).text)
    assert translate("en", "settings.channel.fallback", poll=20) in page


def test_no_right_names_the_missing_rights(ready_client):
    set_state(ready_client, "no_right")
    page = channel_region(ready_client.get(TAB).text)
    assert "websocket:connect" in page
    assert "Read Status" in page


def test_nothing_without_a_stored_address(ready_client):
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "bambuddy_url", None)
    set_state(ready_client, "no_right")
    page = ready_client.get(TAB).text
    assert "websocket:connect" not in page
    assert regions.extract(page, ["channel"]).get("channel", "").strip() == ""


def test_the_region_can_be_fetched_alone(ready_client):
    set_state(ready_client, "waiting")
    part = ready_client.get(TAB, headers={regions.HEADER: "channel"}).text
    assert translate("en", "settings.channel.waiting", minutes=15) in part


def test_the_region_listens_to_its_own_topic(ready_client):
    page = ready_client.get(TAB).text
    assert 'data-live-region="channel" data-live-topics="live-channel"' in page


def test_other_tabs_do_not_carry_the_region(ready_client):
    assert "data-live-region=\"channel\"" not in ready_client.get("/settings?tab=general").text


async def test_a_state_change_is_published(ready_client):
    app = ready_client.app
    sub = app.state.live.subscribe()
    try:
        app.state.live_listener.state = "no_right"
        topics = await sub.wait(1.0)
    finally:
        sub.close()
    assert topics is not None and "live-channel" in topics


async def test_the_same_state_again_publishes_nothing(ready_client):
    app = ready_client.app
    app.state.live_listener.state = "waiting"
    sub = app.state.live.subscribe()
    try:
        app.state.live_listener.state = "waiting"
        assert await sub.wait(0.05) is None
    finally:
        sub.close()


def test_page_holds_no_key_or_token(ready_client):
    set_state(ready_client, "no_right")
    page = ready_client.get(TAB).text
    assert "bb_testkey" not in page
