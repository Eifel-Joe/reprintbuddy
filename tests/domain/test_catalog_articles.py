import pytest
from sqlalchemy import select

from factories import make_article, make_part, make_set
from reprintbuddy.bambuddy.types import FilamentRequirement, LibraryFile, Plate, Printer
from reprintbuddy.domain import catalog
from reprintbuddy.domain.catalog import ArticleInput
from reprintbuddy.domain.errors import ConflictError, NotFoundError, ValidationError
from reprintbuddy.models import Article, ArticleFilament, ArticlePart, ProductSet

REQS = [FilamentRequirement(1, 42.5, "PLA", "#000000")]


def a_file(file_id=1, filename="Template A.gcode.3mf", model="A1", fingerprint="v1"):
    return LibraryFile(file_id, filename, None, fingerprint, model)


def create(s, *, data=None, file=None, plate=1, thumbnail=b"\x89PNG-x"):
    file = file or a_file()
    data = data or catalog.default_article_input(file)
    return catalog.create_article(
        s, file=file, plate=Plate(plate, None), requirements=REQS, thumbnail=thumbnail, data=data
    )


def test_suggest_name():
    assert catalog.suggest_name("Template A.gcode.3mf") == "Template A"
    assert catalog.suggest_name("Template B.3MF") == "Template B"
    assert catalog.suggest_name("Template C.GCODE") == "Template C"
    assert catalog.suggest_name("plain") == "plain"


def test_defaults_from_file():  # Spec 5.2
    assert catalog.default_article_input(a_file()) == ArticleInput(
        name="Template A", sku=None, units_per_print=1, min_stock=0, buffer=0,
        target_model="A1", printer_id=None, needs_assembly=False, auto_enabled=False, parts={},
    )


def test_create_article_takes_over_file_data(db):  # Spec 5.1
    with db.begin() as s:
        file = a_file(fingerprint="v1")
        article = create(s, file=file, plate=2)
        article_id = article.id
    with db.begin() as s:
        art = s.get(Article, article_id)
        assert art.file_name == "Template A.gcode.3mf"
        assert art.file_fingerprint == "v1"
        assert art.plate_id == 2 and art.library_file_id == 1
        rows = s.scalars(select(ArticleFilament).where(ArticleFilament.article_id == article_id)).all()
        assert [(r.slot, r.grams, r.material, r.color_hex) for r in rows] == [(1, 42.5, "PLA", "#000000")]
        assert art.thumbnail == b"\x89PNG-x"


def test_plate_only_once(db):  # Spec 5.1
    with db.begin() as s:
        create(s, plate=1)
    with pytest.raises(ConflictError) as info:
        with db.begin() as s:
            create(s, plate=1)
    assert info.value.key == "articles.plate_taken"
    assert info.value.params["name"] == "Template A"
    with db.begin() as s:
        create(s, plate=2, data=catalog.ArticleInput(name="Template A2"))


def test_sku_unique_across_articles_and_sets(db):
    with db.begin() as s:
        create(s, data=ArticleInput(name="Template A", sku="A-100"))
        with pytest.raises(ConflictError) as info:
            catalog.validate_sku(s, " A-100 ")
        assert info.value.key == "catalog.sku_taken"
        s.add(ProductSet(name="Starter Set", sku="S-1"))
        s.flush()
        with pytest.raises(ConflictError):
            create(s, plate=2, data=ArticleInput(name="B", sku="S-1"))
        assert catalog.validate_sku(s, "  ") is None
        assert catalog.validate_sku(s, "  ") is None


def test_sku_can_be_kept_when_editing(db):
    with db.begin() as s:
        art = create(s, data=ArticleInput(name="Template A", sku="A-100"))
        catalog.update_article(s, art.id, ArticleInput(name="Template A", sku="A-100", min_stock=2))
        assert s.get(Article, art.id).min_stock == 2


def test_sku_too_long(db):
    with db.begin() as s:
        with pytest.raises(ValidationError):
            catalog.validate_sku(s, "x" * 65)


def test_target_exclusive(db):
    with pytest.raises(ValidationError) as info:
        with db.begin() as s:
            create(s, data=ArticleInput(name="A", target_model="A1", printer_id=2))
    assert info.value.key == "articles.target_exclusive"


def test_target_may_be_missing(db):
    with db.begin() as s:
        art = create(s, data=ArticleInput(name="A", target_model=None, printer_id=None))
        assert art.target_model is None and art.printer_id is None


def test_parse_target():
    assert catalog.parse_target("model:A1") == ("A1", None)
    assert catalog.parse_target("printer:3") == (None, 3)
    assert catalog.parse_target("") == (None, None)
    for bad in ("printer:x", "model:", "bogus", "printer:0", "printer:-1"):
        with pytest.raises(ValidationError) as info:
            catalog.parse_target(bad)
        assert info.value.key == "articles.invalid_target"


def test_target_value():
    assert catalog.target_value("A1", None) == "model:A1"
    assert catalog.target_value(None, 3) == "printer:3"
    assert catalog.target_value(None, None) == ""


def test_target_options():
    printers = [Printer(1, "Printer 1", "A1"), Printer(2, "Printer 2", "A1"), Printer(3, "Printer 3", "P1S")]
    values = [o.value for o in catalog.target_options(printers, "A1")]
    assert values == ["model:A1", "model:P1S", "printer:1", "printer:2", "printer:3", ""]
    assert [o.value for o in catalog.target_options([], "A1")] == ["model:A1", ""]
    with_current = [o.value for o in catalog.target_options([], "A1", current="printer:99")]
    assert with_current == ["model:A1", "printer:99", ""]
    unknown = catalog.target_options([], None, current="printer:99")[0]
    assert unknown.printer_name is None and unknown.model is None
    assert [o.value for o in catalog.target_options(None, None, current="model:A1")] == ["model:A1"]
    assert [o.value for o in catalog.target_options(printers, "A1", current="bogus")][-1] == ""
    assert "bogus" not in [o.value for o in catalog.target_options(printers, "A1", current="bogus")]
    assert [o.value for o in catalog.target_options(None, None, current="bogus")] == [""]
    p = catalog.target_options(printers, "A1")[2]
    assert (p.model, p.printer_name) == ("A1", "Printer 1")


def test_target_options_offer_only_active_printers():
    printers = [
        Printer(1, "Printer 1", "A1"),
        Printer(2, "Printer 2", "P1S", active=False),
        Printer(3, "Printer 3", "H2D", active=False),
        Printer(4, "Printer 4", "H2D"),
    ]
    values = [o.value for o in catalog.target_options(printers, "A1")]
    assert values == ["model:A1", "model:H2D", "printer:1", "printer:4", ""]
    # the model of an inactive printer is no choice, unless the file has it
    assert "model:P1S" not in values and "printer:2" not in values
    with_file = [o.value for o in catalog.target_options(printers, "P1S")]
    assert "model:P1S" in with_file and "printer:2" not in with_file


def test_target_options_keep_printer_in_maintenance_as_known_target():
    printers = [Printer(1, "Printer 1", "A1"), Printer(2, "Printer 2", "P1S", active=False)]
    options = catalog.target_options(printers, "A1", current="printer:2")
    kept = [o for o in options if o.value == "printer:2"]
    assert len(kept) == 1
    assert (kept[0].model, kept[0].printer_name) == ("P1S", "Printer 2")
    # a stored model of an inactive printer is not turned into a printer
    assert [o.value for o in options].count("model:P1S") == 0


@pytest.mark.parametrize(
    "field,value",
    [("units_per_print", 0), ("units_per_print", 1001), ("min_stock", -1), ("buffer", -1),
     ("min_stock", 1_000_001), ("printer_id", 0)],
)
def test_invalid_numbers(db, field, value):
    data = ArticleInput(name="A", **{field: value})
    with pytest.raises(ValidationError) as info:
        with db.begin() as s:
            create(s, data=data)
    assert info.value.key == "form.invalid_number"
    assert info.value.params["field"] == f"articles.field.{field}"


def test_name_required_and_trimmed(db):
    with pytest.raises(ValidationError) as info:
        with db.begin() as s:
            create(s, data=ArticleInput(name="   "))
    assert info.value.key == "form.required"
    assert info.value.params["field"] == "articles.field.name"
    with db.begin() as s:
        art = create(s, data=ArticleInput(name="  Template A  "))
        assert art.name == "Template A"
    with pytest.raises(ValidationError):
        with db.begin() as s:
            create(s, plate=2, data=ArticleInput(name="x" * 201))


def test_update_article(db):
    with db.begin() as s:
        art = create(s)
        art_id = art.id
        catalog.update_article(s, art_id, ArticleInput(name="Template A", min_stock=4, buffer=2, auto_enabled=True))
    with db.begin() as s:
        row = catalog.list_articles(s)[0]
        assert row.target == 6 and row.min_stock == 4
        assert s.get(Article, art_id).auto_enabled is True
        assert s.get(Article, art_id).plate_id == 1  # file and plate stay


def test_update_unknown_article(db):
    with pytest.raises(NotFoundError) as info:
        with db.begin() as s:
            catalog.update_article(s, 999, ArticleInput(name="x"))
    assert info.value.key == "articles.not_found"


def test_article_parts(db):
    with db.begin() as s:
        insert, hex_key = make_part(s, name="Insert M4"), make_part(s, name="Hex key 2")
        art = create(s, data=ArticleInput(name="A", parts={insert.id: 2, hex_key.id: 1}))
        ids = (art.id, insert.id, hex_key.id)
    with db.begin() as s:
        links = {l.part_id: l.qty_per_unit for l in s.scalars(select(ArticlePart))}
        assert links == {ids[1]: 2, ids[2]: 1}
        catalog.update_article(s, ids[0], ArticleInput(name="A", parts={ids[1]: 2}))
    with db.begin() as s:
        assert {l.part_id for l in s.scalars(select(ArticlePart))} == {ids[1]}


def test_article_parts_validation(db):
    with db.begin() as s:
        insert = make_part(s)
        insert_id = insert.id
    with pytest.raises(NotFoundError) as info:
        with db.begin() as s:
            create(s, data=ArticleInput(name="A", parts={999: 1}))
    assert info.value.key == "parts.not_found"
    with pytest.raises(ValidationError):
        with db.begin() as s:
            create(s, data=ArticleInput(name="A", parts={insert_id: 0}))
    with pytest.raises(ValidationError):
        with db.begin() as s:
            create(s, data=ArticleInput(name="A", parts={insert_id: 1001}))


def test_failed_create_leaves_no_article(db):
    with pytest.raises(ValidationError):
        with db.begin() as s:
            create(s, data=ArticleInput(name="A", min_stock=-1))
    with db.begin() as s:
        assert s.scalars(select(Article)).all() == []


def test_list_available_negative(db):
    with db.begin() as s:
        make_article(s, stock=1, reserved=3)
    with db.begin() as s:
        assert catalog.list_articles(s)[0].available == -2


def test_list_sorted_by_name_ignoring_case(db):
    with db.begin() as s:
        make_article(s, name="b Template")
        make_article(s, name="A Template")
        make_article(s, name="C Template")
    with db.begin() as s:
        assert [r.name for r in catalog.list_articles(s)] == ["A Template", "b Template", "C Template"]


def test_article_row_status_and_coverage(db):
    with db.begin() as s:
        article = make_article(s, stock=1, min_stock=4, provisional=2, awaiting_assembly=1)
        row = catalog.article_row(article)
        assert row.coverage == 4 and row.status == "low"
        assert row.has_thumbnail is False and row.paused is False
    with db.begin() as s:
        listed = catalog.list_articles(s)[0]
        assert (listed.coverage, listed.status) == (4, "low")


def test_article_row_full_coverage(db):  # Spec 7.1 coverage, Spec 10.1 status
    with db.begin() as s:
        article = make_article(s, stock=0, min_stock=3, provisional=1)
        row = catalog.article_row(article, queued_units=1, in_progress_units=1, in_progress_prints=1)
        assert (row.queued_units, row.in_progress_units, row.in_progress_prints) == (1, 1, 1)
        assert row.coverage == 3 and row.status == "low"
        bare = catalog.article_row(article)
        assert bare.coverage == 1 and bare.status == "below_min"


def test_article_row_thumbnail_and_pause(db):
    with db.begin() as s:
        make_article(s, thumbnail=b"x", paused=True, stock=0, reserved=1)
    with db.begin() as s:
        row = catalog.list_articles(s)[0]
        assert row.has_thumbnail is True and row.paused is True
        assert row.status == "overbooked"


# ---- the automation switch of the list (p44) --------------------------------------------------


def test_set_auto_enabled_on_and_off(db):
    with db.begin() as s:
        a = make_article(s, target_model="A1").id
    with db.begin() as s:
        assert catalog.set_auto_enabled(s, a, True).auto_enabled is True
    with db.begin() as s:
        assert catalog.set_auto_enabled(s, a, True).auto_enabled is True  # a wanted state, no toggle
        assert catalog.set_auto_enabled(s, a, False).auto_enabled is False


def test_set_auto_enabled_on_needs_a_target(db):
    with db.begin() as s:
        a = make_article(s, target_model=None, printer_id=None).id
    with db.begin() as s:
        with pytest.raises(ConflictError) as err:
            catalog.set_auto_enabled(s, a, True)
        assert err.value.key == "articles.auto_needs_target"
        assert s.get(Article, a).auto_enabled is False
    with db.begin() as s:
        s.get(Article, a).printer_id = 2
    with db.begin() as s:
        assert catalog.set_auto_enabled(s, a, True).auto_enabled is True


def test_set_auto_enabled_off_needs_no_target(db):
    with db.begin() as s:
        a = make_article(s, target_model=None, printer_id=None, auto_enabled=True).id
    with db.begin() as s:
        assert catalog.set_auto_enabled(s, a, False).auto_enabled is False


def test_set_auto_enabled_refuses_archived_and_missing(db):
    from datetime import datetime, timezone

    with db.begin() as s:
        a = make_article(s, target_model="A1", archived_at=datetime.now(timezone.utc)).id
    with db.begin() as s:
        for wanted in (True, False):
            with pytest.raises(ConflictError) as err:
                catalog.set_auto_enabled(s, a, wanted)
            assert err.value.key == "articles.auto_archived"
        with pytest.raises(NotFoundError):
            catalog.set_auto_enabled(s, 999, True)


def test_list_row_carries_the_automation_state(db):
    with db.begin() as s:
        make_article(s, name="Template A", target_model="A1", auto_enabled=True)
        make_article(s, name="Template B", target_model="A1")
    with db.begin() as s:
        rows = {r.name: r.auto_enabled for r in catalog.list_articles(s)}
    assert rows == {"Template A": True, "Template B": False}
