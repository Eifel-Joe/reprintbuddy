"""What the end-to-end run does in the interface, one function per thing a person does there.

Each step reads the page first and sends the form found on it (`webclient.Browser.submit`), so it passes the same
CSRF check, one-time token and password confirmation as a click. A step that does not land where a person would
see success raises `StepFailed` with what the page said. English texts: the run sets the language to English.
Standard library only.
"""

from __future__ import annotations

import html
import re
import urllib.parse

from webclient import Browser, Page, forms

MAINTENANCE = "/settings?tab=maintenance"


class StepFailed(Exception):
    """A step did not end as it does for a person when everything works."""


def _path(page: Page) -> str:
    return urllib.parse.urlsplit(page.url).path


def _query(page: Page) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(page.url).query)


def visible_text(page: Page) -> str:
    """The text of the page without tags, scripts and styles, blanks folded: what a person reads."""
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", page.text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _expect(page: Page, path: str, what: str) -> Page:
    if page.status != 200 or _path(page) != path:
        raise StepFailed(f"{what}: ended on {page.url} (HTTP {page.status}): {visible_text(page)[:400]}")
    return page


def _no_message(page: Page, what: str) -> Page:
    codes = _query(page).get("ops")
    if codes:
        raise StepFailed(f"{what}: the page says ops={codes[0]}: {visible_text(page)[:400]}")
    return page


def set_up(web: Browser, user: str, password: str, bambuddy_url: str, key: str) -> None:
    """The first start: the admin account, then Bambuddy (the stand-in)."""
    page = web.get("/setup")
    page = web.submit(page, "/setup", {"username": user, "password": password, "password_repeat": password, "language": "en"})
    if not _path(page).startswith("/setup/bambuddy"):
        raise StepFailed(f"account: ended on {page.url}: {visible_text(page)[:400]}")
    page = web.get("/setup/bambuddy/connect")
    page = web.submit(page, "/setup/bambuddy/connect", {"bambuddy_address": bambuddy_url, "bambuddy_token": key})
    if _path(page) != "/setup/bambuddy/done":
        raise StepFailed(f"Bambuddy: ended on {page.url}: {visible_text(page)[:400]}")


def settings_reachable(web: Browser) -> bool:
    return _path(web.get("/settings")) == "/settings"


def log_in(web: Browser, user: str, password: str) -> None:
    page = web.get("/login")
    page = web.submit(page, "/login", {"username": user, "password": password})
    if page.status != 200 or _path(page) == "/login":
        raise StepFailed(f"log in: ended on {page.url}: {visible_text(page)[:300]}")


def add_part(web: Browser, name: str) -> None:
    page = web.get("/parts/new")
    page = web.submit(page, "/parts/new", {"name": name})
    _expect(page, "/parts", f"add part {name}")


def has_part(web: Browser, name: str) -> bool:
    return name in visible_text(_expect(web.get("/parts"), "/parts", "list of parts"))


def download_backup(web: Browser, *, login_password: str, backup_password: str) -> bytes:
    page = web.get(MAINTENANCE)
    answer = web.submit(
        page, "/settings/backup",
        {"backup_password": backup_password, "backup_password_repeat": backup_password, "login_password": login_password},
    )
    if answer.status != 200 or not answer.body.startswith(b"PK"):
        raise StepFailed(f"backup: no file, ended on {answer.url}: {visible_text(answer)[:300]}")
    return answer.body


def restore(web: Browser, backup: bytes, *, backup_password: str, login_password: str) -> Page:
    """Upload a backup and confirm it. Returns the page after the confirmation (it asks for the restart)."""
    page = _no_message(web.get(MAINTENANCE), "settings")
    page = web.submit(page, "/settings/restore/upload", files={"backup_file": ("backup.zip", backup)})
    page = _no_message(_expect(page, "/settings/restore", "upload of the backup"), "upload of the backup")
    done = web.submit(page, "/settings/restore", {"backup_password": backup_password, "login_password": login_password})
    # the answer is the page "Backup restored" itself (it then restarts the service or asks for the restart)
    if done.status != 200 or "ops" in _query(done) or "Backup restored" not in visible_text(done):
        raise StepFailed(f"restore: ended on {done.url} (HTTP {done.status}): {visible_text(done)[:400]}")
    return done


def log_text(web: Browser) -> str:
    return visible_text(_expect(web.get("/settings?tab=log"), "/settings", "log"))


def update_status(web: Browser) -> dict[str, object]:
    page = web.get("/settings/update/status")
    if page.status != 200:
        raise StepFailed(f"update status: HTTP {page.status}")
    found = page.json()
    assert isinstance(found, dict)
    return found


# --- update and way back: these need the installation and the update helper, so they run only in the CI job ---


def maintenance(web: Browser) -> Page:
    return _expect(web.get(MAINTENANCE), "/settings", "maintenance tab")


def include_prereleases(web: Browser) -> Page:
    """The switch "Include pre-releases": saved, and a check for new versions follows at once."""
    page = web.submit(maintenance(web), "/settings/update/channel", {"enabled": "1"})
    return _no_message(_expect(page, "/settings", "pre-releases"), "pre-releases")


def check_for_updates(web: Browser) -> Page:
    page = web.submit(maintenance(web), "/settings/update/check")
    return _no_message(_expect(page, "/settings", "check for updates"), "check for updates")


def start_update(web: Browser) -> Page:
    page = web.submit(maintenance(web), "/settings/update")
    return _no_message(_expect(page, "/settings", "start of the update"), "start of the update")


def check_way_back(web: Browser) -> Page:
    """The button "Back to the stable version": loads and checks the stable package, shows the confirmation page."""
    page = web.submit(maintenance(web), "/settings/update/return/check")
    return _no_message(_expect(page, "/settings/update/return", "check of the way back"), "check of the way back")


def confirm_way_back(web: Browser, page: Page, *, login_password: str) -> tuple[int, Page]:
    """Confirm the way back the page shows; case 3 asks for the password again. Returns (case, page)."""
    form = page.form("/settings/update/return")
    case = int(dict(form.fields).get("case", "0"))
    values = {"login_password": login_password} if case == 3 else {}
    done = web.submit(page, "/settings/update/return", values)
    _no_message(_expect(done, "/settings", "start of the way back"), "start of the way back")
    return case, done


def dismiss_card(web: Browser) -> None:
    page = maintenance(web)
    if any(urllib.parse.urlsplit(form.action).path == "/settings/update/dismiss" for form in forms(page.text)):
        web.submit(page, "/settings/update/dismiss")
