"""The FastAPI application."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

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
from reprintbuddy.bambuddy.factory import make_http_factory
from reprintbuddy.changes import install_change_feed
from reprintbuddy.db import make_engine, make_sessionmaker, upgrade
from reprintbuddy.domain import status
from reprintbuddy.domain.engine import Engine
from reprintbuddy.ops.context import OpsContext
from reprintbuddy.ops.running import update_running
from reprintbuddy.web import deps
from reprintbuddy.web.flash import pop_flashes
from reprintbuddy.web.formatting import fill_percent, local_date, local_dt
from reprintbuddy.web.formtoken import issue_form_token
from reprintbuddy.web.i18n import load_locales
from reprintbuddy.web.live import LiveBroker
from reprintbuddy.web.preload import PreloadedLoader
from reprintbuddy.web.rights import RightsMonitor
from reprintbuddy.web.throttle import FailureBrake, HashLimiter
from reprintbuddy.web.routes import articles as articles_routes
from reprintbuddy.web.routes import auth as auth_routes
from reprintbuddy.web.routes import bookings as bookings_routes
from reprintbuddy.web.routes import inspection as inspection_routes
from reprintbuddy.web.routes import journal as journal_routes
from reprintbuddy.web.routes import live as live_routes
from reprintbuddy.web.routes import orders as orders_routes
from reprintbuddy.web.routes import ops as ops_routes
from reprintbuddy.web.routes import overview as overview_routes
from reprintbuddy.web.routes import rights as rights_routes
from reprintbuddy.web.routes import parts as parts_routes
from reprintbuddy.web.routes import queue as queue_routes
from reprintbuddy.web.routes import settings as settings_routes
from reprintbuddy.web.routes import sets as sets_routes
from reprintbuddy.web.routes import setup as setup_routes
from reprintbuddy.web.routes import ui as ui_routes

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
TICK_EXEMPT_PREFIXES = ("/login", "/logout", "/setup", "/settings/restart", "/settings/update")


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


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the tick engine in the background while the app serves (`run_engine`)."""
    task: asyncio.Task[None] | None = None
    if app.state.run_engine:
        task = asyncio.create_task(app.state.ticker.run(lambda: _poll_seconds(app)), name="ticker")
    app.state.ticker_task = task
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def create_app(
    db_path: Path,
    http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    *,
    ops: OpsContext | None = None,
    run_engine: bool = True,
) -> FastAPI:
    engine = make_engine(db_path)
    upgrade(engine)
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
    app.state.http_client_factory = http_client_factory or httpx.AsyncClient
    # The client is built from the stored settings at every call; tests replace the factory.
    app.state.bambuddy_factory = make_http_factory(sessionmaker, app.state.http_client_factory)
    app.state.session_secret = secret  # also signs the one-time form tokens
    app.state.rights = RightsMonitor()  # what the stored key may do, checked once per start
    app.state.live = live
    app.state.version = __version__  # the live channel tells pages of another version to reload
    # The tick engine (plan B4): the app's own sessionmaker, so the change feed reports its
    # writes like any other; its runtime status goes to the pages as topic "bambuddy".
    app.state.ticker = Engine(
        sessionmaker,
        lambda: deps.bambuddy_client(app),
        update_running=lambda: update_running(db_path.parent, app.state.ops.runner, datetime.now(UTC)),
        publish=live.publish,
    )
    app.state.run_engine = run_engine
    app.state.ticker_task = None  # the background loop while the app runs (see _lifespan)

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
    app.include_router(articles_routes.router)
    app.include_router(journal_routes.router)
    app.include_router(sets_routes.router)
    app.include_router(orders_routes.router)
    app.include_router(bookings_routes.router)
    app.include_router(queue_routes.router)
    app.include_router(inspection_routes.router)
    app.include_router(ops_routes.router)
    app.include_router(live_routes.router)
    app.include_router(ui_routes.router)
    return app
