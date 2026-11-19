"""The small browser of the end-to-end run (tests/system/webclient.py): which fields a form sends."""

from __future__ import annotations

import pytest

from webclient import Page, WebError, _multipart, forms

HTML = """
<form action="/a" method="post"><input type="hidden" name="csrf_token" value="t1">
  <input name="name" value="old"><input type="checkbox" name="off"><input type="checkbox" name="on" checked>
  <input type="radio" name="r" value="x"><input type="radio" name="r" value="y" checked>
  <select name="s"><option value="1">one</option><option value="2" selected>two</option></select>
  <select name="first"><option value="f">f</option><option value="g">g</option></select>
  <textarea name="t">some &amp; text</textarea><input type="submit" name="go" value="Go">
  <input name="gone" disabled value="z"><input type="file" name="upload">
</form>
<form action="/b?x=1" method="post" id="other" enctype="multipart/form-data"></form>
<input type="hidden" name="outside" value="o" form="other">
"""


def test_a_form_sends_what_a_browser_would() -> None:
    first, second = forms(HTML)
    assert (first.action, first.method) == ("/a", "post")
    assert first.fields == [
        ("csrf_token", "t1"), ("name", "old"), ("on", "on"), ("r", "y"), ("s", "2"), ("first", "f"), ("t", "some & text"),
    ]
    assert first.files == ["upload"]
    assert (second.action, second.enctype, second.fields) == ("/b?x=1", "multipart/form-data", [("outside", "o")])


def test_the_form_is_found_by_its_path_and_must_be_there_once() -> None:
    page = Page(200, "http://h/p", HTML.encode(), {})
    assert page.form("/b").form_id == "other"
    with pytest.raises(WebError, match="0 forms send to /c"):
        page.form("/c")
    twice = Page(200, "http://h/p", (HTML + '<form action="/a"></form>').encode(), {})
    with pytest.raises(WebError, match="2 forms"):
        twice.form("/a")


def test_a_multipart_body_holds_fields_and_the_file() -> None:
    body, kind = _multipart([("a", "1")], {"f": ("b.zip", b"\x00PK")})
    boundary = kind.split("boundary=")[1]
    assert body.startswith(f"--{boundary}\r\n".encode())
    assert b'name="a"\r\n\r\n1\r\n' in body
    assert b'name="f"; filename="b.zip"\r\nContent-Type: application/octet-stream\r\n\r\n\x00PK\r\n' in body
    assert body.endswith(f"--{boundary}--\r\n".encode())
