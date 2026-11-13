"""Sets with base parts and choice fields whose articles make variants (Spec 5.4, p340)."""

import pytest
from sqlalchemy import select

from factories import make_article, make_set
from reprintbuddy.domain import catalog
from reprintbuddy.domain.catalog import (
    ArticleInput,
    BaseInput,
    ChoiceInput,
    ChoiceOptionInput,
    SetInput,
)
from reprintbuddy.domain.errors import ConflictError, NotFoundError, ValidationError
from reprintbuddy.models import SetPosition, SetPositionOption


def articles(s, **stocks) -> dict[str, int]:
    """Articles named after the keys, with that stock: articles(s, frame=3) -> {"frame": id}."""
    return {name: make_article(s, name=name.capitalize(), stock=stock).id for name, stock in stocks.items()}


def choice(qty, *options):
    return ChoiceInput(qty, [ChoiceOptionInput(article_id, suffix) for article_id, suffix in options])


def refused(db, data, key, cls=ValidationError, set_id=None):
    with pytest.raises(cls) as info:
        with db.begin() as s:
            if set_id is None:
                catalog.create_set(s, data)
            else:
                catalog.update_set(s, set_id, data)
    assert info.value.key == key, data
    return info.value


def test_set_without_choices_is_the_minimum_over_its_articles(db):  # Spec 5.4
    with db.begin() as s:
        a = articles(s, frame=3, screw=5)
        catalog.create_set(s, SetInput("Starter Set", "EX-STARTER-SET", [BaseInput(1, a["frame"]), BaseInput(2, a["screw"])], []))
    with db.begin() as s:
        row = catalog.list_sets(s)[0]
        assert (row.available, row.variants, row.incomplete, row.orderable) == (2, [], None, True)
        assert [(b.qty, b.name) for b in row.base] == [(1, "Frame"), (2, "Screw")]


def test_set_with_two_choice_fields_has_six_variants_with_composed_numbers(db):  # Spec 5.4 example
    with db.begin() as s:
        a = articles(s, frame=9, a=1, b=1, c=1, d=1, e=1)
        catalog.create_set(s, SetInput("Kit", "EX-S10", [BaseInput(1, a["frame"])], [
            choice(1, (a["a"], "-A"), (a["b"], "-B"), (a["c"], "-C")),
            choice(1, (a["d"], "-1"), (a["e"], "-2")),
        ]))
    with db.begin() as s:
        row = catalog.list_sets(s)[0]
        assert [v.sku for v in row.variants] == [
            "EX-S10-A-1", "EX-S10-A-2", "EX-S10-B-1", "EX-S10-B-2", "EX-S10-C-1", "EX-S10-C-2",
        ]
        assert row.variants[0].key == (a["a"], a["d"]) and row.variants[0].names == ["A", "D"]
        assert [o.suffix for o in row.choices[0].options] == ["-A", "-B", "-C"]


def test_variants_without_a_second_field(db):
    with db.begin() as s:
        a = articles(s, frame=9, a=1, b=1, c=1)
        catalog.create_set(s, SetInput("Kit", "EX-S10", [BaseInput(1, a["frame"])], [
            choice(1, (a["a"], "-A"), (a["b"], "-B"), (a["c"], "-C")),
        ]))
    with db.begin() as s:
        assert [v.sku for v in catalog.list_sets(s)[0].variants] == ["EX-S10-A", "EX-S10-B", "EX-S10-C"]


def test_variant_availability_and_together(db):  # Spec 5.4, second example, with reservations
    with db.begin() as s:
        a = articles(s, frame=4, a=2, b=0, c=1)
        catalog.create_set(s, SetInput("Kit", "EX-S10", [BaseInput(1, a["frame"])], [
            choice(1, (a["a"], "-A"), (a["b"], "-B"), (a["c"], "-C")),
        ]))
        s.get(catalog.Article, a["frame"]).reserved = 1  # 3 available
    with db.begin() as s:
        row = catalog.list_sets(s)[0]
        assert [(v.sku, v.available) for v in row.variants] == [("EX-S10-A", 2), ("EX-S10-B", 0), ("EX-S10-C", 1)]
        assert (row.available, row.status) == (3, "deliverable")


def test_choice_quantity_counts_for_the_chosen_article(db):
    with db.begin() as s:
        a = articles(s, frame=9, a=5, b=1)
        catalog.create_set(s, SetInput("Kit", "EX-S10", [BaseInput(1, a["frame"])], [
            choice(2, (a["a"], "-A"), (a["b"], "-B")),
        ]))
    with db.begin() as s:
        row = catalog.list_sets(s)[0]
        assert [v.available for v in row.variants] == [2, 0] and row.available == 2
        assert row.choices[0].qty == 2


def test_choice_fields_need_a_set_number(db):
    with db.begin() as s:
        a = articles(s, frame=1, a=1)
    refused(db, SetInput("Kit", None, [BaseInput(1, a["frame"])], [choice(1, (a["a"], "-A"))]), "form.required")


def test_every_choice_needs_a_suffix(db):
    with db.begin() as s:
        a = articles(s, frame=1, a=1, b=1)
    error = refused(db, SetInput("Kit", "EX-S10", [BaseInput(1, a["frame"])],
                                 [choice(1, (a["a"], "-A"), (a["b"], "  "))]), "sets.suffix_required")
    assert error.params["name"] == "B"


def test_suffixes_differ_within_a_field(db):
    with db.begin() as s:
        a = articles(s, frame=1, a=1, b=1)
    error = refused(db, SetInput("Kit", "EX-S10", [BaseInput(1, a["frame"])],
                                 [choice(1, (a["a"], "-A"), (a["b"], " -A"))]), "sets.suffix_twice")
    assert error.params["suffix"] == "-A"


def test_an_article_is_only_once_in_a_set(db):  # decision p340, question 5
    with db.begin() as s:
        a = articles(s, frame=1, a=1, b=1)
    cases = [
        SetInput("Kit", "EX-S10", [BaseInput(1, a["frame"]), BaseInput(2, a["frame"])], []),
        SetInput("Kit", "EX-S10", [BaseInput(1, a["frame"])], [choice(1, (a["frame"], "-F"), (a["a"], "-A"))]),
        SetInput("Kit", "EX-S10", [BaseInput(1, a["frame"])], [choice(1, (a["a"], "-A")), choice(1, (a["a"], "-1"))]),
        SetInput("Kit", "EX-S10", [], [choice(1, (a["a"], "-A"), (a["a"], "-B")), choice(1, (a["b"], "-1"))]),
    ]
    for data in cases:
        error = refused(db, data, "sets.article_twice")
        assert error.params["name"] in ("Frame", "A")


def test_every_variant_has_two_different_articles(db):  # decision p340, question 3
    with db.begin() as s:
        a = articles(s, frame=1, a=1, b=1)
    refused(db, SetInput("Kit", "EX-KIT", [BaseInput(2, a["frame"])], []), "sets.too_few_articles")
    refused(db, SetInput("Kit", "EX-S10", [], [choice(1, (a["a"], "-A"), (a["b"], "-B"))]), "sets.too_few_articles")
    refused(db, SetInput("Kit", "EX-KIT", [], []), "sets.too_few_articles")
    with db.begin() as s:
        catalog.create_set(s, SetInput("One base and a choice", "EX-S11", [BaseInput(1, a["frame"])],
                                       [choice(1, (a["a"], "-A"))]))
        catalog.create_set(s, SetInput("Two fields", "EX-S12", [],
                                       [choice(1, (a["a"], "-A")), choice(1, (a["b"], "-1"))]))
    with db.begin() as s:
        assert len(catalog.list_sets(s)) == 2


def test_second_field_needs_the_first(db):
    with db.begin() as s:
        a = articles(s, frame=1, a=1)
    refused(db, SetInput("Kit", "EX-S10", [BaseInput(1, a["frame"])], [choice(1), choice(1, (a["a"], "-1"))]),
            "sets.choices2_without_1")
    with db.begin() as s:  # an empty last field is no field
        catalog.create_set(s, SetInput("Kit", "EX-S10", [BaseInput(1, a["frame"])], [choice(1, (a["a"], "-A")), choice(1)]))
        assert len(catalog.list_sets(s)[0].choices) == 1


def test_variant_number_longer_than_64_is_refused(db):
    with db.begin() as s:
        a = articles(s, frame=1, a=1)
    error = refused(db, SetInput("Kit", "S" * 60, [BaseInput(1, a["frame"])], [choice(1, (a["a"], "-ABCDE"))]),
                    "sets.variant_too_long")
    assert error.params["sku"] == "S" * 60 + "-ABCDE"


def test_variant_number_must_not_exist_already(db):  # decision p340, question 6
    with db.begin() as s:
        a = articles(s, frame=1, a=1, b=1)
        make_article(s, name="Loose", sku="EX-S10-A")
        catalog.create_set(s, SetInput("Other", "EX-S20", [BaseInput(1, a["frame"])], [choice(1, (a["b"], "-B"))]))
    error = refused(db, SetInput("Kit", "EX-S10", [BaseInput(1, a["frame"])], [choice(1, (a["a"], "-A"))]),
                    "catalog.sku_taken", ConflictError)
    assert error.params["sku"] == "EX-S10-A"
    refused(db, SetInput("Kit", "EX-S2", [BaseInput(1, a["frame"])], [choice(1, (a["a"], "0-B"))]),
            "catalog.sku_taken", ConflictError)  # EX-S2 + 0-B is the variant EX-S20-B of "Other"
    refused(db, SetInput("Kit", "EX-S20-B", [BaseInput(1, a["frame"]), BaseInput(1, a["a"])], []),
            "catalog.sku_taken", ConflictError)  # a set number that is a variant of another set


def test_two_variants_of_one_set_must_differ(db):
    with db.begin() as s:
        a = articles(s, frame=1, a=1, b=1, c=1, d=1)
    refused(db, SetInput("Kit", "K", [BaseInput(1, a["frame"])], [
        choice(1, (a["a"], "-A"), (a["b"], "-A-")), choice(1, (a["c"], "-1"), (a["d"], "1"))
    ]), "catalog.sku_taken", ConflictError)  # K-A-1 twice


def test_article_cannot_take_a_variant_number(db):  # review focus 3
    with db.begin() as s:
        a = articles(s, frame=1, a=1)
        catalog.create_set(s, SetInput("Kit", "EX-S10", [BaseInput(1, a["frame"])], [choice(1, (a["a"], "-A"))]))
    with pytest.raises(ConflictError):
        with db.begin() as s:
            catalog.update_article(s, a["frame"], ArticleInput(name="Frame", sku="EX-S10-A"))


def test_set_cannot_take_a_variant_number(db):
    with db.begin() as s:
        a = articles(s, frame=1, a=1, b=1)
        catalog.create_set(s, SetInput("Kit", "EX-S10", [BaseInput(1, a["frame"])], [choice(1, (a["a"], "-A"))]))
    refused(db, SetInput("Other", "EX-S10-A", [BaseInput(1, a["frame"]), BaseInput(1, a["b"])], []),
            "catalog.sku_taken", ConflictError)


def test_numbers_of_archived_choices_and_archived_sets_stay_taken(db):  # condition 2 of the plan approval
    with db.begin() as s:
        a = articles(s, frame=1, a=1, b=1)
        catalog.create_set(s, SetInput("Kit", "EX-S10", [BaseInput(1, a["frame"])],
                                       [choice(1, (a["a"], "-A"), (a["b"], "-B"))]))
        old = catalog.create_set(s, SetInput("Old", "EX-S30", [BaseInput(1, a["frame"])], [choice(1, (a["a"], "-A"))]))
        s.get(catalog.Article, a["b"]).archived_at = catalog.utcnow()
        catalog.archive_set(s, old.id)
    with db.begin() as s:
        assert catalog.variant_numbers(s) == {"EX-S10-A": 1, "EX-S10-B": 1, "EX-S30-A": old.id}
    with pytest.raises(ConflictError):
        with db.begin() as s:
            catalog.update_article(s, a["frame"], ArticleInput(name="Frame", sku="EX-S10-B"))
    with pytest.raises(ConflictError):
        with db.begin() as s:
            catalog.update_article(s, a["frame"], ArticleInput(name="Frame", sku="EX-S30-A"))


def test_archived_choice_article_keeps_its_variants(db):  # Spec 5.4 Altfall (0.4.3; until 0.4.2 they dropped)
    with db.begin() as s:
        a = articles(s, frame=5, a=2, b=3)
        set_id = catalog.create_set(s, SetInput("Kit", "EX-S10", [BaseInput(1, a["frame"])],
                                                [choice(1, (a["a"], "-A"), (a["b"], "-B"))])).id
        s.get(catalog.Article, a["b"]).archived_at = catalog.utcnow()
    with db.begin() as s:
        row = catalog.list_sets(s)[0]
        assert [v.sku for v in row.variants] == ["EX-S10-A", "EX-S10-B"]
        assert (row.available, row.orderable) == (5, True)
        assert [(o.name, o.archived) for o in row.choices[0].options] == [("A", False), ("B", True)]
    with db.begin() as s:  # saving the set again keeps the archived article (Spec 5.5)
        catalog.update_set(s, set_id, SetInput("Kit", "EX-S10", [BaseInput(1, a["frame"])],
                                               [choice(1, (a["a"], "-A"), (a["b"], "-B"))]))


def test_an_archived_article_cannot_come_into_a_set(db):
    with db.begin() as s:
        a = articles(s, frame=5, a=2)
        s.get(catalog.Article, a["a"]).archived_at = catalog.utcnow()
    refused(db, SetInput("Kit", "EX-S10", [BaseInput(1, a["frame"])], [choice(1, (a["a"], "-A"))]),
            "sets.article_archived")


def test_archived_base_part_keeps_the_set_orderable(db):  # Spec 5.4 Altfall (0.4.3; until 0.4.2 not orderable)
    with db.begin() as s:
        a = articles(s, frame=5, screw=5)
        catalog.create_set(s, SetInput("Kit", "EX-KIT", [BaseInput(1, a["frame"]), BaseInput(1, a["screw"])], []))
        s.get(catalog.Article, a["screw"]).archived_at = catalog.utcnow()
    with db.begin() as s:
        row = catalog.list_sets(s)[0]
        assert (row.available, row.orderable, row.status) == (5, True, "deliverable")


def test_migrated_set_without_suffixes_is_incomplete_and_not_orderable(db):  # review focus 5
    with db.begin() as s:
        a = articles(s, frame=5, a=2, b=3)
        set_id = make_set(s, "Kit", [(1, a["frame"])], [(1, [(a["a"], None), (a["b"], None)])]).id
    with db.begin() as s:
        row = catalog.list_sets(s)[0]
        assert (row.incomplete, row.orderable, row.available) == ("suffixes_missing", False, 5)
        assert [v.sku for v in row.variants] == [None, None]
    refused(db, SetInput("Kit", "EX-S10", [BaseInput(1, a["frame"])], [choice(1, (a["a"], "-A"), (a["b"], ""))]),
            "sets.suffix_required", set_id=set_id)
    with db.begin() as s:
        catalog.update_set(s, set_id, SetInput("Kit", "EX-S10", [BaseInput(1, a["frame"])],
                                               [choice(1, (a["a"], "-A"), (a["b"], "-B"))]))
    with db.begin() as s:
        row = catalog.list_sets(s)[0]
        assert (row.incomplete, row.orderable) == (None, True)


def test_migrated_set_with_too_few_articles_is_incomplete(db):
    with db.begin() as s:
        a = articles(s, frame=5)
        make_set(s, "Kit", [(2, a["frame"])])
    with db.begin() as s:
        assert catalog.list_sets(s)[0].incomplete == "too_few"
        assert not catalog.list_sets(s)[0].orderable


def test_set_validation(db):
    with db.begin() as s:
        a = articles(s, frame=1, screw=1)
    two = [BaseInput(1, a["frame"]), BaseInput(1, a["screw"])]
    cases = [
        (SetInput("S", "EX-S", [BaseInput(0, a["frame"]), two[1]], []), "form.invalid_number"),
        (SetInput("S", "EX-S", [BaseInput(1001, a["frame"]), two[1]], []), "form.invalid_number"),
        (SetInput("S", "K", two[:1], [choice(0, (a["screw"], "-S"))]), "form.invalid_number"),
        (SetInput("  ", "EX-", two, []), "form.required"),
        (SetInput("S", "EX-S", [BaseInput(1, 999), two[1]], []), "articles.not_found"),
        (SetInput("S", "K", two[:1], [choice(1, (999, "-X"))]), "articles.not_found"),
    ]
    for data, key in cases:
        refused(db, data, key, (ValidationError, NotFoundError))
    with db.begin() as s:
        assert catalog.list_sets(s) == []


def test_update_replaces_base_parts_and_choices(db):
    with db.begin() as s:
        a = articles(s, frame=2, screw=9, a=4)
        set_id = catalog.create_set(s, SetInput("Starter Set", "S-1", [BaseInput(1, a["frame"])],
                                                [choice(1, (a["a"], "-A"))])).id
    with db.begin() as s:
        catalog.update_set(s, set_id, SetInput("Starter Set 2", "S-1",
                                               [BaseInput(3, a["screw"]), BaseInput(1, a["frame"])], []))
    with db.begin() as s:
        row = catalog.list_sets(s)[0]
        assert (row.name, row.available, row.choices) == ("Starter Set 2", 2, [])
        assert [b.article_id for b in row.base] == [a["screw"], a["frame"]]  # the order of the form
        assert len(s.scalars(select(SetPosition)).all()) == 2
        assert len(s.scalars(select(SetPositionOption)).all()) == 2


def test_get_set_unknown(db):
    with pytest.raises(NotFoundError) as info:
        with db.begin() as s:
            catalog.get_set(s, 999)
    assert info.value.key == "sets.not_found"


def test_set_sku_conflicts_with_article(db):
    with db.begin() as s:
        a = articles(s, frame=1, screw=1)
        make_article(s, name="Loose", sku="A-100")
    refused(db, SetInput("S", " A-100 ", [BaseInput(1, a["frame"]), BaseInput(1, a["screw"])], []),
            "catalog.sku_taken", ConflictError)


def test_set_keeps_own_numbers_on_update(db):
    with db.begin() as s:
        a = articles(s, frame=1, a=1)
        set_id = catalog.create_set(s, SetInput("S", "S-1", [BaseInput(1, a["frame"])], [choice(1, (a["a"], "-A"))])).id
    with db.begin() as s:
        catalog.update_set(s, set_id, SetInput("S2", "S-1", [BaseInput(2, a["frame"])], [choice(1, (a["a"], "-A"))]))


def test_article_sku_conflicts_with_set(db):
    with db.begin() as s:
        a = articles(s, frame=1, screw=1)
        catalog.create_set(s, SetInput("S", "X-1", [BaseInput(1, a["frame"]), BaseInput(1, a["screw"])], []))
    with pytest.raises(ConflictError):
        with db.begin() as s:
            catalog.update_article(s, a["frame"], ArticleInput(name="Frame", sku="X-1"))


def test_set_row_lookup(db):
    with db.begin() as s:
        a = articles(s, frame=1, a=1)
        set_id = catalog.create_set(s, SetInput("S", "S-1", [BaseInput(1, a["frame"])], [choice(1, (a["a"], "-A"))])).id
    with db.begin() as s:
        row = catalog.set_row(s, set_id)
        assert row.id == set_id and row.variants[0].sku == "S-1-A"


def test_set_can_turn_its_old_number_into_a_variant_number(db):  # review of p340 (I1)
    """After saving nobody holds EX-S10-A but the variant: the set's own old number is no conflict."""
    with db.begin() as s:
        a = articles(s, frame=1, a=1, b=1)
        set_id = catalog.create_set(s, SetInput("Kit", "EX-S10-A", [BaseInput(1, a["frame"]), BaseInput(1, a["a"])], [])).id
    with db.begin() as s:
        catalog.update_set(s, set_id, SetInput("Kit", "EX-S10", [BaseInput(1, a["frame"])],
                                               [choice(1, (a["a"], "-A"), (a["b"], "-B"))]))
    with db.begin() as s:
        assert [v.sku for v in catalog.list_sets(s)[0].variants] == ["EX-S10-A", "EX-S10-B"]
