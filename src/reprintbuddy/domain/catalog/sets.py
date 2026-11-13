"""Sets of the catalog: base parts, choice fields and variants."""

from __future__ import annotations

import itertools
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy.domain import articlesets, inputs, rules, setresolve
from reprintbuddy.domain.errors import ConflictError, NotFoundError, ValidationError
from reprintbuddy.domain.status import SetStatus, set_status
from reprintbuddy.models import (
    Article,
    ArticleOption,
    ProductSet,
    SetPosition,
    SetPositionOption,
    SetPositionSet,
)
from reprintbuddy.domain.catalog.articles import (
    MAX_SKU,
    _archive_filter,
    _delete_rows,
    _required_name,
    same_number,
    validate_sku,
)


# ---- sets ----------------------------------------------------------------------------------
# A set has base parts and up to two choice fields (Spec 5.4, p340). A base part is a position in
# slot 0 with one article; a choice field is one position in slot 1 or 2 whose options are the
# articles to choose from, each with a suffix. A variant is one article of each field with all base
# parts; its number is the set number with the suffixes behind it, written as they are. Sets only
# point at articles and count with their available number, nothing about how an article is built.

CHOICE_FIELDS = 2


@dataclass
class BaseInput:
    """A base row: an article, or an inner set in its fixed variant (0.4.3 p423; `key` holds the
    chosen article per choice field of the inner set, empty without choices)."""

    qty: int
    article_id: int | None
    set_id: int | None = None
    key: tuple[int, ...] = ()


@dataclass
class ChoiceOptionInput:
    article_id: int
    suffix: str | None


@dataclass
class ChoiceInput:
    qty: int
    options: list[ChoiceOptionInput]


@dataclass
class SetInput:
    name: str
    sku: str | None
    base: list[BaseInput]
    choices: list[ChoiceInput]  # choice field 1 and 2; an empty last field is no field


@dataclass(frozen=True)
class BaseRow:
    qty: int
    article_id: int | None  # None for an inner set
    name: str  # of the article or of the inner set
    archived: bool  # the article or the inner set itself is archived
    set_id: int | None = None  # an inner set (0.4.3 p423); it brings its choice parts (0.4.4 p441)
    sku: str | None = None  # the number of the inner set (Spec 10.1)
    contents: tuple[tuple[int, str], ...] = ()  # inner set: (qty per piece of it, base article), all levels


@dataclass(frozen=True)
class ChoiceOptionRow:
    article_id: int
    name: str
    suffix: str | None
    archived: bool


@dataclass(frozen=True)
class ChoiceRow:
    qty: int
    options: list[ChoiceOptionRow]


@dataclass(frozen=True)
class VariantRow:
    key: tuple[int, ...]  # the chosen article per field
    sku: str | None  # None while the set number or a suffix is missing
    names: list[str]  # the chosen articles
    available: int


@dataclass(frozen=True)
class SetRow:
    id: int
    name: str
    sku: str | None
    available: int  # without choices: how often the set comes together; with them "together at most"
    base: list[BaseRow]
    choices: list[ChoiceRow]  # the set's own choice fields 1 and 2, as the form edits them
    variants: list[VariantRow]  # over every field, the brought ones first (Spec 5.4, 0.4.4)
    # "too_few" or "suffixes_missing" (sets the update to v0.4.0 rebuilt), "broken" (0.4.3),
    # "number_missing" (brings choice parts without a number of its own: the update to 0.4.4)
    incomplete: str | None
    orderable: bool
    status: SetStatus  # deliverable when at least one set comes together (Spec 10.1)
    # what an earlier version left archived directly in this set: ("article" or "set", name), in the order
    # of the form (Spec 5.4 Altfall, 0.4.3): the list and the overview warn about it
    held_archived: tuple[tuple[str, str], ...] = ()


def _numbers_of(defs: dict[int, setresolve.SetDef], set_id: int) -> list[str]:
    """The variant numbers of a set over every field, brought ones first (Spec 5.4, 0.4.4); none
    without a number, a suffix, or for a set that cannot be resolved."""
    if not defs[set_id].sku:
        return []
    try:
        slots = setresolve.fields(defs, set_id)
    except setresolve.ResolveError:
        return []
    if not slots:
        return []
    numbers = []
    for suffixes in itertools.product(*(slot.suffixes for slot in slots)):
        number = rules.variant_number(defs[set_id].sku, suffixes)
        if number is not None:
            numbers.append(number)
    return numbers


def variant_numbers(
    session: Session, *, exclude_set_id: int | None = None, defs: dict[int, setresolve.SetDef] | None = None
) -> dict[str, int]:
    """Every variant number -> its set, brought choice parts included (0.4.4). Archived sets and
    archived choice articles count too, so that restoring one never brings back a number that is
    taken meanwhile (plan approval p340)."""
    defs = load_defs(session) if defs is None else defs
    numbers: dict[str, int] = {}
    for set_id in sorted(defs):
        if set_id != exclude_set_id:
            numbers.update((number, set_id) for number in _numbers_of(defs, set_id))
    return numbers


def _allowed_in_set(session: Session, article: Article) -> bool:
    """Only articles sold alone; an article made of parts only built on stock and without choices: it
    then counts like a print article with its stock (Spec 5.4, 0.4.0 p341)."""
    if article.kind == articlesets.PRINT:
        return article.sold_alone
    if article.kind == articlesets.ASSEMBLED and article.assembly_mode == "stock":
        return session.scalar(select(ArticleOption.id).where(ArticleOption.article_id == article.id).limit(1)) is None
    return False


BaseSpec = tuple[int, int | None, int | None]  # qty, article id, inner set id
MAX_VARIANT_FIELDS = MAX_SKU - 1  # a number and each suffix have one character at least (Spec 5.4: 64)
MAX_VARIANTS = 100  # variants of one set or one article made of parts (Spec 5.4, 5.6; p450)


def _check_set_input(
    session: Session, data: SetInput, *, set_id: int | None = None
) -> tuple[str, str | None, list[BaseSpec], list[tuple[int, list[tuple[int, str | None]]]]]:
    name = _required_name(data.name, "sets.field.name")
    taken = variant_numbers(session, exclude_set_id=set_id)  # looked up once per save
    sku = validate_sku(session, data.sku, set_id=set_id, variants=taken, required=True)  # p533
    defs = load_defs(session)
    kept: set[int] = set()  # archived articles that are already part of this set may stay
    kept_sets: set[int] = set()  # so may inner sets that became archived or incomplete (plan v0.4.3, 7)
    if set_id is not None:
        kept = set(
            session.scalars(
                select(SetPositionOption.article_id)
                .join(SetPosition, SetPosition.id == SetPositionOption.position_id)
                .where(SetPosition.set_id == set_id)
            )
        )
        kept_sets = {row.set_id for row in defs[set_id].base if row.set_id is not None}
    seen: set[int] = set()
    seen_sets: set[int] = set()

    def article(article_id: int) -> Article:
        """The article of a row; each article is only once in a set (p340, question 5)."""
        found = session.get(Article, article_id)
        if found is None:
            raise NotFoundError("articles.not_found")
        if found.archived_at is not None and article_id not in kept:
            raise ValidationError("sets.article_archived", name=found.name)
        if article_id in seen:
            raise ValidationError("sets.article_twice", name=found.name)
        if article_id not in kept and not _allowed_in_set(session, found):
            raise ValidationError("sets.article_not_allowed", name=found.name)
        seen.add(article_id)
        return found

    def inner_set(inner_id: int) -> ProductSet:
        """An inner set of a base row (Spec 5.4, 0.4.3 p423): never the set itself, only once, and
        only an active and complete one, unless it is in the set already."""
        found = session.get(ProductSet, inner_id)
        if found is None:
            raise NotFoundError("sets.not_found")
        if inner_id == set_id:
            raise ValidationError("sets.ring", path=f"{name} → {name}")
        if inner_id in seen_sets:
            raise ValidationError("sets.set_twice", name=found.name)
        if inner_id not in kept_sets and (found.archived_at is not None or _incomplete_of(defs, inner_id) is not None):
            raise ValidationError("sets.set_not_allowed", name=found.name)
        seen_sets.add(inner_id)
        return found

    base: list[BaseSpec] = []
    for row in data.base:
        qty = inputs.int_in(row.qty, "sets.field.qty", 1, 1000)
        if row.set_id is not None:
            base.append((qty, None, inner_set(row.set_id).id))
        else:
            base.append((qty, article(row.article_id).id, None))
    fields = list(data.choices)
    while fields and not fields[-1].options:
        fields.pop()  # an empty last field is no field
    if len(fields) > CHOICE_FIELDS:
        raise ValueError("a set has at most two choice fields")
    if any(not field.options for field in fields):
        raise ValidationError("sets.choices2_without_1")
    choices: list[tuple[int, list[tuple[Article, str | None]]]] = []
    for field in fields:
        qty = inputs.int_in(field.qty, "sets.field.choice_qty", 1, 1000)
        options = [
            (article(option.article_id), inputs.text(option.suffix, "sets.field.suffix", MAX_SKU)) for option in field.options
        ]
        choices.append((qty, options))
    if len(base) + len(choices) < 2:  # every variant has two different articles (p340, question 3)
        raise ValidationError("sets.too_few_articles")  # a row with an inner set counts as one (F8)
    for _qty, options in choices:
        suffixes: set[str] = set()
        for found, suffix in options:
            if suffix is None:
                raise ValidationError("sets.suffix_required", name=found.name)
            if suffix in suffixes:
                raise ValidationError("sets.suffix_twice", suffix=suffix)
            suffixes.add(suffix)
    checked = [(qty, [(found.id, suffix) for found, suffix in options]) for qty, options in choices]
    before = dict(defs)
    own_id = _check_nesting(session, defs, set_id, name, sku, base, checked)
    _check_brought(session, before, defs, own_id, set_id)
    return name, sku, base, checked


def _check_nesting(
    session: Session,
    defs: dict[int, setresolve.SetDef],
    set_id: int | None,
    name: str,
    sku: str | None,
    base: list[BaseSpec],
    choices: list[tuple[int, list[tuple[int, str | None]]]],
) -> None:
    """The set as it would be saved, checked on every level (Spec 5.4, 0.4.3 p423): no ring, no
    article and no inner set twice, every option of every field counted (0.4.4); then every set that
    holds it, on any level and archived ones too (plan v0.4.3, decision 5), must stay whole: no
    article twice there. The checks run on the snapshot with the new rows in place of the stored
    ones (`defs` is changed so), so nothing is written before they pass. Returns the set's id in
    `defs`. Replaced: the refusal "a fixed variant falls away" (0.4.3); there is none any more.
    Tests: tests/domain/test_catalog_sets_nested.py, tests/domain/test_catalog_brought_choices.py."""
    own_id = set_id if set_id is not None else 0  # 0: a new set, no set has this id
    defs[own_id] = setresolve.SetDef(
        own_id,
        tuple(
            setresolve.Base(qty, set_id=inner_id) if inner_id is not None else setresolve.Base(qty, article_id=a)
            for qty, a, inner_id in base
        ),
        tuple(setresolve.Field(qty, tuple(a for a, _s in options), tuple(s for _a, s in options)) for qty, options in choices),
        name,
        sku,
    )
    try:
        setresolve.check(defs, own_id)
    except setresolve.Cycle as error:
        raise ValidationError("sets.ring", path=" → ".join(defs[i].name for i in error.path)) from None
    except setresolve.Twice as error:
        if error.article_id is not None:
            raise ValidationError("sets.article_twice", name=session.get(Article, error.article_id).name) from None
        raise ValidationError("sets.set_twice", name=defs[error.set_id].name) from None
    except setresolve.Unknown:
        raise NotFoundError("sets.not_found") from None
    if set_id is None:
        return own_id  # a new set stands in no other set yet
    doubled: list[int] = []
    twice_name = ""
    for parent in setresolve.containing(defs, set_id):
        try:
            setresolve.check(defs, parent)
        except setresolve.Twice as error:
            doubled.append(parent)
            if not twice_name:
                twice_name = (
                    session.get(Article, error.article_id).name if error.article_id is not None
                    else defs[error.set_id].name
                )
        except setresolve.ResolveError:
            continue  # a ring left in the database elsewhere; the own check above passed
    if doubled:
        raise ConflictError("sets.breaks_parent", names=_marked_names(session, defs, doubled), name=twice_name)
    return own_id


def _marked_names(session: Session, defs: dict[int, setresolve.SetDef], ids: list[int]) -> list[tuple[str, bool]]:
    """Each set with whether it is archived: the message marks those (plan approval v0.4.3, A1)."""
    archived = _archived_sets(session)
    return sorted(((defs[i].name, i in archived) for i in ids), key=lambda n: n[0].casefold())


def _check_brought(
    session: Session,
    before: dict[int, setresolve.SetDef],
    defs: dict[int, setresolve.SetDef],
    own_id: int,
    set_id: int | None,
) -> None:
    """The numbers of the set and of every set that holds it, with the new rows (Spec 5.4, 0.4.4):
    a choice part an inner set brings changes the variants of the sets around it at once.

    A set that would start to bring choice parts into a set without a number is refused, naming
    those sets (plan v0.4.4, F2): no set may fall out of the order form through a change made
    elsewhere. Then every variant number of these sets fits (64 characters) and is free: not an
    article's or a set's number and not another variant's, also not between these sets (p340,
    question 6); the message names the number, and the set when it is one that holds this one
    (Spec 5.4 "Änderungen", review of v0.4.4 finding 3). Article numbers are read once, not per
    number (finding 5). Tests: tests/domain/test_catalog_brought_choices.py."""
    parents = setresolve.containing(defs, set_id) if set_id is not None else []

    def has_fields(found: dict[int, setresolve.SetDef], i: int) -> bool:
        try:
            return bool(setresolve.fields(found, i))
        except setresolve.ResolveError:
            return False

    lacking = [p for p in parents if not defs[p].sku and has_fields(defs, p) and not has_fields(before, p)]
    if lacking:
        raise ConflictError("sets.parent_sku_required", names=_marked_names(session, defs, lacking))
    affected = [own_id, *parents]
    # At most 100 variants (Spec 5.4, p450), counted before any number is listed: listing them was
    # unbounded (two fields of 200 options, or inner sets on any level). A set stored with more since
    # an earlier version stays orderable; it is refused here once it or an inner set is saved.
    # Tests: tests/domain/test_catalog_variant_limit.py.
    for i in affected:
        count = setresolve.variant_count(defs, i)
        if count > MAX_VARIANTS:
            if i != own_id:
                raise ValidationError("sets.parent_too_many_variants", names=_marked_names(session, defs, [i]),
                                      count=count, max=MAX_VARIANTS)
            raise ValidationError("sets.too_many_variants", count=count, max=MAX_VARIANTS)
    # Every number already there, folded (case does not count, p350) -> as it is stored, for the message.
    taken = {same_number(number): number for i in defs if i not in affected for number in _numbers_of(defs, i)}
    taken.update((same_number(d.sku), d.sku) for d in defs.values() if d.sku)
    taken.update((same_number(n), n) for n in session.scalars(select(Article.sku).where(Article.sku.is_not(None))))
    seen: dict[str, str] = {}
    for i in affected:
        names = _marked_names(session, defs, [i]) if i != own_id else None
        for number in _numbers_of(defs, i):
            if len(number) > MAX_SKU:
                if names:
                    raise ValidationError("sets.parent_variant_too_long", names=names, sku=number, max=MAX_SKU)
                raise ValidationError("sets.variant_too_long", sku=number, max=MAX_SKU)
            folded = same_number(number)
            clash = seen.get(folded) or taken.get(folded)
            if clash is not None:
                if names:
                    raise ConflictError("sets.parent_sku_taken", names=names, sku=clash)
                raise ConflictError("catalog.sku_taken", sku=clash)
            seen[folded] = number


def _write_positions(
    session: Session,
    set_id: int,
    base: list[BaseSpec],
    choices: list[tuple[int, list[tuple[int, str | None]]]],
) -> None:
    for sort, (qty, article_id, inner_id) in enumerate(base):
        position = SetPosition(set_id=set_id, sort=sort, qty=qty, slot=0)
        session.add(position)
        session.flush()
        if inner_id is not None:
            session.add(SetPositionSet(position_id=position.id, set_id=inner_id))
        else:
            session.add(SetPositionOption(position_id=position.id, article_id=article_id))
    for slot, (qty, options) in enumerate(choices, start=1):
        position = SetPosition(set_id=set_id, sort=0, qty=qty, slot=slot)
        session.add(position)
        session.flush()
        for sort, (article_id, suffix) in enumerate(options):
            session.add(SetPositionOption(position_id=position.id, article_id=article_id, suffix=suffix, sort=sort))
    session.flush()


def create_set(session: Session, data: SetInput) -> ProductSet:
    name, sku, base, choices = _check_set_input(session, data)
    product_set = ProductSet(name=name, sku=sku)
    session.add(product_set)
    session.flush()
    _write_positions(session, product_set.id, base, choices)
    return product_set


def get_set(session: Session, set_id: int) -> ProductSet:
    product_set = session.get(ProductSet, set_id)
    if product_set is None:
        raise NotFoundError("sets.not_found")
    return product_set


def update_set(session: Session, set_id: int, data: SetInput) -> ProductSet:
    """Replace name, number, base parts and choices. Open orders keep their own lines."""
    product_set = get_set(session, set_id)
    name, sku, base, choices = _check_set_input(session, data, set_id=set_id)
    old = session.scalars(select(SetPosition.id).where(SetPosition.set_id == set_id)).all()
    if old:
        # Options and inner sets first: they point at their position.
        _delete_rows(session, select(SetPositionOption).where(SetPositionOption.position_id.in_(old)))
        _delete_rows(session, select(SetPositionSet).where(SetPositionSet.position_id.in_(old)))
        _delete_rows(session, select(SetPosition).where(SetPosition.id.in_(old)))
    product_set.name = name
    product_set.sku = sku
    _write_positions(session, set_id, base, choices)
    return product_set


def load_defs(session: Session) -> dict[int, setresolve.SetDef]:
    """All sets, archived ones too, as the snapshot setresolve reads (0.4.3 p423): four queries for
    any number of sets, so a page resolves every set without a query per level."""
    positions = session.scalars(
        select(SetPosition).order_by(SetPosition.set_id, SetPosition.slot, SetPosition.sort, SetPosition.id)
    ).all()
    options: dict[int, list[SetPositionOption]] = defaultdict(list)
    for option in session.scalars(
        select(SetPositionOption).order_by(SetPositionOption.sort, SetPositionOption.article_id)
    ):
        options[option.position_id].append(option)
    inner = {row.position_id: row for row in session.scalars(select(SetPositionSet))}
    base: dict[int, list[setresolve.Base]] = defaultdict(list)
    fields: dict[int, list[setresolve.Field]] = defaultdict(list)
    for position in positions:
        if position.slot > 0:
            found = options[position.id]
            fields[position.set_id].append(setresolve.Field(
                position.qty, tuple(o.article_id for o in found), tuple(o.suffix for o in found)
            ))
        elif position.id in inner:
            row = inner[position.id]
            base[position.set_id].append(setresolve.Base(position.qty, set_id=row.set_id))
        elif options[position.id]:
            base[position.set_id].append(setresolve.Base(position.qty, article_id=options[position.id][0].article_id))
    return {
        set_id: setresolve.SetDef(set_id, tuple(base[set_id]), tuple(fields[set_id]), name, sku)
        for set_id, name, sku in session.execute(select(ProductSet.id, ProductSet.name, ProductSet.sku))
    }


def _incomplete_of(
    defs: dict[int, setresolve.SetDef], set_id: int, memo: dict[int, str | None] | None = None
) -> str | None:
    """Why a set is not orderable as it is stored, or None: a ring, an article twice or an incomplete
    set inside ("broken", 0.4.3); too few rows, or a choice field without its numbers (sets the
    update to v0.4.0 rebuilt); brought choice parts without a number of its own ("number_missing",
    left by the update to 0.4.4). A row with an inner set counts as one (F8), a brought field does
    not count again (plan v0.4.4, F1).

    An inner set that only lacks its number does not break the sets around it: their variant numbers
    start with their own number, so each of them shows "number_missing" itself or stays orderable
    (Spec 5.4 update paragraph; review of v0.4.4 finding 2). Only the direct inner sets are asked,
    since each answers for its own, and `memo` keeps every answer of one page; asking every level
    again took twice as long per level (finding 1). The resolve runs first, so a stored ring inside a
    set with too few rows shows "broken" instead of failing the page (finding 4).
    Tests: tests/domain/test_catalog_brought_choices.py."""
    memo = {} if memo is None else memo
    if set_id not in memo:
        memo[set_id] = "broken"  # while it is asked: a stored ring through it is broken
        memo[set_id] = _state_of(defs, set_id, memo)
    return memo[set_id]


def _state_of(defs: dict[int, setresolve.SetDef], set_id: int, memo: dict[int, str | None]) -> str | None:
    found = defs[set_id]
    try:
        setresolve.check(defs, set_id)
        slots = setresolve.fields(defs, set_id)
    except setresolve.ResolveError:
        return "broken"
    if len(found.base) + len(found.fields) < 2:
        return "too_few"
    if found.fields and any(
        rules.variant_number(found.sku, [suffix]) is None for field in found.fields for suffix in field.suffixes
    ):
        return "suffixes_missing"
    direct = {row.set_id for row in found.base if row.set_id is not None}
    if any(_incomplete_of(defs, i, memo) not in (None, "number_missing") for i in direct):
        return "broken"
    if not found.sku and any(slot.path for slot in slots):
        return "number_missing"
    return None


@dataclass(frozen=True)
class SetToMend:
    """An active set that the update to 0.4.4 left out of the order form (plan v0.4.4, F3, N1)."""

    set_id: int
    set_name: str
    kind: Literal["number_missing", "twice", "broken"]
    name: str | None  # the article that stands twice


def sets_to_mend(session: Session) -> list[SetToMend]:
    """What the overview warns about besides the Altfall (plan v0.4.4, N1): active sets without a
    number that bring choice parts, and broken ones, an article twice named. Since 0.4.4 every option
    of an inner set counts against an article twice, so a set that 0.4.3 allowed may now be broken;
    neither can be ordered until it is changed. From the snapshot alone, like archived_in_sets.
    Tests: tests/domain/test_catalog_brought_choices.py."""
    defs = load_defs(session)
    archived = _archived_sets(session)
    found: list[SetToMend] = []
    memo: dict[int, str | None] = {}
    for held in sorted((d for d in defs.values() if d.id not in archived), key=lambda d: (d.name.casefold(), d.id)):
        state = _incomplete_of(defs, held.id, memo)
        if state == "number_missing":
            found.append(SetToMend(held.id, held.name, "number_missing", None))
        elif state == "broken":
            try:
                setresolve.check(defs, held.id)
            except setresolve.Twice as error:
                if error.article_id is not None:
                    name = session.get(Article, error.article_id).name
                    found.append(SetToMend(held.id, held.name, "twice", name))
                    continue
            except setresolve.ResolveError:
                pass
            found.append(SetToMend(held.id, held.name, "broken", None))
    return found


def sets_containing(session: Session, set_id: int) -> list[str]:
    """Names of the active sets that hold this set in one of their own rows ("Steckt in", Spec 5.4)."""
    parents = setresolve.direct_parents(load_defs(session), set_id)
    return list(session.scalars(
        select(ProductSet.name)
        .where(ProductSet.id.in_(parents), ProductSet.archived_at.is_(None))
        .order_by(ProductSet.name, ProductSet.id)
    ))


def _archived_sets(session: Session) -> set[int]:
    return set(session.scalars(select(ProductSet.id).where(ProductSet.archived_at.is_not(None))))


def _set_row(
    session: Session,
    product_set: ProductSet,
    defs: dict[int, setresolve.SetDef] | None = None,
    archived_sets: set[int] | None = None,
    memo: dict[int, str | None] | None = None,
) -> SetRow:
    """A set as the list shows it. Its base parts count with their resolved articles (Spec 5.4: an
    inner set counts as its articles, quantities multiplied); no article comes twice (Spec 5.4), so
    the availability rule of 0.4.0 stays as it is. A set that cannot be resolved (a ring or a doubled
    article left in the database), or that holds an incomplete set, shows "broken" and is not
    orderable instead of hanging the page (plan v0.4.3, decision 6).

    Archived parts no longer take a set or a variant out of the order form (Spec 5.4 "Archivieren und
    Sets", 0.4.3): what stands in an active set cannot be archived any more, and an archived article
    left in one by 0.4.0-0.4.2 (the "Altfall") counts with its available stock like any other, so an
    incoming order can always be entered; the list warns about it (archived_in_sets). Replaced: an
    archived choice dropped its variants and an archived base part made the set unorderable (0.4.0).
    Tests: tests/domain/test_catalog_sets_nested.py, tests/domain/test_archive.py."""
    defs = load_defs(session) if defs is None else defs
    archived_sets = _archived_sets(session) if archived_sets is None else archived_sets
    own = defs[product_set.id]
    incomplete = _incomplete_of(defs, product_set.id, memo)
    broken = incomplete == "broken"
    lines: tuple[setresolve.Line, ...] = ()
    slots: tuple[setresolve.Slot, ...] = ()
    if not broken:
        lines = setresolve.resolve_base(defs, product_set.id)
        slots = setresolve.fields(defs, product_set.id)
    wanted = {line.article_id for line in lines}
    wanted |= {row.article_id for row in own.base if row.article_id is not None}
    wanted |= {option for field in own.fields for option in field.options}
    wanted |= {option for slot in slots for option in slot.options}
    found = {a.id: a for a in session.scalars(select(Article).where(Article.id.in_(wanted)))}

    base: list[BaseRow] = []
    for row in own.base:
        if row.set_id is None:
            article = found[row.article_id]
            base.append(BaseRow(row.qty, article.id, article.name, article.archived_at is not None))
            continue
        child = defs.get(row.set_id)
        if child is None:
            continue
        contents = tuple(
            (line.qty // row.qty, found[line.article_id].name)
            for line in lines
            if line.path and line.path[0].set_id == row.set_id
        )
        base.append(BaseRow(
            row.qty, None, child.name, row.set_id in archived_sets, set_id=row.set_id, sku=child.sku, contents=contents,
        ))
    base_stock = [
        (line.qty, rules.available(found[line.article_id].stock, found[line.article_id].reserved)) for line in lines
    ]
    choices = [
        ChoiceRow(field.qty, [
            ChoiceOptionRow(a, found[a].name, suffix, found[a].archived_at is not None)
            for a, suffix in zip(field.options, field.suffixes)
        ])
        for field in own.fields
    ]
    # every field, brought ones first, counts for availability and variants (Spec 5.4, 0.4.4); archived
    # options too (0.4.3)
    stocks = [
        rules.ChoiceStock(slot.qty, tuple((a, rules.available(found[a].stock, found[a].reserved)) for a in slot.options))
        for slot in slots
    ]
    suffixes = {a: suffix for slot in slots for a, suffix in zip(slot.options, slot.suffixes)}
    stock = rules.SetStock(0, {}) if broken else rules.set_availability(base_stock, stocks)
    variants = [
        VariantRow(key, rules.variant_number(product_set.sku, [suffixes[a] for a in key]), [found[a].name for a in key], count)
        for key, count in stock.variants.items()
    ]
    orderable = product_set.archived_at is None and incomplete is None and (not slots or bool(variants))
    held: list[tuple[str, str]] = []
    if product_set.archived_at is None:  # the Altfall concerns active sets only (review finding 1)
        held = [("set" if row.set_id is not None else "article", row.name) for row in base if row.archived]
        held += [("article", option.name) for choice in choices for option in choice.options if option.archived]
    return SetRow(
        id=product_set.id,
        name=product_set.name,
        sku=product_set.sku,
        available=stock.total,
        base=base,
        choices=choices,
        variants=variants,
        incomplete=incomplete,
        orderable=orderable,
        status=set_status(available=stock.total),
        held_archived=tuple(held),
    )


@dataclass(frozen=True)
class ArchivedInSet:
    """An archived article or set that an active set holds directly (Spec 5.4 Altfall, 0.4.3)."""

    set_id: int
    set_name: str
    kind: Literal["article", "set"]
    name: str


def archived_in_sets(session: Session) -> list[ArchivedInSet]:
    """What the set list and the overview warn about (Spec 5.4 Altfall): per active set, by name, the
    archived articles and sets it holds directly. Only there, because that is where it is restored
    or taken out; a set that holds it through an inner set is not named again. Since 0.4.3 nothing
    in an active set can be archived, so this is what an earlier version left, or what came back
    with a restored set. Tests: tests/domain/test_catalog_sets_nested.py."""
    # From the snapshot alone, not from list_sets: the overview asks on every live refresh (review finding 6).
    defs = load_defs(session)
    archived_sets = _archived_sets(session)
    archived = dict(session.execute(select(Article.id, Article.name).where(Article.archived_at.is_not(None))).all())
    found: list[ArchivedInSet] = []
    for held in sorted((d for d in defs.values() if d.id not in archived_sets), key=lambda d: (d.name.casefold(), d.id)):
        for row in held.base:
            if row.article_id in archived:
                found.append(ArchivedInSet(held.id, held.name, "article", archived[row.article_id]))
            elif row.set_id in archived_sets:
                found.append(ArchivedInSet(held.id, held.name, "set", defs[row.set_id].name))
        for field in held.fields:
            found += [ArchivedInSet(held.id, held.name, "article", archived[o]) for o in field.options if o in archived]
    return found


def _active_holders(session: Session, defs: dict[int, setresolve.SetDef], direct: set[int]) -> list[str]:
    """Names of the active sets that hold something on any level (Spec 5.4 "Archivieren und Sets"):
    the sets that hold it directly and every set that holds one of those, archived ones passed
    through, since a restored set may hold an archived set (Spec 5.4 Wiederherstellen; review finding 2)."""
    ids = set(direct)
    for set_id in direct:
        ids.update(setresolve.containing(defs, set_id))
    archived_sets = _archived_sets(session)
    return sorted((defs[i].name for i in ids if i in defs and i not in archived_sets), key=str.casefold)


def sets_holding_article(session: Session, article_id: int) -> list[str]:
    """The active sets an article stands in on any level, as base part or choice (Spec 5.4, 5.5)."""
    direct = set(session.scalars(
        select(SetPosition.set_id)
        .join(SetPositionOption, SetPositionOption.position_id == SetPosition.id)
        .where(SetPositionOption.article_id == article_id)
    ))
    return _active_holders(session, load_defs(session), direct)


def sets_holding_set(session: Session, set_id: int) -> list[str]:
    """The active sets a set stands in on any level (Spec 5.4, 5.5)."""
    defs = load_defs(session)
    return _active_holders(session, defs, set(setresolve.direct_parents(defs, set_id)))


def set_row(session: Session, set_id: int) -> SetRow:
    return _set_row(session, get_set(session, set_id))


def list_sets(session: Session, *, archived: bool = False) -> list[SetRow]:
    found = session.scalars(select(ProductSet).where(_archive_filter(ProductSet.archived_at, archived))).all()
    sets = sorted(found, key=lambda s: (s.name.casefold(), s.id))
    defs, archived_sets = load_defs(session), _archived_sets(session)
    memo: dict[int, str | None] = {}  # one answer per set for the whole page (review of v0.4.4, finding 1)
    return [_set_row(session, s, defs, archived_sets, memo) for s in sets]
