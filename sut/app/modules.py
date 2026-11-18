"""Бизнес-модули видеоплатформы: аутентификация, каталог, выдача сегментов, телеметрия.

Код модулей один и тот же в обоих вариантах архитектуры. Различается только то,
как модуль выдачи проверяет токен:
  * LocalAuth  — вызов функции в том же процессе (модульный монолит);
  * RemoteAuth — HTTP-вызов сервиса auth по внутренней сети (микросервисы).
Так фактор «архитектура» отделён от различий в реализации логики.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time

import httpx
import redis.asyncio as aioredis
from fastapi import APIRouter, HTTPException, Request, Response
from prometheus_client import Histogram

from . import media, settings

SESSION_TTL_S = 3600
# Время проверки токена при выдаче сегмента: в монолите — вызов функции и запрос в Redis,
# в микросервисах — HTTP-вызов сервиса auth. Мелкие корзины, чтобы видеть миллисекунды.
# Фактическая длительность внесённой задержки (калибровка): измеряется по реальным часам.
INJECTED = Histogram("stand_injected_delay_seconds", "Actual injected delay in delivery",
                     buckets=(0.005, 0.01, 0.015, 0.018, 0.019, 0.0195, 0.02, 0.0205, 0.021, 0.022, 0.025, 0.03, 0.05))
AUTH_CHECK = Histogram("stand_auth_check_seconds", "Token check duration in delivery",
                       buckets=(0.0005, 0.001, 0.002, 0.003, 0.005, 0.0075, 0.01, 0.015, 0.02, 0.03,
                                0.05, 0.075, 0.1, 0.15, 0.2, 0.3, 0.5, 1.0))


def _sign(sid: str) -> str:
    return hmac.new(settings.SECRET, sid.encode(), hashlib.sha256).hexdigest()[:16]


async def verify_token(r: aioredis.Redis, token: str) -> bool:
    sid, _, sig = token.partition(".")
    if not sid or not hmac.compare_digest(sig, _sign(sid)):
        return False
    return bool(await r.exists(f"session:{sid}"))


class LocalAuth:
    def __init__(self, r: aioredis.Redis):
        self.r = r

    async def check(self, token: str) -> bool:
        return await verify_token(self.r, token)


class RemoteAuth:
    def __init__(self, base_url: str):
        limits = httpx.Limits(max_connections=200, max_keepalive_connections=100)
        self.client = httpx.AsyncClient(base_url=base_url, timeout=5.0, limits=limits)

    async def check(self, token: str) -> bool:
        resp = await self.client.get("/internal/verify", params={"token": token})
        return resp.status_code == 204


def auth_router(r: aioredis.Redis) -> APIRouter:
    api = APIRouter()

    @api.post("/api/v1/sessions", status_code=201)
    async def create_session(request: Request) -> dict:
        raw = await request.body()
        body = json.loads(raw) if raw else {}
        sid = secrets.token_hex(8)
        await r.set(f"session:{sid}", json.dumps({"user": body.get("user", "anon")}), ex=SESSION_TTL_S)
        return {
            "token": f"{sid}.{_sign(sid)}",
            "manifest": f"/api/v1/videos/{media.VIDEO.video_id}/manifest.mpd",
        }

    @api.get("/internal/verify", status_code=204)
    async def verify(token: str) -> Response:
        if not await verify_token(r, token):
            raise HTTPException(status_code=401)
        return Response(status_code=204)

    return api


def catalog_router(r: aioredis.Redis) -> APIRouter:
    api = APIRouter()
    mpd = media.build_mpd()

    @api.get("/api/v1/videos/{video_id}/manifest.mpd")
    async def manifest(video_id: str) -> Response:
        if video_id != media.VIDEO.video_id:
            raise HTTPException(status_code=404)
        await r.incr(f"views:{video_id}")
        return Response(content=mpd, media_type="application/dash+xml")

    return api


def delivery_router(auth: LocalAuth | RemoteAuth) -> APIRouter:
    api = APIRouter()

    @api.get("/media/{video_id}/{level}/{segment}.m4s")
    async def segment(video_id: str, level: int, segment: int, token: str = "") -> Response:
        t0 = time.perf_counter()
        ok = await auth.check(token)
        AUTH_CHECK.observe(time.perf_counter() - t0)
        if not ok:
            raise HTTPException(status_code=401)
        body = media.segment_bytes(level, segment) if video_id == media.VIDEO.video_id else None
        if body is None:
            raise HTTPException(status_code=404)
        if settings.INJECT_DELAY_MS:
            # Таймеры uvloop (libuv) отсчитываются от времени, закэшированного в начале итерации
            # цикла, и под нагрузкой срабатывают раньше. Поэтому досыпаем по реальным часам.
            target = settings.INJECT_DELAY_MS / 1000
            t_inj = time.perf_counter()
            while (left := target - (time.perf_counter() - t_inj)) > 0:
                await asyncio.sleep(left)
            INJECTED.observe(time.perf_counter() - t_inj)
        return Response(content=body, media_type="video/iso.segment")

    return api


def telemetry_router(r: aioredis.Redis) -> APIRouter:
    api = APIRouter()

    @api.post("/api/v1/events", status_code=202)
    async def event(request: Request) -> dict:
        payload = await request.body()
        await r.xadd("events", {"e": payload[:2048]}, maxlen=200_000, approximate=True)
        return {"accepted": True}

    return api
