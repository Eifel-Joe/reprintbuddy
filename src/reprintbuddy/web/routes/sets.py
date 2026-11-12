"""Sets: list, create, edit (Spec 5.4). A set has no stock of its own."""

from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy.domain import catalog, setresolve
from reprintbuddy.domain.catalog import BaseInput, ChoiceInput, ChoiceOptionInput, SetInput
from reprintbuddy.domain.errors import DomainError, ValidationError
from reprintbuddy.domain.status import STATES, summarize
from reprintbuddy.models import Article, ProductSet
from reprintbuddy.web import kpis, listing
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import current_language, get_db, render
from reprintbuddy.web.i18n import translate
from reprintbuddy.web.forms import (
    bounded_int,
    duplicate_redirect,
    run_form_action,
    error_status,
    error_text,
    parse_int,
    parse_text,
    saved_redirect,
)
from reprintbuddy.web.formtoken import (
    FormTokenInvalid,
    FormTokenUsed,
    form_transaction,
    write_transaction,
)

router = APIRouter()

# The form is compact (plan approval p340): three rows of base parts and of choice parts 1; choice
# parts 2 appear on request ("add_c2") or when the set has them. "More rows" adds rows to one part.
DEFAULT_ROWS = 3
MORE_ROWS = 3
MAX_ROWS = 200  # a set may have many rows; the edit form must show all of them
MAX_ID = 2**31 - 1
SECTIONS = ("base", "c1", "c2")


def _count(form, section: str) -> int:
    value = bounded_int(str(form.get(f"{section}_rows", "")).strip())
    return value if value is not None and 1 <= value <= MAX_ROWS else DEFAULT_ROWS


def _typed(form) -> dict:
    """What the form holds, as typed: rows of base parts and of the choice fields."""
    typed = {
        "name": str(form.get("name", "")),
        "sku": str(form.get("sku", "")),
        "show_c2": form.get("c2_shown") == "1",
        "base": [
            {"qty": str(form.get(f"base_{i}_qty", "1")), "article": str(form.get(f"base_{i}_article", "")).strip()}
            for i in range(_count(form, "base"))
        ],
    }
    for section in SECTIONS[1:]:
        typed[f"{section}_qty"] = str(form.get(f"{section}_qty", "1"))
        typed[section] = [
            {
                "article": str(form.get(f"{section}_{i}_article", "")).strip(),
                "suffix": str(form.get(f"{section}_{i}_suffix", "")),
            }
            for i in range(_count(form, section) if section == "c1" or typed["show_c2"] else 0)
        ]
    return typed


def _empty(values: dict) -> dict:
    values.setdefault("fresh", "")  # "section:index" of the first row a "more" button added, else ""
    values.setdefault("base", [])
    for section in SECTIONS[1:]:
        values.setdefault(f"{section}_qty", "1")
        values.setdefault(section, [])
    return values


def _grow(values: dict, section: str, rows: int) -> None:
    """At least `rows` rows in a part (an empty row is one to choose in)."""
    blank = {"qty": "1", "article": ""} if section == "base" else {"article": "", "suffix": ""}
    values[section].extend(dict(blank) for _ in range(min(rows, MAX_ROWS) - len(values[section])))


def _article_id(raw: str) -> int:
    return parse_int(raw, field="sets.field.article", min=1, max=MAX_ID)


def _set_value(set_id: int, key: tuple[int, ...]) -> str:
    """The value of an inner set in the drop-down of base parts: "set:5", a variant "set:5:12"
    (the chosen article per choice field), like the order form (0.4.3 p423)."""
    return ":".join(["set", str(set_id), *(str(a) for a in key)])


def _base_choice(raw: str) -> tuple[int | None, int | None, tuple[int, ...]]:
    """What a base row holds: (article id, None, ()) or (None, set id, key)."""
    if not raw.startswith("set:"):
        return _article_id(raw), None, ()
    parts = raw[4:].split(":")
    if len(parts) > 1 + catalog.CHOICE_FIELDS:
        raise ValidationError("form.invalid_number", field="sets.field.article", min=1, max=MAX_ID)
    numbers = [_article_id(part) for part in parts]
    return None, numbers[0], tuple(numbers[1:])


def _parse(values: dict) -> SetInput:
    base = []
    for row in values["base"]:
        if not row["article"]:
            continue  # rows without an article are skipped
        qty = parse_int(row["qty"], field="sets.field.qty", min=1, max=1000)
        article_id, set_id, key = _base_choice(row["article"])
        base.append(BaseInput(qty, article_id, set_id, key))
    choices = []
    for section in ("c1", "c2") if values["show_c2"] else ("c1",):
        options = [ChoiceOptionInput(_article_id(row["article"]), row["suffix"]) for row in values[section] if row["article"]]
        qty = parse_int(values[f"{section}_qty"], field="sets.field.choice_qty", min=1, max=1000) if options else 1
        choices.append(ChoiceInput(qty, options))
    return SetInput(
        name=parse_text(values["name"], field="sets.field.name", max_len=200, required=True),
        sku=parse_text(values["sku"], field="catalog.field.sku", max_len=64),
        base=base,
        choices=choices,
    )


def _article_options(db: Session, values: dict) -> list[dict[str, object]]:
    """Active articles, plus archived ones that are chosen in the form: the set keeps them (Spec 5.5)."""
    options = {
        row.id: {"id": str(row.id), "name": row.name, "archived": False}
        for row in catalog.list_articles(db)
        if row.sold_alone and (row.assembled is None or (row.assembled.mode == "stock" and not row.assembled.fields))
    }
    chosen = set()
    for section in SECTIONS:
        for row in values[section]:
            number = bounded_int(row["article"])
            if number is not None and number <= MAX_ID:
                chosen.add(number)
    for article in db.scalars(select(Article).where(Article.id.in_(chosen), Article.archived_at.is_not(None))):
        options[article.id] = {"id": str(article.id), "name": article.name, "archived": True}
    return sorted(options.values(), key=lambda o: (str(o["name"]).casefold(), o["id"]))


def _set_options(db: Session, values: dict, product_set: ProductSet | None, lang: str) -> list[dict[str, object]]:
    """What the drop-down of base parts offers under "Sets" (Spec 5.4, 0.4.3 p423): each active,
    complete set by "name · number", a set with choice parts once per variant, "name · variant
    number (chosen articles)". Never the set itself or a set that holds it (a ring); an inner set the
    form already holds stays, marked archived if it is. Each entry carries what it takes ("takes":
    the set, the inner sets in it and its resolved articles), so that the form hides every entry
    that would bring an article or a set twice, and the text "contains ..." shown under its row."""
    defs = catalog.load_defs(db)
    own = product_set.id if product_set is not None else None
    holding = set(setresolve.containing(defs, own)) | {own} if own is not None else set()
    names = dict(db.execute(select(Article.id, Article.name)).all())
    options: dict[str, dict[str, object]] = {}

    def add(set_id: int, key: tuple[int, ...], label: str, archived: bool) -> None:
        try:
            lines = setresolve.resolve(defs, set_id, key)
        except setresolve.ResolveError:
            return
        inner = sorted({f"s{step.set_id}" for line in lines for step in line.path})
        takes = [f"s{set_id}", *inner, *(f"a{line.article_id}" for line in lines)]
        items = ", ".join(f"{line.qty} × {names[line.article_id]}" for line in lines)
        options[_set_value(set_id, key)] = {
            "id": _set_value(set_id, key), "name": label, "archived": archived,
            "takes": " ".join(takes), "contains": translate(lang, "sets.contains", items=items),
        }

    for row in catalog.list_sets(db):
        if row.id in holding or row.incomplete is not None:
            continue
        if not row.choices:
            add(row.id, (), f"{row.name} · {row.sku}" if row.sku else row.name, False)
        for variant in row.variants:
            add(row.id, variant.key, f"{row.name} · {variant.sku} ({', '.join(variant.names)})", False)
    archived = set(db.scalars(select(ProductSet.id).where(ProductSet.archived_at.is_not(None))))
    for row in values["base"]:  # an inner set the form holds although it is not offered (plan v0.4.3, 7)
        if not row["article"].startswith("set:") or row["article"] in options:
            continue
        try:
            _article_id_none, set_id, key = _base_choice(row["article"])
        except ValidationError:
            continue
        if set_id not in defs or set_id in holding:
            continue
        number = setresolve.variant_number(defs, set_id, key)
        label = defs[set_id].name + (f" · {number}" if number else "")
        add(set_id, key, label, set_id in archived)
    return sorted(options.values(), key=lambda o: (str(o["name"]).casefold(), o["id"]))


def _blocker(set_options: list[dict[str, object]], values: dict):
    """Whether an entry is hidden in a drop-down: one of the things it takes is chosen in another
    row already (Spec 5.4: no article and no inner set twice, on any level). An article takes itself.
    set_form.js does the same while choosing; this is the page without the script."""
    takes = {str(o["id"]): str(o["takes"]).split() for o in set_options}

    def tokens(value: str) -> list[str]:
        return takes.get(value) or ([f"a{value}"] if value.isdigit() else [])

    counts = Counter(tok for section in SECTIONS for row in values[section] if row["article"] for tok in tokens(row["article"]))

    def blocked(value: str, chosen: str) -> bool:
        if value == chosen:
            return False
        mine = set(tokens(chosen))
        return any(counts[tok] - (1 if tok in mine else 0) > 0 for tok in tokens(value))

    return blocked


def _form_page(request, db, *, product_set, values, error=None, status=200):
    lang = current_language(request)
    set_options = _set_options(db, values, product_set, lang)
    part_of = catalog.sets_containing(db, product_set.id) if product_set is not None else []
    return render(
        request,
        "set_form.html",
        status_code=status,
        active_nav="sets",
        product_set=product_set,
        values=values,
        articles=_article_options(db, values),
        sets=set_options,  # inner sets for the base parts (0.4.3 p423)
        contains={str(o["id"]): o["contains"] for o in set_options},
        blocked=_blocker(set_options, values),
        part_of=", ".join(part_of),
        form_action=f"/sets/{product_set.id}/edit" if product_set else "/sets/new",
        default_rows=DEFAULT_ROWS,  # rows of choice parts 2 when it is added
        more_rows=MORE_ROWS,
        max_rows=MAX_ROWS,
        error=error,
    )


async def _post(request: Request, db: Session, product_set: ProductSet | None):
    """Create or edit: "more rows", "add choice parts 2" or save."""
    form = await request.form()
    values = _empty(_typed(form))
    action = str(form.get("action", ""))
    if action == "add_c2":  # nothing is saved and the token stays unused
        values["show_c2"] = True
        _grow(values, "c2", DEFAULT_ROWS)
        values["fresh"] = "c2"  # the page jumps to the new fieldset (p380)
        return _form_page(request, db, product_set=product_set, values=values)
    if action.startswith("more_") and action[5:] in SECTIONS:
        section = action[5:]
        before = len(values[section])
        _grow(values, section, before + MORE_ROWS)
        if len(values[section]) > before:
            values["fresh"] = f"{section}:{before}"  # the page jumps to the first new row (p380)
        return _form_page(request, db, product_set=product_set, values=values)
    try:
        data = _parse(values)
        if product_set is None:
            with form_transaction(request, str(form.get("form_token", ""))) as tx:
                catalog.create_set(tx, data)
        else:
            with write_transaction(request) as tx:
                catalog.update_set(tx, product_set.id, data)
    except FormTokenUsed:
        return duplicate_redirect(request, "/sets")
    except (DomainError, FormTokenInvalid) as exc:
        return _form_page(
            request, db, product_set=product_set, values=values,
            error=error_text(current_language(request), exc), status=error_status(exc),
        )
    return saved_redirect(request, "/sets", "sets.saved" if product_set else "sets.created", name=data.name)


@router.get("/sets")
async def sets_list(
    request: Request, view: str = "active", q: str | None = None, db: Session = Depends(get_db)
):
    """Overview with key figure cards, search, status filter and tiles or list (Spec 10.1).

    Same rules as the article list: the cards count all sets of the view, the filter counts
    follow the search, the list shows what passes both. The archive has no status filter.
    """
    if (redirect := listing.remember_layout(request, "sets")) is not None:
        return redirect  # ?layout=... only sets the cookie
    archived = view == "archived"
    states = STATES["set"]
    status = None if archived else listing.pick(request.query_params.get("status"), states)
    every = catalog.list_sets(db, archived=archived)
    found = [row for row in every if listing.matches(q, row.name, row.sku, *(v.sku for v in row.variants if v.sku))]
    counts = Counter(row.status for row in found)
    lang = current_language(request)
    t = lambda key: translate(lang, key)  # noqa: E731
    icon, label, named = ("archive", "kpi.sets_archived", []) if archived else (
        "boxes", "kpi.sets", summarize((row.status for row in every), ("not_deliverable",))
    )
    cards = kpis.state_cards(t, "set", icon, len(every), t(label), named)
    keep = (("status", status), ("view", "archived" if archived else None))
    layout = listing.layout_of(request, "sets")
    return render(
        request, "sets_list.html", active_nav="sets", archived=archived,
        rows=[row for row in found if status is None or row.status == status],
        total=len(every),
        kpis=cards,
        filters=listing.filter_links(request, states, counts, len(found), status),
        layout=layout,
        layout_links=listing.layout_links(request, layout),
        q=q,
        search_keep=[(name, value) for name, value in keep if value],  # a new search keeps these
    )


@router.get("/sets/new")
async def sets_new(request: Request, db: Session = Depends(get_db)):
    values = _empty({"name": "", "sku": "", "show_c2": False})
    _grow(values, "base", DEFAULT_ROWS)
    _grow(values, "c1", DEFAULT_ROWS)
    return _form_page(request, db, product_set=None, values=values)


@router.post("/sets/new", dependencies=[Depends(verify_csrf)])
async def sets_create(request: Request, db: Session = Depends(get_db)):
    return await _post(request, db, None)


def _get_set(db: Session, set_id: int) -> ProductSet:
    product_set = db.get(ProductSet, set_id)
    if product_set is None:
        raise HTTPException(status_code=404)
    return product_set


@router.get("/sets/{set_id:int}/edit")
async def sets_edit(request: Request, set_id: int, db: Session = Depends(get_db)):
    return _edit_again(request, db, _get_set(db, set_id), None, 200)


@router.post("/sets/{set_id:int}/edit", dependencies=[Depends(verify_csrf)])
async def sets_update(request: Request, set_id: int, db: Session = Depends(get_db)):
    return await _post(request, db, _get_set(db, set_id))


def _archive_action(action, flash_key: str):
    async def handler(request: Request, set_id: int, db: Session = Depends(get_db)):
        product_set = _get_set(db, set_id)

        def work(tx):
            action(tx, set_id)
            return {"name": product_set.name}

        return await run_form_action(
            request,
            work=work,
            success_url="/sets",  # back to the list; an archived set leaves it, that is right
            flash_key=flash_key,
            on_error=lambda text, status, exc: _edit_again(request, db, product_set, text, status),
        )

    return handler


def _edit_again(request, db, product_set, error, status):
    """The form with what the set holds, one empty row more per part (at least three)."""
    row = catalog.set_row(db, product_set.id)
    values = _empty({
        "name": product_set.name,
        "sku": product_set.sku or "",
        "show_c2": len(row.choices) > 1,
        "base": [
            {"qty": str(b.qty), "article": _set_value(b.set_id, b.key) if b.set_id is not None else str(b.article_id)}
            for b in row.base
        ],
    })
    for section, field in zip(("c1", "c2"), row.choices):
        values[f"{section}_qty"] = str(field.qty)
        values[section] = [{"article": str(o.article_id), "suffix": o.suffix or ""} for o in field.options]
    for section in SECTIONS if values["show_c2"] else SECTIONS[:2]:
        _grow(values, section, max(DEFAULT_ROWS, len(values[section]) + 1))
    return _form_page(request, db, product_set=product_set, values=values, error=error, status=status)


router.add_api_route(
    "/sets/{set_id:int}/archive", _archive_action(catalog.archive_set, "sets.flash_archived"),
    methods=["POST"], dependencies=[Depends(verify_csrf)],
)
router.add_api_route(
    "/sets/{set_id:int}/restore", _archive_action(catalog.restore_set, "sets.flash_restored"),
    methods=["POST"], dependencies=[Depends(verify_csrf)],
)
