"""The Bambuddy address is a text area that wraps, not a one-line field that cuts it off (v0.3.8 p113).

A line break or a blank inside the address is refused (a browser would otherwise read
"http://host\n:8000" as "http://host:8000" while the stored text keeps the break).
"""

import re

import pytest

from conftest import csrf_of, sm_of
from reprintbuddy import settings as st
from reprintbuddy.web.bambuddy_check import validate_address

LONG = "http://bambuddy-with-a-very-long-hostname.example.invalid:8000/some/long/path/segment"


def test_settings_page_has_a_wrapping_text_area(ready_client):
    html = ready_client.get("/settings?tab=bambuddy").text
    area = re.search(r'<textarea[^>]*id="bambuddy_address"[^>]*>(.*?)</textarea>', html, re.S)
    assert area and 'name="bambuddy_address"' in area.group(0)
    assert 'rows="2"' in area.group(0) and 'spellcheck="false"' in area.group(0) and 'autocomplete="off"' in area.group(0)
    assert "http://bambuddy.test:8000" in area.group(1)
    assert not re.search(r'<input[^>]*name="bambuddy_address"', html)


def test_setup_page_has_the_same_text_area(client):
    from conftest import finish_admin_setup

    finish_admin_setup(client)
    html = client.get("/setup/bambuddy/connect").text
    assert re.search(r'<textarea[^>]*name="bambuddy_address"', html)
    assert not re.search(r'<input[^>]*name="bambuddy_address"', html)


@pytest.mark.parametrize("address", [
    "http://bambuddy.test\n:8000", "http://bambuddy.test:80\r\n00", "http://bambuddy .test:8000", "http://bambuddy.test:8000\t/x",
])
def test_address_with_break_or_blank_is_invalid(address):
    assert validate_address(address) == (None, "bambuddy.url_blank")


def test_a_long_address_and_outer_whitespace_are_fine():
    assert validate_address(f"  {LONG}\n") == (LONG, None)


def test_post_with_a_line_break_is_refused_and_nothing_changes(ready_client):
    with sm_of(ready_client).begin() as s:
        before = st.get_setting(s, "bambuddy_url")
    token = csrf_of(ready_client, "/settings?tab=bambuddy")
    response = ready_client.post(
        "/settings/bambuddy",
        data={"csrf_token": token, "bambuddy_address": "http://other.test\n:8000", "bambuddy_token": "bb_key12345"},
    )
    assert response.status_code == 200
    assert "must not contain blanks or line breaks" in response.text  # says why, not the general address rule
    with sm_of(ready_client).begin() as s:
        assert st.get_setting(s, "bambuddy_url") == before


def test_css_wraps_the_address_area():
    from pathlib import Path

    import reprintbuddy.web

    css = (Path(reprintbuddy.web.__file__).parent / "static/css/reprintbuddy.css").read_text(encoding="utf-8")
    assert "textarea.address" in css and "overflow-wrap: anywhere" in css[css.index("textarea.address"):][:300]
