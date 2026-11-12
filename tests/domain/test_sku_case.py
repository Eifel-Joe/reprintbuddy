"""Article numbers are compared without regard to case (Spec 5.4 "Artikelnummern", p350): EX-S10-A and
ex-s10-a are the same number. A number is stored as typed; the refusal names the number that is
taken, as it is stored. Pairs left by an earlier version stay until one of them is renamed."""

import pytest

from factories import make_article, make_set
from reprintbuddy.domain import assembled, catalog
from reprintbuddy.domain.assembled import AssembledInput, OptionInput
from reprintbuddy.domain.catalog import ArticleInput, BaseInput, ChoiceInput, ChoiceOptionInput, SetInput
from reprintbuddy.domain.errors import ConflictError
from reprintbuddy.models import Article


def _taken(db, call) -> str:
    with pytest.raises(ConflictError) as info:
        with db.begin() as s:
            call(s)
    assert info.value.key in ("catalog.sku_taken", "sets.parent_sku_taken"), info.value.key
    return info.value.params["sku"]


def _rename(s, article_id, sku):
    return catalog.update_article(s, article_id, ArticleInput(name=s.get(Article, article_id).name, sku=sku))


def test_article_number_taken_by_an_article_in_other_case(db):
    with db.begin() as s:
        make_article(s, name="First", sku="EX-S10-A")
        second = make_article(s, name="Second").id
    assert _taken(db, lambda s: _rename(s, second, "ex-s10-a")) == "EX-S10-A"


def test_number_is_stored_as_typed(db):
    with db.begin() as s:
        one = make_article(s, name="One").id
        assert _rename(s, one, "ex-Mixed-1").sku == "ex-Mixed-1"


def test_set_number_taken_by_an_article_in_other_case(db):
    with db.begin() as s:
        make_article(s, name="Taken", sku="EX-SET")
        a, b = make_article(s, name="A").id, make_article(s, name="B").id
    data = SetInput("Pair", "ex-set", [BaseInput(1, a), BaseInput(1, b)], [])
    assert _taken(db, lambda s: catalog.create_set(s, data)) == "EX-SET"


def test_article_number_taken_by_a_set_variant_in_other_case(db):
    with db.begin() as s:
        a, p, q = (make_article(s, name=n).id for n in ("A", "P", "Q"))
        make_set(s, "Choice", [(1, a)], [(1, [(p, "-P"), (q, "-Q")])], sku="EX-C")
        other = make_article(s, name="Other").id
    assert _taken(db, lambda s: _rename(s, other, "ex-c-q")) == "EX-C-Q"


def test_set_variant_taken_by_an_article_in_other_case(db):
    with db.begin() as s:
        make_article(s, name="Taken", sku="ex-v-p")
        a, p, q = (make_article(s, name=n).id for n in ("A", "P", "Q"))
    data = SetInput("Choice", "EX-V", [BaseInput(1, a)], [ChoiceInput(1, [ChoiceOptionInput(p, "-P"), ChoiceOptionInput(q, "-Q")])])
    assert _taken(db, lambda s: catalog.create_set(s, data)) == "ex-v-p"


def test_set_variant_taken_by_another_sets_variant_in_other_case(db):
    with db.begin() as s:
        a, p, b, q = (make_article(s, name=n).id for n in ("A", "P", "B", "Q"))
        make_set(s, "First", [(1, a)], [(1, [(p, "-P")]), (1, [(q, "-Q")])], sku="EX-W")
        c, r = make_article(s, name="C").id, make_article(s, name="R").id
    data = SetInput("Second", "ex-w-p", [BaseInput(1, b), BaseInput(1, c)], [ChoiceInput(1, [ChoiceOptionInput(r, "-q")])])
    assert _taken(db, lambda s: catalog.create_set(s, data)) == "EX-W-P-Q"


def test_set_suffixes_that_differ_only_in_case_give_the_same_number(db):
    with db.begin() as s:
        a, p, q = (make_article(s, name=n).id for n in ("A", "P", "Q"))
    data = SetInput("Choice", "EX-D", [BaseInput(1, a)], [ChoiceInput(1, [ChoiceOptionInput(p, "-x"), ChoiceOptionInput(q, "-X")])])
    assert _taken(db, lambda s: catalog.create_set(s, data)).casefold() == "ex-d-x"


def _hose(s, suffixes, sku="EX-H"):
    seg, left, right = (make_article(s, name=n).id for n in ("Segment", "Left", "Right"))
    return AssembledInput(
        name="Hose", sku=sku, mode="stock", base=[BaseInput(2, seg)],
        fields=[[OptionInput(None, suffixes[0], [BaseInput(1, left)]), OptionInput(None, suffixes[1], [BaseInput(1, right)])]],
        parts={},
    )


def test_assembled_variant_taken_by_an_article_in_other_case(db):
    with db.begin() as s:
        make_article(s, name="Taken", sku="ex-h-l")
        data = _hose(s, ("-L", "-R"))
    assert _taken(db, lambda s: assembled.create_assembled(s, data)) == "ex-h-l"


def test_assembled_suffixes_that_differ_only_in_case_give_the_same_number(db):
    with db.begin() as s:
        data = _hose(s, ("-l", "-L"))
    assert _taken(db, lambda s: assembled.create_assembled(s, data)).casefold() == "ex-h-l"


def test_assembled_own_variant_may_change_its_case(db):
    with db.begin() as s:
        data = _hose(s, ("-L", "-R"))
        hose = assembled.create_assembled(s, data)
    with db.begin() as s:
        rows = {r.sku: r.variant_key for r in assembled.variants(s, s.get(Article, hose.id))}
        assert set(rows) == {"EX-H-L", "EX-H-R"}
        ids = [int(rows["EX-H-L"]), int(rows["EX-H-R"])]  # one field: the key is the option id
        changed = AssembledInput(
            name="Hose", sku="EX-H", mode="stock", base=data.base,
            fields=[[OptionInput(ids[0], "-l", data.fields[0][0].rows), OptionInput(ids[1], "-R", data.fields[0][1].rows)]],
            parts={},
        )
        assembled.update_assembled(s, hose.id, changed)
    with db.begin() as s:
        assert {r.sku for r in assembled.variants(s, s.get(Article, hose.id))} == {"EX-H-l", "EX-H-R"}


def test_pair_left_by_an_earlier_version_stays_until_renamed(db):
    with db.begin() as s:
        first = make_article(s, name="First", sku="EX-OLD").id
        second = make_article(s, name="Second", sku="ex-old").id  # stored before 0.4.5
    assert _taken(db, lambda s: _rename(s, second, "ex-old")) == "EX-OLD"
    with db.begin() as s:
        assert _rename(s, second, "EX-NEW").sku == "EX-NEW"
        assert _rename(s, first, "EX-OLD").sku == "EX-OLD"
