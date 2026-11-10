"""The FastAPI application."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import httpx
from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.convertors import Convertor, register_url_convertor
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from reprintbuddy import __version__
from reprintbuddy import settings as st
from reprintbuddy.activity import states as activity_states
from reprintbuddy.activity.writer import (
    ActivityLog,
    read_restored_marker,
    restore_marks,
    restore_notify_marks,
)
from reprintbuddy.bambuddy.client import BambuddyUnavailable
from reprintbuddy.bambuddy.factory import make_http_factory
from reprintbuddy.bambuddy.live import LiveListener
from reprintbuddy.changes import install_change_feed
from reprintbuddy.db import make_engine, make_sessionmaker, upgrade
from reprintbuddy.domain import filament, status
from reprintbuddy.domain.engine import Engine
from reprintbuddy.notify.events import Rights
from reprintbuddy.notify.service import Notifier
from reprintbuddy.ops import fullbackup
from reprintbuddy.ops.autocheck import AutoCheck
from reprintbuddy.ops.context import OpsContext
from reprintbuddy.ops.releasenotes import with_notes
from reprintbuddy.ops.running import update_just_started, update_running
from reprintbuddy.ops.status import is_busy, read_status
from reprintbuddy.ops.versions import VersionInfo, check_versions, is_git_install
from reprintbuddy.web import deps
from reprintbuddy.web import qr as qr_view
from reprintbuddy.web.bodylimit import MULTIPART_OVERHEAD, BodyLimitMiddleware
from reprintbuddy.web.securecookies import SecureCookiesOnHttps
from reprintbuddy.web.subpath import SubPathWarning
from reprintbuddy.web.flash import pop_flashes
from reprintbuddy.web.formatting import decimal, fill_percent, local_date, local_day, local_dt, unit
from reprintbuddy.web.notes_markdown import render_notes
from reprintbuddy.web.formtoken import issue_form_token
from reprintbuddy.web.i18n import load_locales
from reprintbuddy.web.live import LIVE_CHANNEL_TOPIC, LiveBroker
from reprintbuddy.web.preload import PreloadedLoader
from reprintbuddy.web.rights import RightsMonitor
from reprintbuddy.web.throttle import FailureBrake, HashLimiter
from reprintbuddy.web.routes import articles as articles_routes
from reprintbuddy.web.routes import upload as upload_routes
from reprintbuddy.web.routes import auth as auth_routes
from reprintbuddy.web.routes import backup as backup_routes
from reprintbuddy.web.routes import bookings as bookings_routes
from reprintbuddy.web.routes import inspection as inspection_routes
from reprintbuddy.web.routes import journal as journal_routes
from reprintbuddy.web.routes import live as live_routes
from reprintbuddy.web.routes import orders as orders_routes
from reprintbuddy.web.routes import ops as ops_routes
from reprintbuddy.web.routes import overview as overview_routes
from reprintbuddy.web.routes import rights as rights_routes
from reprintbuddy.web.routes import notify as notify_routes
from reprintbuddy.web.routes import filament as filament_routes
from reprintbuddy.web.routes import reorder as reorder_routes
from reprintbuddy.web.routes import parts as parts_routes
from reprintbuddy.web.routes import labels as labels_routes
from reprintbuddy.web.routes import qr as qr_routes
from reprintbuddy.web.routes import queue as queue_routes
from reprintbuddy.web.routes import settings as settings_routes
from reprintbuddy.web.routes import sets as sets_routes
from reprintbuddy.web.routes import setup as setup_routes
from reprintbuddy.web.routes import ui as ui_routes

log = logging.getLogger("reprintbuddy.app")
WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"


class BoundedInt(Convertor[int]):
    """`{id:int}` in a path: at most 18 digits. Longer numbers name nothing that exists (404),
    instead of raising in `int()` (above 4300 digits) or in SQLite (from 2**63)."""

    regex = "[0-9]{1,18}"

    def convert(self, value: str) -> int:
        return int(value)

    def to_string(self, value: int) -> str:
        return str(int(value))


register_url_convertor("int", BoundedInt())
SESSION_COOKIE = "reprintbuddy_session"

# Plan B4 decision 13: after every successful write the engine ticks once (rule 7.1 and
# hand-over), so every booking of B3, B5 and B6 is covered without touching its route.
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Signing in and out, the first-run setup and the operations of the server itself change
# no stock and no queue.
TICK_EXEMPT_PREFIXES = (
    "/login",
    "/logout",
    "/setup",
    "/settings/restart",
    "/settings/update",
    "/settings/backup",
    "/settings/restore",
)


async def tick_after_write(request: Request, call_next) -> Response:
    """Ask the tick engine for a tick after a write that was not refused (status < 400).

    A form answered with 200 and a message (a refused booking) asks too: the tick then
    finds nothing to do. It only wakes the background loop; the answer does not wait.
    """
    response = await call_next(request)
    if (
        request.method in WRITE_METHODS
        and response.status_code < 400
        and not request.url.path.startswith(TICK_EXEMPT_PREFIXES)
    ):
        request.app.state.ticker.request_tick()
    return response


def _poll_seconds(app: FastAPI) -> float:
    """The poll interval, read from the settings at every regular tick (Spec 7.6)."""
    with app.state.sessionmaker() as session:
        return 60.0 * int(st.get_setting(session, "poll_minutes"))


class _LiveClient:
    """What the live listener asks of a client, answered by the client of the moment.

    The app's `bambuddy_factory` is asked afresh on each call, so changed settings take effect
    at the next connect. No Bambuddy set up (the factory gives None), or a client without a
    live channel, counts as "unavailable": the listener pauses and tries later.
    """

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    def _channel(self, method: str) -> Any:
        client = deps.bambuddy_client(self._app)
        if client is None or not hasattr(client, method):
            raise BambuddyUnavailable("no Bambuddy live channel available")
        return client

    async def ws_token(self) -> str:
        return await self._channel("ws_token").ws_token()

    def ws_url(self, token: str) -> str:
        return self._channel("ws_url").ws_url(token)


def _install_auto_check(app: FastAPI) -> None:
    """The automatic check for new versions (v0.3.6, Spec 11.3a), started with the app's loop.

    A good result goes where the button "Check for updates" puts its own (`version_info`,
    `installed_tag`), so there is one state. The settings switch sets `update_check_wake`,
    which ends the pause at once: switched on, the next round is now.
    """
    wake = asyncio.Event()
    app.state.update_check_wake = wake

    def enabled() -> bool:
        with app.state.sessionmaker() as session:
            return bool(st.get_setting(session, "update_check"))

    def store(info: VersionInfo) -> None:
        ops_routes.store_versions(app.state, info, actor="system")

    async def wait(delay: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(wake.wait(), delay)
        wake.clear()

    def check() -> VersionInfo:  # blocking (git, GitHub); `AutoCheck` runs it in a thread
        ops = app.state.ops
        info = check_versions(ops.install_dir, ops.runner, now=datetime.now(UTC))
        return with_notes(info, ops.install_dir, ops.runner, ops.http_get)  # p143: same round

    app.state.auto_check = AutoCheck(
        enabled=enabled,
        is_install=lambda: is_git_install(app.state.ops.install_dir),
        busy=lambda: app.state.update_starting
        or update_running(app.state.db_path.parent, app.state.ops.runner, datetime.now(UTC)),
        check=check,
        store=store,
        wait=wait,
    )


def _install_activity_log(app: FastAPI, sessionmaker, db_path: Path) -> None:
    """The writer of the activity log (Spec 10.7). Its runs count as writes in flight, like a request:
    a restore waits for a run in its worker thread, and while a restore or an update runs, nothing is
    written (the queue keeps it). Decided and counted in the loop, with no `await` in between."""

    def may_write() -> bool:
        now = datetime.now(UTC)
        return not (
            app.state.restore.active
            or app.state.update_starting
            or update_just_started(db_path.parent, now)
            or is_busy(read_status(db_path.parent), now)  # for the whole update (review v0.3.13 C, finding 3)
        )

    def begin_write() -> None:
        app.state.writes_in_flight += 1

    def end_write() -> None:
        app.state.writes_in_flight -= 1

    app.state.activity = ActivityLog(
        sessionmaker,
        may_write=may_write,
        begin_write=begin_write,
        end_write=end_write,
        update_result=lambda: activity_states.update_outcome(read_status(db_path.parent)),
    )


def _log_bambuddy(app: FastAPI, poll: bool) -> None:
    """After a regular poll: the connection to Bambuddy and its rights, as changes only (Spec 10.7,
    group 5). Only queued here: the tick never waits for the log."""
    if not poll:
        return
    activity: ActivityLog = app.state.activity
    connection = activity_states.connection_state(app.state.ticker.status)
    if connection is not None:
        activity.transition("bambuddy.connection", connection, "bambuddy_connection", "bambuddy", {"state": connection})
    rights = activity_states.rights_state(app.state.rights)
    if rights is not None:
        details: dict[str, object] = {"state": rights}
        if rights == "missing" and app.state.rights.check is not None:
            details["missing"] = sorted(app.state.rights.check.missing)
        activity.transition("bambuddy.rights", rights, "bambuddy_rights", "bambuddy", details)


def _log_start(app: FastAPI) -> None:
    """At every start: "Reprintbuddy started", and a restore the last process finished (Spec 10.7)."""
    activity: ActivityLog = app.state.activity
    restored = read_restored_marker(app.state.db_path.parent)
    if restored is not None:
        via = restored.pop("via", None)
        marks = restored.pop("marks", None)
        if marks is not None:  # p186: before anything is logged into the restored database
            restore_marks(app.state.sessionmaker, marks)
        notify_marks = restored.pop("notify_marks", None)
        if notify_marks is not None:  # p220: before the first tick looks for events to report
            restore_notify_marks(app.state.sessionmaker, notify_marks)
        activity.record("restore_done", "user", restored, via=via if via == "cli" else None)
    activity.record("started", "system", {"version": __version__})


SHUTDOWN_TASK_WAIT = 5.0  # seconds the background tasks get to stop; the process must not hang on one


async def stop_background(tasks: list[asyncio.Task[None]], *, wait: float | None = None) -> None:
    """Cancel every background task and wait for all of them together, at most `wait` seconds.

    One after the other (v0.3.7) a task that ended with an error left the next one unwaited, and a
    task that ignored the cancel held the shutdown for ever. Now: all are cancelled first, then one
    wait for the whole set; an error of a task is logged (its type only, never its text: it may name
    an address) and one that is still running after the time is named. Nothing is left pending without
    being looked at ("Task was destroyed but it is pending", "exception was never retrieved") - v0.3.8 p67.
    Tests: test_background_shutdown.py.
    """
    tasks = [task for task in tasks if task is not None]
    if not tasks:
        return
    for task in tasks:
        task.cancel()
    _done, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_TASK_WAIT if wait is None else wait)
    for task in tasks:
        if task in pending or task.cancelled():
            continue
        error = task.exception()  # looked at, so asyncio does not complain about it later
        if error is not None:
            log.warning("background task %s ended with %s", task.get_name(), type(error).__name__)
    for task in pending:
        log.warning(
            "background task %s did not stop within %s s", task.get_name(), SHUTDOWN_TASK_WAIT if wait is None else wait
        )


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the tick engine and Bambuddy's live listener in the background (`run_engine`)."""
    tasks: list[asyncio.Task[None]] = []
    ticker_task: asyncio.Task[None] | None = None
    live_task: asyncio.Task[None] | None = None
    if app.state.run_engine:
        ticker_task = asyncio.create_task(
            app.state.ticker.run(lambda: _poll_seconds(app)), name="ticker"
        )
        live_task = asyncio.create_task(app.state.live_listener.run(), name="bambuddy-live")
        # The check for new versions (Spec 11.3a): its own task, so neither the tick nor a page waits for git.
        tasks = [ticker_task, live_task, asyncio.create_task(app.state.auto_check.run(), name="update-check")]
    app.state.ticker_task = ticker_task
    app.state.live_task = live_task
    _log_start(app)
    if app.state.run_engine:
        tasks.append(asyncio.create_task(app.state.activity.run(), name="activity"))
    try:
        yield
    finally:
        app.state.stopping = True
        await app.state.notifier.stop()  # a message on its way is ended, nothing new starts
        background = list(tasks)
        check = app.state.rights.task  # a check of the key's rights started by a page
        if check is not None:
            background.append(check)
        await stop_background(background)


def notify_rights(monitor) -> Rights:
    """The check of the Bambuddy key at start (Spec 11.3) as the push messages read it (event 7)."""
    if monitor.status == "missing" and monitor.check is not None:
        return Rights("missing", tuple(monitor.check.missing))
    return Rights("ok" if monitor.status == "ok" else "unknown", ())


def shared_tls_client_factory() -> Callable[[], httpx.AsyncClient]:
    """A factory of `httpx.AsyncClient`s that all use one TLS context, built here, once.

    Still one client per call (Decision 4 of B2): only the certificate loading is shared.
    `httpx.AsyncClient()` loads the CA certificates every time it is built, which takes up to
    seconds and blocks the event loop. `httpx.create_ssl_context()` with no arguments is the
    context httpx 0.28 builds for its default `verify=True` (`httpx/_config.py:23-40`); given
    as `verify=`, httpx uses it as it is (`:55-56`), so certificates are checked as before.
    httpcore sets ALPN on the shared context at every connect, so it must never be handed to an
    `http2=True` client.
    """
    context = httpx.create_ssl_context()

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(verify=context)

    return factory


def create_app(
    db_path: Path,
    http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    *,
    ops: OpsContext | None = None,
    run_engine: bool = True,
    live_connect: Callable[..., Any] | None = None,
    live_sleep: Callable[[float], Awaitable[None]] | None = None,
) -> FastAPI:
    engine = make_engine(db_path)
    upgrade(engine)
    # Uploads and half-written files of a restore or backup the last process did not finish.
    fullbackup.clear_work_dir(fullbackup.work_dir_for(db_path))
    sessionmaker = make_sessionmaker(engine)
    # Every committed change of this sessionmaker goes to the open pages (Spec 10.5.2).
    live = LiveBroker()
    install_change_feed(sessionmaker, live.publish)
    with sessionmaker() as session:
        secret = st.ensure_session_secret(session)
        https = st.tls_enabled(session)

    # No interactive API docs: this is a browser application, not a public API.
    app = FastAPI(
        title="Reprintbuddy", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan
    )
    app.state.engine = engine  # the SQLAlchemy engine; the tick engine is `ticker`
    app.state.db_path = db_path
    app.state.ops = ops or OpsContext()
    app.state.sessionmaker = sessionmaker
    app.state.login_failure_delay = 0.5  # seconds of pause after a wrong password
    app.state.login_brake = FailureBrake()  # 10 failures in 60 s: every attempt waits 2 s
    app.state.hash_limiter = HashLimiter(capacity=2)  # at most two argon2 runs at once
    # Built at start, not on the first Bambuddy call: the TLS context is loaded before any page.
    app.state.http_client_factory = http_client_factory or shared_tls_client_factory()
    # The client is built from the stored settings at every call; tests replace the factory.
    app.state.bambuddy_factory = make_http_factory(sessionmaker, app.state.http_client_factory)
    app.state.session_secret = secret  # also signs the one-time form tokens
    app.state.rights = RightsMonitor()  # what the stored key may do, checked once per start
    app.state.live = live
    app.state.restore = backup_routes.RestoreState()  # Spec 11.6: pauses writes and the engine
    app.state.update_starting = False  # a click on "Update" is on its way (see `update_start`)
    app.state.writes_in_flight = 0  # counted by the update guard; a restore waits for them
    app.state.restore_write_wait = 30.0  # seconds a restore waits for those writes
    app.state.version = __version__  # the live channel tells pages of another version to reload
    # The tick engine (plan B4): the app's own sessionmaker, so the change feed reports its
    # writes like any other; its runtime status goes to the pages as topic "bambuddy".
    app.state.ticker = Engine(
        sessionmaker,
        lambda: deps.bambuddy_client(app),
        # A restore counts like an update: the engine must not touch the database it replaces.
        # A restore, and a click on "Update" whose sudo is still on its way, count like a running update.
        update_running=lambda: app.state.restore.active
        or app.state.update_starting
        or update_running(db_path.parent, app.state.ops.runner, datetime.now(UTC)),
        update_started=lambda: app.state.restore.active
        or app.state.update_starting
        or update_just_started(db_path.parent, datetime.now(UTC)),
        publish=live.publish,
    )
    app.state.ticker.add_poll_hook("filament", filament.poll_hook)  # the daily record and receipt check (B6)
    # Push messages through Home Assistant (Spec 10.6): after every tick, in a task of their own.
    app.state.notifier = Notifier(
        sessionmaker,
        http_factory=app.state.http_client_factory,
        engine_status=lambda: app.state.ticker.status,
        rights=lambda: notify_rights(app.state.rights),
        update_started=app.state.ticker.update_started,
        base=lambda session: qr_view.background_base(app, session),
        publish=live.publish,
    )
    app.state.ticker.after_tick.append(app.state.notifier.after_tick)
    _install_activity_log(app, sessionmaker, db_path)
    app.state.ticker.after_tick.append(lambda poll: _log_bambuddy(app, poll))
    app.state.run_engine = run_engine
    app.state.ticker_task = None  # the background loop while the app runs (see _lifespan)
    app.state.stopping = False  # set when the lifespan shuts down: a restore then asks for no restart
    # Bambuddy's live channel (Spec 7.6): a message that may have changed the queue, and every
    # connect, only flag the engine; the poll runs in the engine's own loop task.
    live_options: dict[str, Any] = {}
    if live_connect is not None:  # tests; the default is the library's `connect`
        live_options["connect"] = live_connect
    if live_sleep is not None:
        live_options["sleep"] = live_sleep
    app.state.live_listener = LiveListener(
        _LiveClient(app),
        lambda kind: app.state.ticker.request_poll(),
        # The settings page shows the state of the channel live (v0.3.6 p86).
        on_state=lambda state: live.publish(frozenset({LIVE_CHANNEL_TOPIC})),
        **live_options,
    )
    app.state.live_task = None
    _install_auto_check(app)

    # Templates and texts are read now and never again: an update replaces the files while this
    # process still answers, and it must go on rendering what it started with.
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.loader = PreloadedLoader(TEMPLATES_DIR)
    templates.env.auto_reload = False
    load_locales()
    templates.env.globals["t"] = deps.t
    templates.env.globals["tc"] = deps.tc
    templates.env.globals["status_tone"] = status.tone
    templates.env.globals["fill_percent"] = fill_percent
    templates.env.globals["pop_flashes"] = pop_flashes
    # In the address of every file under /static/: a new version is a new address, so the browser
    # fetches the new style sheet instead of using the copy it kept from the old version.
    templates.env.globals["app_version"] = __version__
    templates.env.filters["local_dt"] = local_dt
    templates.env.filters["local_date"] = local_date
    templates.env.filters["local_day"] = local_day
    templates.env.filters["notes_markdown"] = render_notes  # p143: always escaped
    templates.env.filters["num"] = decimal
    templates.env.filters["unit"] = unit
    templates.env.globals["form_token"] = lambda: issue_form_token(secret)
    app.state.templates = templates

    # Middleware added later wraps the earlier ones: the session must be the
    # outer layer so the gate can read it.
    # Innermost: pauses writes while an update runs; the gate has already decided who may pass.
    app.middleware("http")(ops_routes.update_guard)
    # Sees the final status of the route and of the update guard (503 asks for no tick).
    app.middleware("http")(tick_after_write)
    app.middleware("http")(deps.gate)
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie=SESSION_COOKIE,
        same_site="lax",
        https_only=https,
    )
    # Outside the session: a request that came over HTTPS (also through a trusted proxy) gets Secure cookies.
    app.add_middleware(SecureCookiesOnHttps)
    # Every body is bounded before anything reads it (v0.3.9 T0, `web/bodylimit.py`); the uploads
    # have limits of their own and refuse a longer declaration with a page of their own.
    app.state.body_limits = {
        "/settings/restore/upload": fullbackup.MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD,
        upload_routes.PATH: upload_routes.BODY_LIMIT,
        settings_routes.SERVER_PATH: settings_routes.SERVER_BODY_LIMIT,
    }
    app.state.upload_lock = upload_routes.make_lock()  # one upload at a time (Spec 5.1a)
    app.add_middleware(BodyLimitMiddleware, limits=app.state.body_limits)
    app.add_middleware(SubPathWarning)  # a proxy sub-path is not supported: say so once (Spec 11.4)
    app.middleware("http")(deps.security_headers)  # outermost: covers every response

    app.add_exception_handler(StarletteHTTPException, deps.http_error_handler)
    app.add_exception_handler(OverflowError, deps.overflow_handler)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> FileResponse:
        # Browsers ask for this path on their own, also before anybody is signed in.
        return FileResponse(WEB_DIR / "static" / "img" / "favicon-48.png", media_type="image/png")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(setup_routes.router)
    app.include_router(auth_routes.router)
    app.include_router(overview_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(rights_routes.router)
    app.include_router(parts_routes.router)
    app.include_router(filament_routes.router)
    app.include_router(reorder_routes.router)
    app.include_router(upload_routes.router)  # before the articles: /articles/new/upload
    app.include_router(articles_routes.router)
    app.include_router(journal_routes.router)
    app.include_router(sets_routes.router)
    app.include_router(orders_routes.router)
    app.include_router(bookings_routes.router)
    app.include_router(queue_routes.router)
    app.include_router(inspection_routes.router)
    app.include_router(qr_routes.router)
    app.include_router(labels_routes.router)
    app.include_router(notify_routes.router)
    app.include_router(ops_routes.router)
    app.include_router(backup_routes.router)
    app.include_router(live_routes.router)
    app.include_router(ui_routes.router)
    return app
