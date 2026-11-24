"""Entry point for the NeatSprint Worklog Service."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from neatsprint_lib.api import create_health_router, install_platform_middleware
from neatsprint_lib.auth import TokenVerifier, install_auth
from neatsprint_lib.db import check_database, create_engine, create_session_factory
from neatsprint_lib.errors import install_error_handlers
from neatsprint_lib.events import run_relay
from neatsprint_lib.logging import configure_logging

from neatsprint_worklog.api.internal import router as internal_router
from neatsprint_worklog.api.router import router
from neatsprint_worklog.settings import settings

configure_logging(settings.service_name, settings.log_level, settings.version)


def _database_check() -> Any:
    engine = getattr(app.state, "engine", None)
    if engine is None:
        return True
    return check_database(engine)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    engine = None
    if settings.database_url:
        engine = create_engine(settings.database_url)
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
    if settings.jwks_url:
        verifier = TokenVerifier(
            issuer=settings.jwt_issuer,
            audience=settings.jwt_audience,
            jwks_url=settings.jwks_url,
        )
        install_auth(app, verifier)
        app.state.token_verifier = verifier

    outbox_stop: asyncio.Event | None = None
    outbox_task: asyncio.Task[None] | None = None
    if engine is not None and settings.redis_url and settings.outbox_enabled:
        outbox_stop = asyncio.Event()
        outbox_task = asyncio.create_task(
            run_relay(
                app.state.session_factory,
                settings.redis_url,
                settings.outbox_stream,
                outbox_stop,
            )
        )
    try:
        yield
    finally:
        if outbox_stop is not None:
            outbox_stop.set()
        if outbox_task is not None:
            try:
                await asyncio.wait_for(outbox_task, timeout=5)
            except TimeoutError:
                outbox_task.cancel()
        if engine is not None:
            await engine.dispose()


app = FastAPI(
    title="NeatSprint Worklog Service",
    version=settings.version,
    lifespan=lifespan,
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None,
    openapi_url=None if settings.is_production else "/openapi.json",
)
app.state.settings = settings

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

install_error_handlers(app)
install_platform_middleware(
    app, settings.service_name, metrics_enabled=settings.metrics_enabled
)
app.include_router(
    create_health_router(
        settings.service_name,
        {"database": _database_check},
        version=settings.version,
    )
)
app.include_router(router, prefix=f"/api/v1/{settings.api_prefix}")
app.include_router(internal_router)


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    return {
        "service": settings.service_name,
        "version": settings.version,
        "docs": "/docs",
    }
