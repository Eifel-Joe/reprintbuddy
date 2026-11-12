"""The order page lists the articles of inner sets indented under them (Spec 6.7, 0.4.3 p423); the
list of orders shows only the head."""

from datetime import date

from conftest import sm_of
from factories import make_article, make_set
from reprintbuddy.domain import orders


def inner(set_id, *key):
    return ("set", set_id, tuple(key))


def ordered(client, deep=False):
    with sm_of(client).begin() as s:
        a = {name: make_article(s, name=name, stock=9).id
             for name in ("Holder", "Manual", "Small", "Large", "Plate", "Screw set", "Lid")}
        basic = make_set(s, "Basic kit", [(2, a["Holder"]), (1, a["Manual"])],
                         [(1, [(a["Small"], "-S"), (a["Large"], "-L")])], sku="EX-GA").id
        extension = make_set(s, "Extension", [(1, a["Plate"]), (1, a["Screw set"])]).id
        top = make_set(s, "Complete kit", [(1, inner(basic, a["Small"])), (1, inner(extension))], sku="EX-KP").id
        if deep:
            top = make_set(s, "Wrapper", [(1, a["Lid"]), (1, inner(top))]).id
        return orders.create_order(s, reference="Order 3001", order_date=date(2026, 11, 12),
                                   items=[orders.SetItem(top, 2)], actor="user").id


def test_detail_shows_every_article_under_its_inner_set(ready_client):  # Spec 6.7
    page = ready_client.get(f"/orders/{ordered(ready_client)}").text
    order = [
        "<strong>Complete kit × 2</strong>",
        'class="order-inner depth-1"', "Basic kit EX-GA-S × 2", "<li>4 × Holder</li>", "<li>2 × Manual</li>", "<li>2 × Small</li>",
        'class="order-inner depth-1"', "Extension × 2", "<li>2 × Plate</li>", "<li>2 × Screw set</li>",
    ]
    at = 0
    for piece in order:
        found = page.find(piece, at)
        assert found >= 0, piece
        at = found + 1


def test_detail_indents_deeper_levels(ready_client):
    page = ready_client.get(f"/orders/{ordered(ready_client, deep=True)}").text
    assert page.index("<li>2 × Lid</li>") < page.index("Complete kit × 2")  # direct articles first
    assert 'class="order-inner depth-1"' in page and 'class="order-inner depth-2"' in page
    assert page.index("Complete kit × 2") < page.index("Basic kit EX-GA-S × 2")


def test_list_shows_only_the_head(ready_client):  # Spec 6.7: the list shows only the head
    ordered(ready_client)
    page = ready_client.get("/orders").text
    assert "Complete kit × 2" in page
    assert "Holder" not in page and "Basic kit" not in page


def test_old_order_detail_unchanged(ready_client):
    with sm_of(ready_client).begin() as s:
        plate = make_article(s, name="Plate", stock=1).id
        screws = make_article(s, name="Screw set", stock=1).id
        kit = make_set(s, "Extension", [(1, plate), (1, screws)]).id
        order_id = orders.create_order(s, reference="Order 3002", order_date=date(2026, 11, 12),
                                       items=[orders.SetItem(kit, 1)], actor="user").id
    page = ready_client.get(f"/orders/{order_id}").text
    assert '<ul class="plain-list"><li>1 × Plate</li><li>1 × Screw set</li></ul>' in page
    assert "order-inner" not in page
