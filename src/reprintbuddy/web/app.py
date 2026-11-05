"""The FastAPI application."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import httpx
from fastapi import FastAPI
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from reprintbuddy import settings as st
from reprintbuddy.db import make_engine, make_sessionmaker, upgrade
from reprintbuddy.web import deps
from reprintbuddy.web.flash import pop_flashes
from reprintbuddy.web.formatting import local_dt
from reprintbuddy.web.formtoken import issue_form_token
from reprintbuddy.web.throttle import FailureBrake, HashLimiter
from reprintbuddy.web.routes import articles as articles_routes
from reprintbuddy.web.routes import auth as auth_routes
from reprintbuddy.web.routes import journal as journal_routes
from reprintbuddy.web.routes import overview as overview_routes
from reprintbuddy.web.routes import parts as parts_routes
from reprintbuddy.web.routes import settings as settings_routes
from reprintbuddy.web.routes import setup as setup_routes

WEB_DIR = Path(__file__).resolve().parent
SESSION_COOKIE = "reprintbuddy_session"


def create_app(
    db_path: Path,
    http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> FastAPI:
    engine = make_engine(db_path)
    upgrade(engine)
    sessionmaker = make_sessionmaker(engine)
    with sessionmaker() as session:
        secret = st.ensure_session_secret(session)
        https = st.tls_enabled(session)

    # No interactive API docs: this is a browser application, not a public API.
    app = FastAPI(title="Reprintbuddy", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
    app.state.sessionmaker = sessionmaker
    app.state.login_failure_delay = 0.5  # seconds of pause after a wrong password
    app.state.login_brake = FailureBrake()  # 10 failures in 60 s: every attempt waits 2 s
    app.state.hash_limiter = HashLimiter(capacity=2)  # at most two argon2 runs at once
    app.state.http_client_factory = http_client_factory or httpx.AsyncClient
    app.state.bambuddy_factory = None  # B2 sets the HTTP factory; tests set the fake
    app.state.session_secret = secret  # also signs the one-time form tokens

    templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
    templates.env.globals["t"] = deps.t
    templates.env.globals["pop_flashes"] = pop_flashes
    templates.env.filters["local_dt"] = local_dt
    templates.env.globals["form_token"] = lambda: issue_form_token(secret)
    app.state.templates = templates

    # Middleware added later wraps the earlier ones: the session must be the
    # outer layer so the gate can read it.
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
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(setup_routes.router)
    app.include_router(auth_routes.router)
    app.include_router(overview_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(parts_routes.router)
    app.include_router(articles_routes.router)
    app.include_router(journal_routes.router)
    return app
