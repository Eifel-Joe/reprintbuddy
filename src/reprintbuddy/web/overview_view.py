"""The numbers of the overview (Spec 10.2): the state of the Bambuddy connection and the key figures.

Nothing is computed twice: the figures count the rows of `catalog.list_articles` (status after
the rules of Spec 10.1, coverage after 7.1), the prints of `workqueue` and the suggestions of
`reorder.count_suggestions`, the same functions the article list, the queue and the reorder page use.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from reprintbuddy.domain.engine import EngineStatus
from reprintbuddy.web.i18n import translate
from reprintbuddy.web.kpis import Kpi

# "Below minimum stock" means the red states, as Spec 10.6 (event 4) uses the term: "low" (yellow)
# is below the minimum too, but its reprint is already planned.
RED_STATES = ("below_min", "overbooked")


class _Row(Protocol):  # the fields of catalog.ArticleRow the figures read
    status: str
    provisional: int
    awaiting_assembly: int


def connection_state(status: EngineStatus) -> tuple[str, str]:
    """(state, tone) of the connection to Bambuddy, from the same fields as the warnings.

    Not answering is checked before a refused query: the engine keeps an old refusal while
    Bambuddy cannot be reached, and "not reachable" is the newer fact then.
    """
    if not status.configured:
        return "not_configured", "neutral"
    if status.reachable is False:
        return "unreachable", "bad"
    if status.poll_error:
        return "rejected", "bad"
    if status.reachable is None:  # set up, but no regular query has run since the start
        return "unknown", "neutral"
    return "connected", "ok"


def figures(
    lang: str, rows: Sequence[_Row], *, queued_prints: int, open_prints: int, suggestions: int
) -> list[Kpi]:
    """The four key figures of Spec 10.2, always all four (a 0 is the news here).

    `queued_prints`: prints in Reprintbuddy's queue, held entries included (they still count to
    the coverage); `open_prints`: prints handed over to Bambuddy and not finished.
    """

    def t(key: str, **params: object) -> str:
        return translate(lang, key, **params)

    red = sum(1 for r in rows if r.status in RED_STATES)
    overbooked = sum(1 for r in rows if r.status == "overbooked")
    planned = queued_prints + open_prints
    check = sum(r.provisional for r in rows)
    assembly = sum(r.awaiting_assembly for r in rows)
    return [
        Kpi(
            icon="triangle-alert", tone="bad" if red else "ok", value=red, label=t("overview.kpi.below_min"),
            note=t("overview.kpi.overbooked_note", n=overbooked) if overbooked else "", href="/articles",
        ),
        Kpi(
            icon="printer", tone="accent", value=planned, label=t("overview.kpi.planned"),
            note=t("overview.kpi.planned_note", queued=queued_prints, open=open_prints) if planned else "",
            href="/queue",
        ),
        Kpi(
            icon="clipboard-check", tone="warn" if check + assembly else "neutral", value=check + assembly,
            label=t("overview.kpi.to_check"),
            note=t("overview.kpi.to_check_note", check=check, assembly=assembly) if check + assembly else "",
            href="/inspection",
        ),
        Kpi(
            icon="shopping-bag", tone="warn" if suggestions else "neutral", value=suggestions,
            label=t("overview.kpi.reorder"), href="/reorder",
        ),
    ]
