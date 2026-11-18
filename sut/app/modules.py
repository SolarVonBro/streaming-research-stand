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

import httpx
import redis.asyncio as aioredis
from fastapi import APIRouter, HTTPException, Request, Response

from . import media, settings

SESSION_TTL_S = 3600


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
        if not await auth.check(token):
            raise HTTPException(status_code=401)
        body = media.segment_bytes(level, segment) if video_id == media.VIDEO.video_id else None
        if body is None:
            raise HTTPException(status_code=404)
        if settings.INJECT_DELAY_MS:
            await asyncio.sleep(settings.INJECT_DELAY_MS / 1000)
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
