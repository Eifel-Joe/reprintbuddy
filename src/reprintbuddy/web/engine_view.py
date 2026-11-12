"""The engine's warnings as the pages show them (Spec 7.7, 10.2; plan B4 Task 12).

The queue page and the overview render them with `_warnings.html`; B7 takes both over.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request
from sqlalchemy.orm import Session

from reprintbuddy import settings as st
from reprintbuddy.domain import catalog
from reprintbuddy.domain.engine import EngineWarning, collect_warnings, warning_text
from reprintbuddy.i18n import translate
from reprintbuddy.web import notify_view

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
    set_id: int | None = None  # a set that holds something archived: the notice links to its form (0.4.3)


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


def warnings_context(request: Request, session: Session, *, sets: bool = False) -> dict[str, object]:
    """`warnings` and `last_poll_at` for `_warnings.html`.

    `last_poll_at` is the end of the last regular poll that Bambuddy did not refuse
    (EngineStatus); the template says "last successful query". With `sets` (the overview) the
    warnings about archived parts in active sets follow (Spec 5.4 Altfall, 0.4.3); they are not the
    engine's, so the queue page and the push messages leave them out.
    """
    status = request.app.state.ticker.status
    lang = str(st.get_setting(session, "language"))
    shown = [_shown(lang, warning) for warning in collect_warnings(session, status)]
    # Spec 10.2, 10.6: the last push message did not go through. Red like Bambuddy not answering;
    # it lives in the notifier, not in the engine's list (`collect_warnings` stays the engine's).
    problem = notify_view.problem(request)
    if problem == "token":  # p152: nothing is tried any more, the person has to act (Spec 10.6)
        shown.append(ShownWarning(translate(lang, "warning.ha_token_rejected"), "bad"))
    elif problem is not None:
        reason = translate(lang, f"notify.outcome.{problem}")
        shown.append(ShownWarning(translate(lang, "warning.ha_unreachable", reason=reason), "bad"))
    if sets:
        for held in catalog.archived_in_sets(session):
            key = "sets.holds_archived_set" if held.kind == "set" else "sets.holds_archived_article"
            shown.append(ShownWarning(translate(lang, key, set=held.set_name, name=held.name), "warn", set_id=held.set_id))
        # sets the update to 0.4.4 left out of the order form (plan v0.4.4, N1); no push, like the Altfall
        mend_keys = {"number_missing": "sets.mend.number_missing", "twice": "sets.mend.twice", "broken": "sets.mend.broken"}
        for mend in catalog.sets_to_mend(session):
            text = translate(lang, mend_keys[mend.kind], set=mend.set_name, name=mend.name or "")
            shown.append(ShownWarning(text, "warn", set_id=mend.set_id))
    return {"warnings": shown, "last_poll_at": status.last_poll_at}
