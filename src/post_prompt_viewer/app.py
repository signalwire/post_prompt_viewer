"""FastAPI application factory."""

from __future__ import annotations

import base64
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from . import __version__, storage
from .config import get_settings
from .web import routes_api, routes_ingest

log = logging.getLogger("post_prompt_viewer")

WEB_DIR = Path(__file__).parent / "web"


def create_app() -> FastAPI:
    settings = get_settings()
    storage.init_db()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Resume analysis for any recordings left pending by a previous run.
        if settings.auto_analyze:
            try:
                from . import recordings

                for call_id in storage.pending_recordings():
                    recordings.schedule_analysis(call_id)
            except Exception:
                log.info("recordings extra not installed; skipping analysis catch-up")
        yield

    # NB: we deliberately do NOT set root_path to the proxy prefix. The standard
    # reverse-proxy setup (e.g. `ProxyPass /collect/ http://127.0.0.1:9070/`)
    # strips the prefix before forwarding, so the app must route on the bare
    # paths it actually receives (/static, /c/..., etc.). The prefix is applied
    # only when *emitting* URLs, via PPV_PROXY_PREFIX in routes_view._path_url.
    app = FastAPI(
        title="Post Prompt Viewer",
        version=__version__,
        lifespan=lifespan,
    )

    if settings.auth_enabled or settings.collect_auth_enabled:

        def _basic_ok(header: str, user: str, pw: str) -> bool:
            if not header.startswith("Basic "):
                return False
            try:
                u, _, p = base64.b64decode(header[6:]).decode("utf-8").partition(":")
            except Exception:
                return False
            return secrets.compare_digest(u, user) and secrets.compare_digest(p, pw)

        def _unauthorized():
            return Response(
                "Authentication required",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Post Prompt Viewer"'},
            )

        @app.middleware("http")
        async def _basic_auth(request, call_next):
            path = request.url.path
            header = request.headers.get("Authorization", "")
            # Ingest webhook: gated by the collect creds (separate from the viewer).
            if request.method == "POST" and (path == "/" or path.startswith("/collect")):
                if settings.collect_auth_enabled and not _basic_ok(
                    header, settings.collect_user, settings.collect_pass
                ):
                    return _unauthorized()
                return await call_next(request)
            if path == "/api/health":
                return await call_next(request)
            # Everything else: the viewer login.
            if settings.auth_enabled and not _basic_ok(header, settings.auth_user, settings.auth_pass):
                return _unauthorized()
            return await call_next(request)

        if settings.auth_enabled:
            log.info("viewer basic auth enabled (user %r)", settings.auth_user)
        if settings.collect_auth_enabled:
            log.info("collect basic auth enabled (user %r)", settings.collect_user)

    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
    app.include_router(routes_ingest.router)
    app.include_router(routes_api.router)

    # Views (templates) are optional at import time so the service still boots
    # if a template is mid-edit; routes_view is the normal path.
    try:
        from .web import routes_view

        app.include_router(routes_view.router)
    except ModuleNotFoundError as exc:  # pragma: no cover
        if exc.name and exc.name.endswith("routes_view"):
            log.debug("routes_view not present yet")
        else:
            raise

    return app
