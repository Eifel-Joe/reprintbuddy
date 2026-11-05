"""The FastAPI application."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import httpx
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from reprintbuddy import settings as st
from reprintbuddy.db import make_engine, make_sessionmaker, upgrade
from reprintbuddy.web import deps
from reprintbuddy.web.routes import auth as auth_routes
from reprintbuddy.web.routes import overview as overview_routes
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
        https = bool(st.get_setting(session, "tls_cert"))

    # No interactive API docs: this is a browser application, not a public API.
    app = FastAPI(title="Reprintbuddy", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
    app.state.sessionmaker = sessionmaker
    app.state.session_secret = secret
    app.state.http_client_factory = http_client_factory or httpx.AsyncClient

    templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
    templates.env.globals["t"] = deps.t
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

    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(setup_routes.router)
    app.include_router(auth_routes.router)
    app.include_router(overview_routes.router)
    app.include_router(settings_routes.router)
    return app
