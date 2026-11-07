"""The engine's warnings as the pages show them (Spec 7.7, 10.2; plan B4 Task 12).

The queue page and the overview render them with `_warnings.html`; B7 takes both over.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request
from sqlalchemy.orm import Session

from reprintbuddy import settings as st
from reprintbuddy.domain.engine import collect_warnings, warning_text

# Red: Bambuddy does not answer or refuses, or a hand-over is unclear (the answer is not
# known). Everything else is yellow: a state the user chose or that waits for the user.
BAD_WARNINGS: frozenset[str] = frozenset(
    {
        "warning.bambuddy_unreachable",
        "warning.bambuddy_rejected",
        "warning.poll_rejected",
        "warning.unclear_prints",
        "warning.resolve_rejected",
        "warning.resolve_no_user",
        "warning.duplicates",
    }
)


@dataclass(frozen=True)
class ShownWarning:
    text: str
    tone: str  # "bad" or "warn"


def warnings_context(request: Request, session: Session) -> dict[str, object]:
    """`warnings` and `last_poll_at` for `_warnings.html`.

    `last_poll_at` is the end of the last regular poll that Bambuddy did not refuse
    (EngineStatus); the template says "last successful query".
    """
    status = request.app.state.ticker.status
    lang = str(st.get_setting(session, "language"))
    shown = [
        ShownWarning(warning_text(lang, warning), "bad" if warning.key in BAD_WARNINGS else "warn")
        for warning in collect_warnings(session, status)
    ]
    return {"warnings": shown, "last_poll_at": status.last_poll_at}
