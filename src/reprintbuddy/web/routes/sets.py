"""Sets: list, create, edit (Spec 5.4). A set has no stock of its own."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from reprintbuddy.domain import catalog
from reprintbuddy.domain.catalog import PositionInput, SetInput
from reprintbuddy.domain.errors import DomainError
from reprintbuddy.models import Article, ProductSet, SetPosition, SetPositionOption
from reprintbuddy.web.csrf import verify_csrf
from reprintbuddy.web.deps import current_language, get_db, render
from reprintbuddy.web.forms import (
    ascii_digits,
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

DEFAULT_ROWS = 5
MORE_ROWS = 5
MAX_ROWS = 200  # a set may have many positions; the edit form must show all of them
MAX_ID = 2**31 - 1


def _rows_of(form, *, more: bool) -> int:
    raw = str(form.get("rows", "")).strip()
    rows = int(raw) if ascii_digits(raw) and 1 <= int(raw) <= MAX_ROWS else DEFAULT_ROWS
    return min(rows + MORE_ROWS, MAX_ROWS) if more else rows


def _typed_rows(form, rows: int) -> list[dict[str, object]]:
    result = []
    for i in range(rows):
        selected = set()
        for raw in form.getlist(f"pos_{i}_articles"):
            if ascii_digits(str(raw).strip()) and int(raw) <= MAX_ID:
                selected.add(int(raw))
        result.append({"qty": str(form.get(f"pos_{i}_qty", "1")), "selected": selected})
    return result


def _parse(form, rows: int) -> SetInput:
    positions = []
    for i in range(rows):
        raw_ids = [str(r).strip() for r in form.getlist(f"pos_{i}_articles") if str(r).strip()]
        if not raw_ids:
            continue  # rows without an article are skipped
        article_ids = [parse_int(r, field="sets.field.articles", min=1, max=MAX_ID) for r in raw_ids]
        qty = parse_int(form.get(f"pos_{i}_qty"), field="sets.field.qty", min=1, max=1000)
        positions.append(PositionInput(qty, article_ids))
    return SetInput(
        name=parse_text(form.get("name"), field="sets.field.name", max_len=200, required=True),
        sku=parse_text(form.get("sku"), field="catalog.field.sku", max_len=64),
        positions=positions,
    )


def _article_options(db: Session, rows_values) -> list[dict[str, object]]:
    """Active articles, plus archived ones that are selected in the form: the set keeps them."""
    options = {row.id: {"id": row.id, "name": row.name, "archived": False} for row in catalog.list_articles(db)}
    selected = {article_id for row in rows_values for article_id in row["selected"]}
    for article in db.scalars(select(Article).where(Article.id.in_(selected), Article.archived_at.is_not(None))):
        options[article.id] = {"id": article.id, "name": article.name, "archived": True}
    return sorted(options.values(), key=lambda o: (str(o["name"]).casefold(), o["id"]))


def _form_page(request, db, *, product_set, values, rows_values, rows, error=None, status=200):
    return render(
        request,
        "set_form.html",
        status_code=status,
        active_nav="sets",
        product_set=product_set,
        values=values,
        rows_values=rows_values,
        rows=rows,
        articles=_article_options(db, rows_values),
        error=error,
    )


@router.get("/sets")
async def sets_list(request: Request, view: str = "active", db: Session = Depends(get_db)):
    archived = view == "archived"
    return render(
        request, "sets_list.html", active_nav="sets", archived=archived,
        rows=catalog.list_sets(db, archived=archived),
    )


@router.get("/sets/new")
async def sets_new(request: Request, db: Session = Depends(get_db)):
    rows_values = [{"qty": "1", "selected": set()} for _ in range(DEFAULT_ROWS)]
    return _form_page(
        request, db, product_set=None, values={"name": "", "sku": ""},
        rows_values=rows_values, rows=DEFAULT_ROWS,
    )


@router.post("/sets/new", dependencies=[Depends(verify_csrf)])
async def sets_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    more = form.get("action") == "more"
    rows = _rows_of(form, more=more)
    values = {"name": str(form.get("name", "")), "sku": str(form.get("sku", ""))}
    rows_values = _typed_rows(form, rows)
    if more:  # show more rows; nothing is saved and the token stays unused
        rows_values += [{"qty": "1", "selected": set()} for _ in range(rows - len(rows_values))]
        return _form_page(request, db, product_set=None, values=values, rows_values=rows_values, rows=rows)
    try:
        data = _parse(form, rows)
        with form_transaction(request, str(form.get("form_token", ""))) as tx:
            catalog.create_set(tx, data)
    except FormTokenUsed:
        return duplicate_redirect(request, "/sets")
    except (DomainError, FormTokenInvalid) as exc:
        return _form_page(
            request, db, product_set=None, values=values, rows_values=rows_values, rows=rows,
            error=error_text(current_language(request), exc), status=error_status(exc),
        )
    return saved_redirect(request, "/sets", "sets.created", name=data.name)


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
    product_set = _get_set(db, set_id)
    form = await request.form()
    more = form.get("action") == "more"
    rows = _rows_of(form, more=more)
    values = {"name": str(form.get("name", "")), "sku": str(form.get("sku", ""))}
    rows_values = _typed_rows(form, rows)
    if more:
        rows_values += [{"qty": "1", "selected": set()} for _ in range(rows - len(rows_values))]
        return _form_page(request, db, product_set=product_set, values=values, rows_values=rows_values, rows=rows)
    try:
        data = _parse(form, rows)
        with write_transaction(request) as tx:
            catalog.update_set(tx, set_id, data)
    except DomainError as exc:
        return _form_page(
            request, db, product_set=product_set, values=values, rows_values=rows_values, rows=rows,
            error=error_text(current_language(request), exc), status=error_status(exc),
        )
    return saved_redirect(request, "/sets", "sets.saved", name=data.name)


def _archive_action(action, flash_key: str):
    async def handler(request: Request, set_id: int, db: Session = Depends(get_db)):
        product_set = _get_set(db, set_id)

        def work(tx):
            action(tx, set_id)
            return {"name": product_set.name}

        return await run_form_action(
            request,
            work=work,
            success_url=f"/sets/{set_id}/edit",
            flash_key=flash_key,
            on_error=lambda text, status, exc: _edit_again(request, db, product_set, text, status),
        )

    return handler


def _edit_again(request, db, product_set, error, status):
    positions = db.scalars(
        select(SetPosition).where(SetPosition.set_id == product_set.id).order_by(SetPosition.sort, SetPosition.id)
    ).all()
    rows_values = []
    for position in positions:
        selected = set(db.scalars(select(SetPositionOption.article_id).where(SetPositionOption.position_id == position.id)))
        rows_values.append({"qty": str(position.qty), "selected": selected})
    rows = max(DEFAULT_ROWS, len(rows_values) + 2)
    rows_values += [{"qty": "1", "selected": set()} for _ in range(rows - len(rows_values))]
    return _form_page(
        request, db, product_set=product_set,
        values={"name": product_set.name, "sku": product_set.sku or ""},
        rows_values=rows_values, rows=rows, error=error, status=status,
    )


router.add_api_route(
    "/sets/{set_id:int}/archive", _archive_action(catalog.archive_set, "sets.flash_archived"),
    methods=["POST"], dependencies=[Depends(verify_csrf)],
)
router.add_api_route(
    "/sets/{set_id:int}/restore", _archive_action(catalog.restore_set, "sets.flash_restored"),
    methods=["POST"], dependencies=[Depends(verify_csrf)],
)
