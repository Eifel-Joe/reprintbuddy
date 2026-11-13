"""p533 (Spec 5.2 "Artikelnummer ist Pflicht", 5.4, 5.6): no article, set or article made of parts is
saved without an article number. Only a part ("Sold alone: no") may stay without one; it needs one
when it is switched to "yes". What is stored without a number from earlier versions keeps working and
is asked for the number at the next save."""

import pytest
from factories import make_article
from reprintbuddy.bambuddy.types import FilamentRequirement, LibraryFile, Plate
from reprintbuddy.domain import assembled, catalog
from reprintbuddy.domain.assembled import AssembledInput
from reprintbuddy.domain.catalog import ArticleInput, BaseInput, SetInput
from reprintbuddy.domain.errors import ValidationError
from reprintbuddy.models import Article

REQS = [FilamentRequirement(1, 42.5, "PLA", "#000000")]
FILE = LibraryFile(1, "Template A.gcode.3mf", None, "v1", "A1")


def create(s, **fields):
    data = ArticleInput(name=fields.pop("name", "Template A"), **fields)
    return catalog.create_article(s, file=FILE, plate=Plate(1, None), requirements=REQS, thumbnail=None, data=data)


def missing(info):
    """The refusal names the missing number (`form.required` with the field "Article number")."""
    assert info.value.key == "form.required"
    assert info.value.params["field"] == "catalog.field.sku"


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_article_without_number_is_refused(db, blank):
    with pytest.raises(ValidationError) as info:
        with db.begin() as s:
            create(s, sku=blank)
    missing(info)


def test_article_with_number_is_saved(db):
    with db.begin() as s:
        assert create(s, sku="EX-1").sku == "EX-1"


def test_part_only_may_stay_without_number(db):
    with db.begin() as s:
        article = create(s, sku=None, sold_alone=False)
        article_id = article.id
    with db.begin() as s:
        assert s.get(Article, article_id).sku is None


def test_part_only_switched_to_sold_alone_needs_a_number(db):
    with db.begin() as s:
        article_id = create(s, sku=None, sold_alone=False).id
    with pytest.raises(ValidationError) as info:
        with db.begin() as s:
            catalog.update_article(s, article_id, ArticleInput(name="Template A", sku=None, sold_alone=True))
    missing(info)
    with db.begin() as s:
        catalog.update_article(s, article_id, ArticleInput(name="Template A", sku="EX-2", sold_alone=True))


def test_editing_an_old_article_without_number_asks_for_it(db):
    with db.begin() as s:
        article_id = make_article(s, name="Old", sku=None, sold_alone=True).id  # stored by an earlier version
    with pytest.raises(ValidationError) as info:
        with db.begin() as s:
            catalog.update_article(s, article_id, ArticleInput(name="Old renamed", sku=None))
    missing(info)
    with db.begin() as s:  # until then it works as before: target, listing and so on do not ask
        catalog.set_target_quantity(s, article_id, 3)
        assert s.get(Article, article_id).min_stock == 3
    with db.begin() as s:
        catalog.update_article(s, article_id, ArticleInput(name="Old renamed", sku="EX-3"))


def two_articles(db):
    with db.begin() as s:
        return make_article(s, name="Frame", sku="EX-F").id, make_article(s, name="Pin", sku="EX-P").id


@pytest.mark.parametrize("blank", [None, "", "  "])
def test_set_without_number_is_refused_also_without_choice_parts(db, blank):
    frame, pin = two_articles(db)
    with pytest.raises(ValidationError) as info:
        with db.begin() as s:
            catalog.create_set(s, SetInput("Kit", blank, [BaseInput(1, frame), BaseInput(1, pin)], []))
    missing(info)


def test_set_with_number_is_saved_and_edit_asks_for_it(db):
    frame, pin = two_articles(db)
    with db.begin() as s:
        set_id = catalog.create_set(s, SetInput("Kit", "EX-K", [BaseInput(1, frame), BaseInput(1, pin)], [])).id
    with pytest.raises(ValidationError) as info:
        with db.begin() as s:
            catalog.update_set(s, set_id, SetInput("Kit", None, [BaseInput(1, frame), BaseInput(1, pin)], []))
    missing(info)


def test_old_set_without_number_is_asked_at_the_next_save(db):
    frame, pin = two_articles(db)
    with db.begin() as s:
        product_set = catalog.create_set(s, SetInput("Old kit", "EX-O", [BaseInput(1, frame), BaseInput(1, pin)], []))
        product_set.sku = None  # as stored by an earlier version
        set_id = product_set.id
    with db.begin() as s:  # it still lists and reads as before
        assert [row.name for row in catalog.list_sets(s)] == ["Old kit"]
    with pytest.raises(ValidationError) as info:
        with db.begin() as s:
            catalog.update_set(s, set_id, SetInput("Old kit", None, [BaseInput(1, frame), BaseInput(1, pin)], []))
    missing(info)
    with db.begin() as s:
        catalog.update_set(s, set_id, SetInput("Old kit", "EX-O", [BaseInput(1, frame), BaseInput(1, pin)], []))


def assembled_input(frame, **changes):
    data = AssembledInput(name="Box", sku="EX-B", mode="order", base=[BaseInput(2, frame)], fields=[], parts={})
    for key, value in changes.items():
        setattr(data, key, value)
    return data


@pytest.mark.parametrize("blank", [None, "", "  "])
def test_assembled_article_without_number_is_refused_also_without_choice_parts(db, blank):
    frame, _pin = two_articles(db)
    with pytest.raises(ValidationError) as info:
        with db.begin() as s:
            assembled.create_assembled(s, assembled_input(frame, sku=blank))
    missing(info)


def test_assembled_article_edit_asks_for_the_number(db):
    frame, _pin = two_articles(db)
    with db.begin() as s:
        box_id = assembled.create_assembled(s, assembled_input(frame)).id
    with pytest.raises(ValidationError) as info:
        with db.begin() as s:
            assembled.update_assembled(s, box_id, assembled_input(frame, sku=None))
    missing(info)


def test_converting_a_print_article_to_assembled_needs_a_number(db):
    """Sister path: a print article made into an article of parts is saved by the same check."""
    frame, pin = two_articles(db)
    with db.begin() as s:
        article_id = make_article(s, name="Was print", sku="EX-W", library_file_id=2).id
    with pytest.raises(ValidationError) as info:
        with db.begin() as s:
            assembled.convert_to_assembled(s, article_id, assembled_input(frame, sku=None))
    missing(info)
