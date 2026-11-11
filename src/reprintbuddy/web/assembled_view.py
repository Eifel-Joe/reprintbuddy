"""What the booking and order forms offer since articles are made of parts (0.4.0, p341).

One list per purpose, as (value, label) pairs in the order of the names:
- count: print articles and stock holders (Spec 6.2: a count per variant on stock);
- sale: the same, and every variant of an article built on order, booked from its parts (Teil C 4);
- stock in: print articles only (Teil C 3);
- order: print articles sold alone, stock holders, and the variants of articles built on order
  (Spec 6.7: every variant its own entry, "Hose · EX-H1-L32 (Side part L, Socket L32)").
A stock holder reads "Name · number"; an article without choices its name. The value of a variant
built on order is "assembled:<article id>[:<option id>...]".
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from reprintbuddy.domain import articlesets, assembled
from reprintbuddy.models import Article

Choice = tuple[str, str]


def _holder_label(row: Article) -> str:
    if row.kind == articlesets.VARIANT:
        return f"{row.name} · {row.sku}" if row.sku else row.name
    return row.name


def _sorted(choices: list[Choice]) -> list[Choice]:
    return sorted(choices, key=lambda c: (c[1].casefold(), c[0]))


def print_choices(session: Session, *, sold_alone_only: bool = False) -> list[Choice]:
    query = articlesets.print_articles()
    if sold_alone_only:
        query = query.where(Article.sold_alone.is_(True))
    return [(str(a.id), a.name) for a in session.scalars(query)]


def holder_choices(session: Session) -> list[Choice]:
    return [(str(a.id), _holder_label(a)) for a in session.scalars(articlesets.stock_holders())]


def on_order_choices(session: Session) -> list[Choice]:
    """Every variant of the active articles made of parts built on order."""
    choices = []
    for composite in session.scalars(articlesets.assembled_articles().where(Article.assembly_mode == "order")):
        row = assembled.assembled_row(session, composite.id)
        for holder in row.holders:
            value = "assembled:" + str(composite.id) + (":" + holder.key if holder.key else "")
            label = composite.name
            if holder.sku and holder.key:
                label += f" · {holder.sku}"
            if holder.names:
                label += f" ({', '.join(holder.names)})"
            choices.append((value, label))
    return choices


def count_choices(session: Session) -> list[Choice]:
    return _sorted(print_choices(session) + holder_choices(session))


def sale_choices(session: Session) -> list[Choice]:
    return _sorted(print_choices(session) + holder_choices(session) + on_order_choices(session))


def stock_in_choices(session: Session) -> list[Choice]:
    return _sorted(print_choices(session))


def order_choices(session: Session) -> list[Choice]:
    """The "Articles" group of the order form: values "article:<id>" and "assembled:<id>[:...]"."""
    plain = [(f"article:{v}", label) for v, label in print_choices(session, sold_alone_only=True) + holder_choices(session)]
    return _sorted(plain + on_order_choices(session))


def parse_assembled(value: str) -> tuple[int, str] | None:
    """(article id, variant key) of "assembled:<id>[:<option id>...]"; None for anything else."""
    kind, _, rest = value.partition(":")
    if kind != "assembled" or not rest:
        return None
    parts = rest.split(":")
    if not all(p.isdigit() and len(p) <= 10 for p in parts):
        return None
    return int(parts[0]), ":".join(parts[1:])

