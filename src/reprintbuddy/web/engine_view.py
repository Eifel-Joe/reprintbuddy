"""The engine's warnings as the pages show them (Spec 7.7, 10.2; plan B4 Task 12).

The queue page and the overview render them with `_warnings.html`; B7 takes both over.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request
from sqlalchemy.orm import Session

from reprintbuddy import settings as st
from reprintbuddy.domain.engine import EngineWarning, collect_warnings, warning_text

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
    # The way on from a paused article (p49): `action` is "resume" (button), "confirm_file",
    # "relink" or "edit" (links to where the pause is settled); None for every other warning.
    article_id: int | None = None
    action: str | None = None


def pause_action(reason: object, has_target: object) -> str:
    """What the notice offers for a pause. Follows `pauses.lift_pause`: a file pause and a missing
    print target refuse lifting, so they link to where they are settled instead of a button."""
    if reason == "file_changed":
        return "confirm_file"
    if reason == "file_missing":
        return "relink"
    if reason == "no_target" and not has_target:
        return "edit"
    return "resume"


def _shown(lang: str, warning: EngineWarning) -> ShownWarning:
    tone = "bad" if warning.key in BAD_WARNINGS else "warn"
    text = warning_text(lang, warning)
    if warning.key != "warning.article_paused":
        return ShownWarning(text, tone)
    action = pause_action(warning.params.get("reason_key"), warning.params.get("has_target"))
    return ShownWarning(text, tone, article_id=int(warning.params["article_id"]), action=action)  # type: ignore[call-overload]


def warnings_context(request: Request, session: Session) -> dict[str, object]:
    """`warnings` and `last_poll_at` for `_warnings.html`.

    `last_poll_at` is the end of the last regular poll that Bambuddy did not refuse
    (EngineStatus); the template says "last successful query".
    """
    status = request.app.state.ticker.status
    lang = str(st.get_setting(session, "language"))
    shown = [_shown(lang, warning) for warning in collect_warnings(session, status)]
    return {"warnings": shown, "last_poll_at": status.last_poll_at}
