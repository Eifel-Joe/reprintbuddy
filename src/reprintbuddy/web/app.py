"""The FastAPI application."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import httpx
from fastapi import FastAPI
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.convertors import Convertor, register_url_convertor
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from reprintbuddy import __version__
from reprintbuddy import settings as st
from reprintbuddy.bambuddy.factory import make_http_factory
from reprintbuddy.changes import install_change_feed
from reprintbuddy.db import make_engine, make_sessionmaker, upgrade
from reprintbuddy.domain import status
from reprintbuddy.ops.context import OpsContext
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
from reprintbuddy.web.routes import journal as journal_routes
from reprintbuddy.web.routes import live as live_routes
from reprintbuddy.web.routes import orders as orders_routes
from reprintbuddy.web.routes import ops as ops_routes
from reprintbuddy.web.routes import overview as overview_routes
from reprintbuddy.web.routes import rights as rights_routes
from reprintbuddy.web.routes import parts as parts_routes
from reprintbuddy.web.routes import settings as settings_routes
from reprintbuddy.web.routes import sets as sets_routes
from reprintbuddy.web.routes import setup as setup_routes

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


def create_app(
    db_path: Path,
    http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    *,
    ops: OpsContext | None = None,
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
    app = FastAPI(title="Reprintbuddy", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
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
    app.include_router(ops_routes.router)
    app.include_router(live_routes.router)
    return app
