import re

from conftest import sm_of
from factories import make_article
from reprintbuddy import settings as st


def test_list_empty(ready_client):
    page = ready_client.get("/articles")
    assert page.status_code == 200 and "/articles/new" in page.text


def test_list_shows_numbers(ready_client):
    with sm_of(ready_client).begin() as s:
        make_article(s, name="Template A", sku="A-100", stock=1, reserved=3, min_stock=4, buffer=2)
    page = ready_client.get("/articles").text
    assert "Template A" in page and "A-100" in page
    assert '<td class="num neg">-2</td>' in page
    # Target quantity with its fill bar (0 % here: nothing available); reserved, provisional and
    # awaiting assembly are on the detail page now (Spec 10.1, design section 3).
    assert '<td class="num target">6<span class="bar tone-bad" role="progressbar"' in page
    assert 'aria-valuenow="0"' in page
    assert "4 / 6" not in page and "Reserved" not in page


def test_list_positive_available_not_red(ready_client):
    with sm_of(ready_client).begin() as s:
        make_article(s, name="Template A", stock=3, reserved=1)
    page = ready_client.get("/articles").text
    assert '<td class="num">2</td>' in page and "neg" not in page.split("<tbody>")[1]


def test_list_german_headers(ready_client):
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
        make_article(s, name="Template A")
    page = ready_client.get("/articles").text
    for header in ("Name / Artikelnummer", "Status", "Bestand", "Verfügbar", "Zielmenge"):
        assert re.search(rf"<th[^>]*>{header}</th>", page), header
    for gone in ("Reserviert", "Vorläufig", "Wartet auf Montage"):  # on the detail page now
        assert gone not in page, gone


def test_nav_articles_active(ready_client):
    assert 'class="nav-link active" href="/articles"' in ready_client.get("/articles").text
