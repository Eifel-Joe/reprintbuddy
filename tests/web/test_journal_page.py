from conftest import sm_of
from factories import make_article, make_part
from reprintbuddy import settings as st
from reprintbuddy.domain import stock
from reprintbuddy.web.i18n import translate


def en(key, **params):
    return translate("en", key, **params)


def test_all_kinds_have_labels():
    from reprintbuddy.web import i18n

    en_keys = i18n.load_locales()["en"]
    for kind in stock.KINDS:
        assert f"journal.kind.{kind}" in en_keys


def test_journal_page_lists_entries(ready_client):  # Spec 6.3
    with sm_of(ready_client).begin() as s:
        a = make_article(s, name="Template A", stock=5).id
        stock.stock_in(s, a, 4, "user", consume_parts=False)
        stock.sell_unreserved(s, a, 1, "user", note="direct sale")
    page = ready_client.get("/journal").text
    assert en("journal.kind.stock_in") in page and en("journal.kind.sale") in page
    assert page.index(en("journal.kind.sale")) < page.index(en("journal.kind.stock_in"))  # newest first
    assert en("journal.actor.user") in page and "direct sale" in page
    assert "Template A" in page and "9 → 8" in page


def test_journal_shows_auto_actor_and_parts(ready_client):
    with sm_of(ready_client).begin() as s:
        a = make_article(s).id
        p = make_part(s, name="Insert M4").id
        stock.add_provisional(s, a, 1, "auto")
        stock.receive_part(s, p, 50, "user")
    page = ready_client.get("/journal").text
    assert en("journal.actor.auto") in page and "Insert M4" in page and "0 → 50" in page


def test_journal_filter_article(ready_client):
    with sm_of(ready_client).begin() as s:
        a = make_article(s, name="Template A", stock=5).id
        b = make_article(s, name="Template B", stock=5).id
        p = make_part(s, name="Insert M4").id
        stock.sell_unreserved(s, a, 1, "user", note="note-for-a")
        stock.sell_unreserved(s, b, 1, "user", note="note-for-b")
        stock.receive_part(s, p, 5, "user", note="note-for-part")
    only_a = ready_client.get(f"/journal?article={a}").text
    assert "note-for-a" in only_a and "note-for-b" not in only_a and "note-for-part" not in only_a
    only_part = ready_client.get(f"/journal?part={p}").text
    assert "note-for-part" in only_part and "note-for-a" not in only_part


def test_journal_paging(ready_client):
    with sm_of(ready_client).begin() as s:
        a = make_article(s, stock=500).id
        for i in range(105):
            stock.sell_unreserved(s, a, 1, "user", note=f"entry-{i:03d}")
    first = ready_client.get("/journal").text
    assert "entry-104" in first and "entry-005" in first and "entry-004" not in first
    assert "page=2" in first
    second = ready_client.get("/journal?page=2").text
    assert "entry-004" in second and "entry-104" not in second


def test_journal_bad_parameters_no_500(ready_client):
    for query in ("?article=abc", "?page=-3", "?page=x", "?part=999999999999999999999", "?article=999"):
        assert ready_client.get(f"/journal{query}").status_code == 200, query


def test_journal_empty(ready_client):
    assert en("journal.empty") in ready_client.get("/journal").text


def test_journal_german(ready_client):
    with sm_of(ready_client).begin() as s:
        st.set_setting(s, "language", "de", commit=False)
        a = make_article(s).id
        stock.count_article(s, a, "stock", 3, "user")
    page = ready_client.get("/journal").text
    assert "Von Hand" in page and "Inventur" in page
    assert "Zeit" in page
