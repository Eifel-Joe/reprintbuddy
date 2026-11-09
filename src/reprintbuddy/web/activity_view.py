"""The tab "Log" of the settings page (Spec 10.7): newest first, filters by group and by who, pages
like the journal. Every text comes from the language files; a line holds only ids, field names,
fixed codes and cleaned values (activity/values.py)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from reprintbuddy.activity.kinds import ACTORS, AREAS
from reprintbuddy.i18n import load_locales, translate
from reprintbuddy.models import ActivityEntry
from reprintbuddy.web.formatting import format_local

PAGE_SIZE = 100
TAB = "log"

# Only these codes have texts; anything else (an older or newer version wrote it) shows as unknown.
_UPDATE_RESULTS = frozenset({"done", "failed", "rolled_back", "rollback_failed", "interrupted"})
_UPLOAD_RESULTS = frozenset({"uploaded", "rejected", "unclear"})
_LABEL_RESULTS = frozenset({"printed", "partial", "not_ready", "failed"})
_CONNECTION = frozenset({"ok", "unreachable", "refused", "missing"})


@dataclass
class Row:
    at: datetime
    area_label: str
    text: str
    actor: str
    address: str | None
    extras: list[str] = field(default_factory=list)
    link: str | None = None


def _t(lang: str, key: str, **params: object) -> str:
    """Like `translate`, but a key nobody knows (an unknown code) says "unknown" instead of failing."""
    known = load_locales().get("en", {})
    if key not in known:
        return translate(lang, "activity.unknown")
    return translate(lang, key, **params)


def _value(lang: str, value: object) -> str:
    if value is None:
        return translate(lang, "activity.none")
    if value is True:
        return translate(lang, "activity.on")
    if value is False:
        return translate(lang, "activity.off")
    if value in ("en", "de"):
        return translate(lang, f"activity.language.{value}")
    return str(value)


def _setting_line(lang: str, change: dict[str, Any]) -> str:
    label = _t(lang, f"activity.setting.{change.get('setting')}")
    if "change" in change:
        return f"{label}: {_t(lang, 'activity.change.' + str(change['change']))}"
    return f"{label}: {_value(lang, change.get('old'))} → {_value(lang, change.get('new'))}"


def _describe(lang: str, entry: ActivityEntry) -> tuple[str, list[str], str | None]:
    """(text, extra lines, link) of one line."""
    details: dict[str, Any] = dict(entry.details or {})
    kind = entry.kind
    params: dict[str, object] = {"name": details.get("name") or "–", "version": details.get("version") or "–"}
    extras: list[str] = []
    link = None
    if kind == "article_auto":
        params["state"] = translate(lang, "activity.on" if details.get("on") else "activity.off")
    if kind.startswith("article_") and isinstance(details.get("id"), int):
        link = f"/articles/{details['id']}"
    elif kind.startswith("part_") and isinstance(details.get("id"), int):
        link = f"/parts/{details['id']}/edit"
    elif kind.startswith("set_") and isinstance(details.get("id"), int):
        link = f"/sets/{details['id']}/edit"
    if details.get("fields"):
        names = ", ".join(_t(lang, f"activity.field.{name}") for name in details["fields"])
        extras.append(translate(lang, "activity.fields", fields=names))
    if kind == "settings_changed":
        extras += [_setting_line(lang, change) for change in details.get("settings", []) if isinstance(change, dict)]
        if details.get("owner"):
            extras.append(translate(lang, "activity.owner", owner=details["owner"]))
    elif kind == "login_failed":
        count = int(details.get("count") or 1)
        last = details.get("last")
        try:
            last_text = format_local(datetime.fromisoformat(str(last)), lang) if last else ""
        except ValueError:
            last_text = ""
        if count > 1:
            extras.append(translate(lang, "activity.failed_count", count=count, last=last_text))
    elif kind == "backup_downloaded":
        extras.append(translate(lang, "activity.encrypted" if details.get("encrypted") else "activity.not_encrypted"))
    elif kind == "restore_rejected":
        extras.append(_t(lang, f"activity.reason.{details.get('reason')}"))
    elif kind == "restore_done":
        try:
            made = format_local(datetime.fromisoformat(str(details.get("created_at"))), lang)
        except ValueError:
            made = "–"
        extras.append(translate(lang, "activity.backup_of", date=made, version=details.get("version") or "–"))
    elif kind in ("update_started", "update_result"):
        extras.append(f"{details.get('from') or '–'} → {details.get('to') or '–'}")
        if kind == "update_result":
            result = details.get("result")
            extras.append(_t(lang, f"activity.update.{result}") if result in _UPDATE_RESULTS else translate(lang, "activity.unknown"))
    elif kind in ("bambuddy_connection", "bambuddy_rights"):
        state = details.get("state")
        extras.append(_t(lang, f"activity.state.{state}") if state in _CONNECTION else translate(lang, "activity.unknown"))
        if details.get("missing"):
            extras.append(", ".join(str(right) for right in details["missing"]))
    elif kind == "upload":
        result = details.get("result")
        extras.append(translate(lang, "activity.file_in", name=details.get("name") or "–",
                                folder=details.get("folder") or translate(lang, "activity.top_level")))
        extras.append(_t(lang, f"activity.upload.{result}") if result in _UPLOAD_RESULTS else translate(lang, "activity.unknown"))
    elif kind in ("message_sent", "message_test"):
        if kind == "message_sent":
            event = _t(lang, f"notify.event.{details.get('event')}")
            extras.append(translate(lang, "activity.message_of", event=event, count=details.get("count") or 1))
        extras.append(_t(lang, f"notify.outcome.{details.get('outcome')}"))
    elif kind in ("label_printed", "label_test"):
        result = details.get("result")
        extras.append(translate(lang, "activity.labels_printed", printed=details.get("printed", 0),
                                requested=details.get("requested", 0)))
        extras.append(_t(lang, f"activity.label.{result}") if result in _LABEL_RESULTS else translate(lang, "activity.unknown"))
    return _t(lang, f"activity.kind.{kind}", **params), extras, link


def rows(lang: str, entries: list[ActivityEntry]) -> list[Row]:
    found = []
    for entry in entries:
        text, extras, link = _describe(lang, entry)
        actor = _t(lang, f"activity.actor.{entry.actor}")
        if entry.via == "cli":
            actor = f"{actor} ({translate(lang, 'activity.via_cli')})"
        address = entry.address if entry.area == "login" else None
        found.append(Row(entry.at, _t(lang, f"activity.area.{entry.area}"), text, actor, address, extras, link))
    return found


def _chosen(raw: str | None, allowed: tuple[str, ...]) -> str | None:
    return raw if raw in allowed else None


def _href(area: str | None, actor: str | None, page: int = 1) -> str:
    query = [("tab", TAB)]
    if area:
        query.append(("area", area))
    if actor:
        query.append(("actor", actor))
    if page > 1:
        query.append(("page", str(page)))
    return "/settings?" + urlencode(query)


def context(request: Request, db: Session, lang: str) -> dict[str, object]:
    """What the tab needs: rows of the page, filter links with counts, pages."""
    params = request.query_params
    area = _chosen(params.get("area"), AREAS)
    actor = _chosen(params.get("actor"), ACTORS)
    raw_page = params.get("page") or "1"
    page = int(raw_page) if raw_page.isascii() and raw_page.isdigit() and 0 < int(raw_page) < 100_000 else 1

    def where(query, *, by_area: bool = True, by_actor: bool = True):
        if by_area and area:
            query = query.where(ActivityEntry.area == area)
        if by_actor and actor:
            query = query.where(ActivityEntry.actor == actor)
        return query

    by_area = dict(db.execute(where(select(ActivityEntry.area, func.count()), by_area=False).group_by(ActivityEntry.area)).all())
    by_actor = dict(db.execute(where(select(ActivityEntry.actor, func.count()), by_actor=False).group_by(ActivityEntry.actor)).all())
    entries = list(
        db.scalars(
            where(select(ActivityEntry))
            .order_by(ActivityEntry.at.desc(), ActivityEntry.id.desc())
            .limit(PAGE_SIZE + 1)
            .offset((page - 1) * PAGE_SIZE)
        )
    )
    area_links = [{"label": translate(lang, "list.filter_all"), "count": sum(by_area.values()),
                   "href": _href(None, actor), "active": area is None}]
    area_links += [
        {"label": translate(lang, f"activity.area.{name}"), "count": by_area.get(name, 0),
         "href": _href(name, actor), "active": area == name}
        for name in AREAS if by_area.get(name) or area == name
    ]
    actor_links = [{"label": translate(lang, "activity.all_actors"), "count": sum(by_actor.values()),
                    "href": _href(area, None), "active": actor is None}]
    actor_links += [
        {"label": translate(lang, f"activity.actor.{name}"), "count": by_actor.get(name, 0),
         "href": _href(area, name), "active": actor == name}
        for name in ACTORS if by_actor.get(name) or actor == name
    ]
    return {
        "log_rows": rows(lang, entries[:PAGE_SIZE]),
        "log_area_links": area_links,
        "log_actor_links": actor_links,
        "log_newer": _href(area, actor, page - 1) if page > 1 else None,
        "log_older": _href(area, actor, page + 1) if len(entries) > PAGE_SIZE else None,
        "log_filtered": bool(area or actor),
    }
