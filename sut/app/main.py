"""Сборка приложения по роли: один и тот же образ работает как монолит или как любой из сервисов."""
from __future__ import annotations

from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from . import modules, settings

ROLES = {"mono", "auth", "catalog", "delivery", "telemetry"}


def create_app(role: str = settings.ROLE) -> FastAPI:
    if role not in ROLES:
        raise ValueError(f"неизвестная роль {role!r}")
    r = aioredis.from_url(settings.REDIS_URL, max_connections=200)
    remote = modules.RemoteAuth(settings.AUTH_URL) if role == "delivery" else None

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        if remote:
            await remote.client.aclose()
        await r.aclose()

    app = FastAPI(title=f"streamhub:{role}", lifespan=lifespan, docs_url=None, redoc_url=None)

    if role in ("mono", "auth"):
        app.include_router(modules.auth_router(r))
    if role in ("mono", "catalog"):
        app.include_router(modules.catalog_router(r))
    if role in ("mono", "delivery"):
        app.include_router(modules.delivery_router(remote or modules.LocalAuth(r)))
    if role in ("mono", "telemetry"):
        app.include_router(modules.telemetry_router(r))

    @app.get("/healthz")
    async def healthz() -> dict:
        await r.ping()
        return {"status": "ok", "role": role}

    Instrumentator(excluded_handlers=["/metrics", "/healthz"]).instrument(app).expose(
        app, include_in_schema=False
    )
    return app


app = create_app()
