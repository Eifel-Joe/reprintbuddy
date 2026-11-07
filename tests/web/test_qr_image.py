"""The pictures of an article's QR code and what the code says (Spec 8.4)."""

import io
import re
from xml.etree import ElementTree

import pytest
import zxingcpp
from PIL import Image, ImageDraw
from conftest import sm_of
from factories import make_article

from reprintbuddy import settings as st
from reprintbuddy.web import qr

SETTING_BASE = "https://shelf.example.com"


@pytest.fixture
def article_id(ready_client):
    with sm_of(ready_client).begin() as s:
        return make_article(s, name="Template A").id


def read_code(picture: Image.Image) -> list[str]:
    """What a scanner reads from the picture: the texts of every QR code in it."""
    return [code.text for code in zxingcpp.read_barcodes(picture) if code.format == zxingcpp.BarcodeFormat.QRCode]


def draw_svg(data: bytes, scale: int = 4) -> Image.Image:
    """The SVG of `qr.svg` drawn by hand (no SVG library needed): a white square and one dark
    stroke path made of M/m, H/h and V/v moves, scaled like the file says."""
    root = ElementTree.fromstring(data)
    ns = {"s": "http://www.w3.org/2000/svg"}
    group = root.find("s:g", ns)
    factor = float(re.search(r"scale\(([\d.]+)\)", group.get("transform")).group(1))
    size = round(float(root.get("width")) / factor)
    picture = Image.new("L", (size * scale, size * scale), 0)
    canvas = ImageDraw.Draw(picture)
    for path in group.findall("s:path", ns):
        colour = 255 if path.get("fill") in ("#fff", "#ffffff") else 0
        x = y = 0.0
        tokens = re.findall(r"([MmHhVvZz])\s*(-?[\d.]+)?\s*(-?[\d.]+)?", path.get("d"))
        for command, a, b in tokens:
            if command == "z" or command == "Z":
                continue
            if command in "Mm":
                x, y = (float(a), float(b)) if command == "M" else (x + float(a), y + float(b))
            elif command in "Hh":
                new = float(a) if command == "H" else x + float(a)
                if path.get("stroke"):  # a stroke of width 1 centred on y
                    canvas.rectangle([x * scale, (y - 0.5) * scale, new * scale - 1, (y + 0.5) * scale - 1], fill=colour)
                elif path.get("fill"):  # the white square: filled below
                    canvas.rectangle([0, 0, size * scale, size * scale], fill=colour)
                x = new
            elif command in "Vv":
                y = float(a) if command == "V" else y + float(a)
    return picture


def test_address_is_short_and_fixed():
    assert qr.article_address("https://shelf.example.com", 7) == "https://shelf.example.com/a/7"
    assert qr.article_address("https://shelf.example.com/", 7) == "https://shelf.example.com/a/7"
    assert qr.article_address("http://testserver", 12) == "http://testserver/a/12"


def test_the_png_scans_to_the_address(ready_client, article_id):
    response = ready_client.get(f"/articles/{article_id}/qr.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    picture = Image.open(io.BytesIO(response.content))
    modules = len(qr.matrix(f"http://testserver/a/{article_id}"))
    assert picture.size == ((modules + 2 * 4) * 10,) * 2
    assert read_code(picture) == [f"http://testserver/a/{article_id}"]


def test_the_svg_scans_to_the_address(ready_client, article_id):
    response = ready_client.get(f"/articles/{article_id}/qr.svg")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.headers["cache-control"] == "no-store"
    assert response.text.startswith("<svg") and "<script" not in response.text
    assert read_code(draw_svg(response.content)) == [f"http://testserver/a/{article_id}"]


def test_the_ground_is_white_so_it_prints_on_any_page_colour():
    picture = Image.open(io.BytesIO(qr.png("https://shelf.example.com/a/7"))).convert("RGB")
    assert picture.getpixel((0, 0)) == (255, 255, 255)  # the quiet zone
    assert any(picture.getpixel((x, 45)) == (0, 0, 0) for x in range(picture.width))  # the dark modules


def test_the_code_survives_some_damage():
    """Error correction M: a small scratch over the code still reads (a code on a shelf)."""
    picture = Image.open(io.BytesIO(qr.png("https://shelf.example.com/a/7"))).convert("L")
    ImageDraw.Draw(picture).rectangle([170, 170, 195, 195], fill=0)
    assert read_code(picture) == ["https://shelf.example.com/a/7"]


def test_the_code_follows_the_setting(ready_client, article_id):
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "qr_base_url", SETTING_BASE, commit=False)
    response = ready_client.get(f"/articles/{article_id}/qr.png")
    assert read_code(Image.open(io.BytesIO(response.content))) == [f"{SETTING_BASE}/a/{article_id}"]


def test_download_names_the_file_by_the_id_only(ready_client, article_id):
    for kind in ("svg", "png"):
        response = ready_client.get(f"/articles/{article_id}/qr.{kind}?download=1")
        assert response.headers["content-disposition"] == f'attachment; filename="qr-article-{article_id}.{kind}"'
    assert "content-disposition" not in ready_client.get(f"/articles/{article_id}/qr.png").headers
    assert "content-disposition" not in ready_client.get(f"/articles/{article_id}/qr.png?download=0").headers


def test_unknown_article_or_format(ready_client, article_id):
    assert ready_client.get("/articles/999/qr.png").status_code == 404
    assert ready_client.get(f"/articles/{article_id}/qr.gif").status_code == 404
    assert ready_client.get(f"/articles/{article_id}/qr.PNG").status_code == 404
    assert ready_client.get("/articles/" + "9" * 19 + "/qr.png").status_code == 404


def test_archived_article_keeps_its_code(ready_client, article_id):
    from datetime import UTC, datetime

    from reprintbuddy.models import Article

    with sm_of(ready_client).begin() as s:
        s.get(Article, article_id).archived_at = datetime.now(UTC)
    assert ready_client.get(f"/articles/{article_id}/qr.png").status_code == 200


def test_signed_out_gets_no_picture(ready_client, article_id):
    ready_client.cookies.clear()
    response = ready_client.get(f"/articles/{article_id}/qr.png")
    assert response.status_code == 303 and response.headers["location"].startswith("/login")


@pytest.mark.parametrize(
    ("base", "local"),
    [
        ("http://localhost:8080", True),
        ("http://127.0.0.1:8080", True),
        ("http://127.1.2.3", True),
        ("http://[::1]:8080", True),
        ("http://app.localhost", True),
        ("https://shelf.example.com", False),
        ("http://testserver", False),
        ("http://10.0.0.5:8080", False),
        ("not a url", False),
    ],
)
def test_local_address(base, local):
    assert qr.is_local_address(base) is local
