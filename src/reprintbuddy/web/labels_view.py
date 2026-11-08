"""What the web layer needs for labels (Spec 8.5): the details of an article as label data, the
picture, the saved settings, and the words for what happened at the printer.

The label carries the address of the article's QR code (Spec 8.4), so it follows the address
rules of `qr.base_address`.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Iterable
from datetime import datetime

from fastapi import Request
from sqlalchemy.orm import Session

from reprintbuddy import settings as st
from reprintbuddy.domain import catalog, rules
from reprintbuddy.labels import fields, render, status
from reprintbuddy.labels.errors import LabelError
from reprintbuddy.labels.rolls import DEFAULT_ROLL, ROLLS, Roll, roll_by_key
from reprintbuddy.labels.service import MAX_COPIES, Outcome
from reprintbuddy.models import Article
from reprintbuddy.web import netaddr, qr
from reprintbuddy.web.formatting import LOCAL_ZONE, format_date
from reprintbuddy.web.i18n import translate

SAMPLE_ARTICLE_ID = 1  # the code on a sample label points at an article address like any other


def caption_for(lang: str) -> Callable[[str], str]:
    """The short word before a number on the label, in the interface language."""
    return lambda key: translate(lang, f"labels.cap.{key}")


def today_text(lang: str) -> str:
    return format_date(datetime.now(LOCAL_ZONE).date(), lang)


def target_text(lang: str, article: Article) -> str | None:
    """The print target as the article's page shows it; None when it has none."""
    value = catalog.target_value(article.target_model, article.printer_id)
    if value == "":
        return None
    if value.startswith("model:"):
        return translate(lang, "articles.target_any", model=article.target_model)
    return translate(lang, "articles.target_printer_id", id=article.printer_id)


def article_data(article: Article, lang: str) -> fields.LabelData:
    return fields.LabelData(
        name=article.name,
        sku=article.sku,
        min_stock=article.min_stock,
        buffer=article.buffer,
        target_qty=rules.target_quantity(article.min_stock, article.buffer),
        units_per_print=article.units_per_print,
        print_target=target_text(lang, article),
        label_text=article.label_text,
        date=today_text(lang),
    )


def sample_data(lang: str) -> fields.LabelData:
    return fields.sample(date=today_text(lang), name=translate(lang, "labels.sample_name"))


class LabelSettings:
    """The saved label settings, read once."""

    def __init__(self, db: Session) -> None:
        self.host: str | None = st.get_setting(db, "label_printer_host") or None  # type: ignore[assignment]
        self.roll: Roll = roll_by_key(st.get_setting(db, "label_roll")) or ROLLS[DEFAULT_ROLL]
        self.fields: tuple[str, ...] = fields.clean(st.get_setting(db, "label_fields"))


def own_addresses(request: Request) -> frozenset[str]:
    """The addresses this server has in the network (the ones the QR codes use): the printer is another
    device, so none of them is accepted as the printer's address."""
    effective = getattr(getattr(request.app.state, "ops", None), "effective", None)
    found = (
        netaddr.usable(effective.host) if effective is not None else None,
        netaddr.connection_address(request.scope.get("server")),
        netaddr.machine_address(),
    )
    return frozenset(item for item in found if item)


def label_address(request: Request, db: Session, article_id: int) -> str:
    """The address the QR code on the label carries (read from the database: in the request's thread)."""
    return qr.article_address(qr.base_address(request, db), article_id)


def draw(lang: str, data: fields.LabelData, chosen: Iterable[str], address: str, roll: Roll) -> render.Rendered:
    """The picture of a label. Drawing takes tens to hundreds of milliseconds with long texts: the
    routes call this in a worker thread (it touches no database and no request)."""
    return render.render_label(data, chosen, address, roll, caption_for(lang))


def png(image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def roll_text(lang: str, width: int, length: int, continuous: bool) -> str:
    if continuous:
        return translate(lang, "labels.roll.continuous", width=width)
    return translate(lang, "labels.roll.die_cut", width=width, length=length)


def problem_message(lang: str, problem: status.Problem, roll: Roll) -> tuple[str, dict[str, object]]:
    """The translation key and parameters of a printer that is not ready."""
    if problem.key != "wrong_roll":
        return f"labels.problem.{problem.key}", {}
    p = problem.params
    inserted = roll_text(lang, int(p["width"]), int(p["length"]), bool(p["continuous"]))  # type: ignore[arg-type]
    expected = roll_text(lang, roll.width_mm, roll.length_mm, False)
    return "labels.problem.wrong_roll", {"inserted": inserted, "expected": expected}


def outcome_message(lang: str, outcome: Outcome, roll: Roll) -> tuple[str, dict[str, object], bool]:
    """What to tell after a print: (translation key, parameters, is it trouble?)."""
    if outcome.problem is not None:
        key, params = problem_message(lang, outcome.problem, roll)
        return key, params, True
    if outcome.error is not None:
        reason = _error_text(lang, outcome.error)
        if outcome.printed:
            return "labels.partial", {"printed": outcome.printed, "requested": outcome.requested, "reason": reason}, True
        return "labels.failed", {"reason": reason}, True
    if outcome.printed == 1:
        return "labels.sent_one", {}, False
    return "labels.sent_many", {"count": outcome.printed}, False


def _error_text(lang: str, error: LabelError) -> str:
    return translate(lang, error.key)


def settings_context(values: dict[str, object]) -> dict[str, object]:
    """What the "Labels" tab of the settings page needs beside the form values: the choices and the
    details the preview starts with (the saved choice, or what was typed when the form is shown again).
    The page draws nothing itself: the preview picture is a request of its own, and the note about a
    cut line is shown by `labels.js` from that request's answer."""
    selected = fields.clean(values.get("label_selected") or ())
    return {
        "label_rolls": list(ROLLS.values()),
        "label_all_fields": fields.FIELDS,
        "label_preview_fields": ",".join(selected),
    }


def article_card_context(db: Session) -> dict[str, object]:
    """What the card "Label" on the article's page needs: whether a printer is set up and the greatest number."""
    return {"label_configured": LabelSettings(db).host is not None, "label_max_copies": MAX_COPIES}
