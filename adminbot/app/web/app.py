"""FastAPI application factory for the Adminbot manager UI."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from adminbot.app.manager import AdminbotManager
from adminbot.app.paths import build_runtime_paths

_WEB_DIR = Path(__file__).parent
_TEMPLATES_DIR = _WEB_DIR / "templates"
_STATIC_DIR = _WEB_DIR / "static"


def create_app() -> FastAPI:
    app = FastAPI(
        title="Adminbot Manager",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    paths = build_runtime_paths()
    manager = AdminbotManager(paths)
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    allow_remote = os.environ.get("ADMINBOT_ALLOW_REMOTE", "").strip() == "1"

    app.state.paths = paths
    app.state.manager = manager
    app.state.templates = templates
    app.state.allow_remote = allow_remote

    @app.middleware("http")
    async def local_only_middleware(request: Request, call_next):
        if request.app.state.allow_remote:
            return await call_next(request)

        client_host = request.client.host if request.client else ""
        if client_host in {"127.0.0.1", "::1", "localhost"}:
            return await call_next(request)
        return PlainTextResponse(
            "Adminbot web UI is local-only by default. Set ADMINBOT_ALLOW_REMOTE=1 only if "
            "you have added a deliberate access-control layer.",
            status_code=403,
        )

    from adminbot.app.web.routes.bots import router as bots_router
    from adminbot.app.web.routes.dashboard import router as dashboard_router
    from adminbot.app.web.routes.logs import router as logs_router

    app.include_router(dashboard_router)
    app.include_router(bots_router)
    app.include_router(logs_router)
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    return app
