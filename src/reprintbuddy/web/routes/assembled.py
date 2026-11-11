"""The form of an article made of parts: create and edit (Spec 5.6, 0.4.0 p341).

Base parts (quantity and print article per row), choice parts 1 and 2 (options with a suffix, each
with rows of quantity and print article), purchased parts as on the article form, and for an article
built on stock the minimum and buffer: without choices for the article, with choices per variant in
a table. Without JavaScript "more rows", "more options" and "add choice parts 2" send the form, which
comes back with the rows added and nothing saved (as the set form of Part A).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy.domain import assembled, catalog
from reprintbuddy.domain.assembled import AssembledInput, OptionInput
from reprintbuddy.domain.catalog import MAX_LABEL_TEXT, BaseInput
from reprintbuddy.domain.errors import DomainError, ValidationError
from reprintbuddy.domain.stock import MAX_QTY
from reprintbuddy.models import Article, ArticlePart
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import current_language, get_db, render
from reprintbuddy.web.forms import (
    bounded_int,
    duplicate_redirect,
    error_status,
    error_text,
    parse_int,
    parse_text,
    saved_redirect,
)
from reprintbuddy.web.formtoken import FormTokenInvalid, FormTokenUsed, form_transaction, write_transaction
from reprintbuddy.web.routes.articles import active_parts, change_part_rows, parse_parts, part_rows

router = APIRouter()

DEFAULT_BASE_ROWS = 3
DEFAULT_OPTIONS = 2
DEFAULT_OPTION_ROWS = 2
MORE_ROWS = 2
MAX_ROWS = 200
MAX_OPTIONS = 200
MAX_ID = 2**31 - 1
FIELDS = ("c1", "c2")


def _count(form, name: str, default: int, maximum: int) -> int:
    value = bounded_int(str(form.get(name, "")).strip())
    return value if value is not None and 1 <= value <= maximum else default


def _blank_row() -> dict[str, str]:
    return {"qty": "1", "article": ""}


def _blank_option(rows: int = DEFAULT_OPTION_ROWS) -> dict:
    return {"id": "", "suffix": "", "rows": [_blank_row() for _ in range(rows)]}


def _typed(form) -> dict:
    """What the form holds, as typed."""
    values: dict = {
        "name": str(form.get("name", "")),
        "sku": str(form.get("sku", "")),
        "label_text": str(form.get("label_text", "")),
        "mode": "order" if form.get("mode") == "order" else "stock",
        "min_stock": str(form.get("min_stock", "0")),
        "buffer": str(form.get("buffer", "0")),
        "show_c2": form.get("c2_shown") == "1",
        "base": [
            {"qty": str(form.get(f"base_{i}_qty", "1")), "article": str(form.get(f"base_{i}_article", "")).strip()}
            for i in range(_count(form, "base_rows", DEFAULT_BASE_ROWS, MAX_ROWS))
        ],
        "has_parts": form.get("has_parts") is not None,
        "part_rows": part_rows(form),
        "focus": None,
        "fresh": "",
        "targets": {},
    }
    for field in FIELDS:
        if field == "c2" and not values["show_c2"]:
            values[field] = []
            continue
        options = []
        for i in range(_count(form, f"{field}_options", DEFAULT_OPTIONS, MAX_OPTIONS)):
            prefix = f"{field}_{i}"
            options.append({
                "id": str(form.get(f"{prefix}_id", "")).strip(),
                "suffix": str(form.get(f"{prefix}_suffix", "")),
                "rows": [
                    {"qty": str(form.get(f"{prefix}_{j}_qty", "1")), "article": str(form.get(f"{prefix}_{j}_article", "")).strip()}
                    for j in range(_count(form, f"{prefix}_rows", DEFAULT_OPTION_ROWS, MAX_ROWS))
                ],
            })
        values[field] = options
    for key in form.keys():
        if key.startswith("v_") and key.endswith("_min"):
            ident = key[2:-4]
            values["targets"][ident] = {"min": str(form.get(key, "0")), "buffer": str(form.get(f"v_{ident}_buffer", "0"))}
    return values


def _article_id(raw: str) -> int:
    return parse_int(raw, field="assembled.field.article", min=1, max=MAX_ID)


def _qty(raw: str) -> int:
    return parse_int(raw, field="assembled.field.qty", min=1, max=1000)


def _parse(values: dict, form, parts: list) -> AssembledInput:
    base = [BaseInput(_qty(row["qty"]), _article_id(row["article"])) for row in values["base"] if row["article"]]
    fields = []
    for field in FIELDS:
        options = []
        for option in values[field]:
            rows = [BaseInput(_qty(r["qty"]), _article_id(r["article"])) for r in option["rows"] if r["article"]]
            if not rows and not option["suffix"].strip():
                continue  # an empty option is no option
            option_id = bounded_int(option["id"]) if option["id"] else None
            options.append(OptionInput(option_id if option_id and option_id <= MAX_ID else None, option["suffix"], rows))
        fields.append(options)
    targets: dict[int, tuple[int, int]] = {}
    for ident, pair in values["targets"].items():
        number = bounded_int(ident)
        if number is None or number > MAX_ID:
            continue
        targets[number] = (
            parse_int(pair["min"], field="articles.field.min_stock", min=0, max=MAX_QTY),
            parse_int(pair["buffer"], field="articles.field.buffer", min=0, max=MAX_QTY),
        )
    return AssembledInput(
        name=parse_text(values["name"], field="articles.field.name", max_len=200, required=True),
        sku=parse_text(values["sku"], field="catalog.field.sku", max_len=64),
        mode=values["mode"],
        base=base,
        fields=fields,
        parts=parse_parts(form, parts),
        label_text=parse_text(values["label_text"], field="articles.field.label_text", max_len=MAX_LABEL_TEXT),
        min_stock=parse_int(values["min_stock"] or "0", field="articles.field.min_stock", min=0, max=MAX_QTY),
        buffer=parse_int(values["buffer"] or "0", field="articles.field.buffer", min=0, max=MAX_QTY),
        variant_targets=targets,
    )


def _print_articles(db: Session, values: dict) -> list[dict[str, object]]:
    """The print articles to choose from: active ones, parts (not sold alone) marked; an archived one
    only when it is chosen already (the list keeps it, Spec 5.5)."""
    from reprintbuddy.domain import articlesets

    offer = {
        a.id: {"id": str(a.id), "name": a.name, "part_only": not a.sold_alone, "archived": False}
        for a in db.scalars(articlesets.print_articles().order_by(Article.name, Article.id))
    }
    chosen = set()
    for row in values["base"] + [r for f in FIELDS for o in values[f] for r in o["rows"]]:
        number = bounded_int(row["article"])
        if number is not None and number <= MAX_ID:
            chosen.add(number)
    for a in db.scalars(select(Article).where(Article.id.in_(chosen), Article.kind == "print", Article.archived_at.is_not(None))):
        offer[a.id] = {"id": str(a.id), "name": a.name, "part_only": not a.sold_alone, "archived": True}
    return sorted(offer.values(), key=lambda o: (str(o["name"]).casefold(), o["id"]))


def _taken(values: dict) -> dict[str, list[str]]:
    """Articles chosen per place: an article chosen in the base parts is offered in no option, one of
    field 1 not in field 2 and the other way round; within a field it may repeat across options."""
    return {
        "base": [r["article"] for r in values["base"] if r["article"]],
        "c1": sorted({r["article"] for o in values["c1"] for r in o["rows"] if r["article"]}),
        "c2": sorted({r["article"] for o in values["c2"] for r in o["rows"] if r["article"]}),
    }


def _variant_table(db: Session, composite: Article | None, values: dict) -> list[dict[str, str]]:
    """The variants of a saved article with choices on stock: number, stock, minimum, buffer."""
    if composite is None:
        return []
    rows = []
    for row in assembled.variants(db, composite):
        if row.id == composite.id:
            continue
        typed = values["targets"].get(str(row.id), {})
        rows.append({
            "id": str(row.id), "sku": row.sku or "", "stock": str(row.stock),
            "min": typed.get("min", str(row.min_stock)), "buffer": typed.get("buffer", str(row.buffer)),
        })
    return rows


def _form_page(request, db, *, composite, values, error=None, status=200, action=None):
    taken = _taken(values)
    if action is None:
        action = f"/articles/{composite.id}/assembled" if composite else "/articles/new-assembled"
    return render(
        request,
        "assembled_form.html",
        status_code=status,
        active_nav="articles",
        composite=composite,
        form_action=action,
        values=values,
        articles=_print_articles(db, values),
        taken=taken,
        blocked={
            "base": set(taken["c1"]) | set(taken["c2"]),
            "c1": set(taken["base"]) | set(taken["c2"]),
            "c2": set(taken["base"]) | set(taken["c1"]),
        },
        parts=active_parts(db),
        variants=_variant_table(db, composite, values),
        blank_option=_blank_option(),  # what a new option and a new choice field start with (more_rows.js)
        default_options=DEFAULT_OPTIONS,
        more_rows=MORE_ROWS,
        max_rows=MAX_ROWS,
        max_options=MAX_OPTIONS,
        error=error,
    )


def _grow(values: dict, action: str) -> bool:
    """The actions that only show the form again with more room; True when one was taken.

    values["fresh"] names the first new place for the jump mark (p380): "base:3" a base row, "c1:2" an
    option, "c1:0:2" a row of option 0, "c2" choice parts 2 as a whole."""
    if action == "add_c2":
        values["show_c2"] = True
        values["c2"] = values["c2"] or [_blank_option() for _ in range(DEFAULT_OPTIONS)]
        values["fresh"] = "c2"
        return True
    if action == "more_base":
        values["fresh"] = f"base:{len(values['base'])}"
        values["base"].extend(_blank_row() for _ in range(MORE_ROWS))
        return True
    for field in FIELDS:
        if action == f"more_{field}":
            values["fresh"] = f"{field}:{len(values[field])}"
            values[field].extend(_blank_option() for _ in range(MORE_ROWS))
            return True
        prefix = f"more_{field}_"
        if action.startswith(prefix):
            index = bounded_int(action[len(prefix):])
            if index is not None and index < len(values[field]):
                values["fresh"] = f"{field}:{index}:{len(values[field][index]['rows'])}"
                values[field][index]["rows"].extend(_blank_row() for _ in range(MORE_ROWS))
            return True
    return False


async def _post(request: Request, db: Session, composite: Article | None):
    form = await request.form()
    values = _typed(form)
    if change_part_rows(form, values) or _grow(values, str(form.get("action", ""))):
        return _form_page(request, db, composite=composite, values=values)
    try:
        data = _parse(values, form, active_parts(db))
        if composite is None:
            with form_transaction(request, str(form.get("form_token", ""))) as tx:
                created = assembled.create_assembled(tx, data)
                target = f"/articles/{created.id}"
        else:
            with write_transaction(request) as tx:
                assembled.update_assembled(tx, composite.id, data)
            target = f"/articles/{composite.id}"
    except FormTokenUsed:
        return duplicate_redirect(request, "/articles")
    except (DomainError, FormTokenInvalid) as exc:
        return _form_page(
            request, db, composite=composite, values=values,
            error=error_text(current_language(request), exc), status=error_status(exc),
        )
    return saved_redirect(request, target, "articles.saved" if composite else "articles.created", name=data.name)


def new_values() -> dict:
    return {
        "name": "", "sku": "", "label_text": "", "mode": "stock", "min_stock": "0", "buffer": "0",
        "show_c2": False, "base": [_blank_row() for _ in range(DEFAULT_BASE_ROWS)],
        "c1": [_blank_option() for _ in range(DEFAULT_OPTIONS)], "c2": [],
        "has_parts": False, "part_rows": [], "focus": None, "fresh": "", "targets": {},
    }


def values_of(db: Session, composite: Article) -> dict:
    """The form of a saved article made of parts, one empty row and option more per part."""
    row = assembled.assembled_row(db, composite.id)
    links = dict(db.execute(select(ArticlePart.part_id, ArticlePart.qty_per_unit).where(ArticlePart.article_id == composite.id)).all())
    values = {
        "name": composite.name, "sku": composite.sku or "", "label_text": composite.label_text or "",
        "mode": composite.assembly_mode or "stock", "min_stock": str(composite.min_stock), "buffer": str(composite.buffer),
        "show_c2": len(row.fields) > 1,
        "base": [{"qty": str(p.qty), "article": str(p.article_id)} for p in row.base] + [_blank_row()],
        "c1": [], "c2": [],
        "has_parts": bool(links),
        "part_rows": [{"part": str(p), "qty": str(q)} for p, q in links.items()],
        "focus": None, "fresh": "", "targets": {},
    }
    for field, options in zip(FIELDS, row.fields):
        values[field] = [
            {"id": str(o.option_id), "suffix": o.suffix,
             "rows": [{"qty": str(p.qty), "article": str(p.article_id)} for p in o.rows] + [_blank_row()]}
            for o in options
        ] + [_blank_option()]
    if not row.fields:
        values["c1"] = [_blank_option() for _ in range(DEFAULT_OPTIONS)]
    return values


def _print_article(db: Session, article_id: int) -> Article:
    article = db.get(Article, article_id)
    if article is None or article.kind != "print":
        raise HTTPException(status_code=404)
    return article


@router.get("/articles/{article_id:int}/to-assembled")
async def to_assembled_form(request: Request, article_id: int, db: Session = Depends(get_db)):
    """A print article becomes an article made of parts (Spec 5.6 "Wechsel"): the form, with its name,
    number and label text; saving checks that nothing runs and its stock is 0."""
    article = _print_article(db, article_id)
    values = new_values()
    values.update(name=article.name, sku=article.sku or "", label_text=article.label_text or "")
    return _form_page(request, db, composite=None, values=values, action=f"/articles/{article_id}/to-assembled")


@router.post("/articles/{article_id:int}/to-assembled", dependencies=[Depends(verify_csrf)])
async def to_assembled(request: Request, article_id: int, db: Session = Depends(get_db)):
    article = _print_article(db, article_id)
    action = f"/articles/{article_id}/to-assembled"
    form = await request.form()
    values = _typed(form)
    if change_part_rows(form, values) or _grow(values, str(form.get("action", ""))):
        return _form_page(request, db, composite=None, values=values, action=action)
    try:
        data = _parse(values, form, active_parts(db))
        with form_transaction(request, str(form.get("form_token", ""))) as tx:
            assembled.convert_to_assembled(tx, article.id, data)
    except FormTokenUsed:
        return duplicate_redirect(request, f"/articles/{article_id}")
    except (DomainError, FormTokenInvalid) as exc:
        return _form_page(
            request, db, composite=None, values=values, action=action,
            error=error_text(current_language(request), exc), status=error_status(exc),
        )
    return saved_redirect(request, f"/articles/{article_id}", "articles.saved", name=data.name)


@router.get("/articles/new-assembled")
async def assembled_new(request: Request, db: Session = Depends(get_db)):
    return _form_page(request, db, composite=None, values=new_values())


@router.post("/articles/new-assembled", dependencies=[Depends(verify_csrf)])
async def assembled_create(request: Request, db: Session = Depends(get_db)):
    return await _post(request, db, None)


def _get(db: Session, article_id: int) -> Article:
    composite = db.get(Article, article_id)
    if composite is None or composite.kind != "assembled":
        raise HTTPException(status_code=404)
    return composite


@router.get("/articles/{article_id:int}/assembled")
async def assembled_edit(request: Request, article_id: int, db: Session = Depends(get_db)):
    composite = _get(db, article_id)
    if composite.archived_at is not None:
        return _form_page(
            request, db, composite=composite, values=values_of(db, composite),
            error=error_text(current_language(request), ValidationError("articles.edit_archived")),
        )
    return _form_page(request, db, composite=composite, values=values_of(db, composite))


@router.post("/articles/{article_id:int}/assembled", dependencies=[Depends(verify_csrf)])
async def assembled_update(request: Request, article_id: int, db: Session = Depends(get_db)):
    return await _post(request, db, _get(db, article_id))
