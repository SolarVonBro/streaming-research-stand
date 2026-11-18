"""Одна сессия просмотра: манифест -> цикл «выбор уровня -> загрузка сегмента -> буфер».

Модель буфера — как в симуляторе Pensieve и в dash.js: воспроизведение начинается после
первого сегмента; во время загрузки буфер расходуется в реальном времени; если загрузка
дольше запаса буфера, разница — ребуферизация (остановка). Буфер ограничен buffer_max_s:
при переполнении плеер ждёт, пока буфер опустеет на один сегмент.
Все события пишутся построчно; QoE считает скрипт анализа из сырых строк.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import asdict, dataclass

import httpx

from . import abr as abr_mod
from .shaper import Trace, Transfer

CHUNK = 16 * 1024
MIN_SLEEP_S = 0.02  # больше шага системного таймера (Windows ~15,6 мс, Linux < 1 мс)
WAIT_EPS_S = 0.002  # чтение дольше 2 мс = данных в сокете не было, ждали сервер


@dataclass
class SegmentLog:
    player: str
    abr: str
    index: int
    level: int
    bitrate_kbps: int
    bytes: int
    t_request_s: float      # от начала сессии
    ttfb_s: float           # до первого байта от сервера (без эмуляции канала)
    download_s: float       # полная длительность загрузки через эмулируемый канал
    throughput_mbps: float
    buffer_before_s: float
    buffer_after_s: float
    rebuffer_s: float
    wait_s: float           # пауза перед запросом (буфер полон или решение ABR)
    status: int
    retries: int


def parse_mpd(xml: str) -> tuple[list[int], float, int, str]:
    bitrates = [int(b) // 1000 for b in re.findall(r'<Representation[^>]*bandwidth="(\d+)"', xml)]
    seg = float(re.search(r'<SegmentTemplate[^>]*duration="([\d.]+)"', xml).group(1))
    total = float(re.search(r'mediaPresentationDuration="PT([\d.]+)S"', xml).group(1))
    media = re.search(r'media="([^"]+)"', xml).group(1)
    return bitrates, seg, round(total / seg), media


class Player:
    def __init__(self, player_id: str, algorithm: abr_mod.Abr, trace: Trace, client: httpx.AsyncClient, *,
                 buffer_max_s: float, rtt_s: float, max_segments: int | None, telemetry: bool = True):
        self.id = player_id
        self.abr = algorithm
        self.trace = trace
        self.client = client
        self.buffer_max_s = buffer_max_s
        self.rtt_s = rtt_s
        self.max_segments = max_segments
        self.telemetry = telemetry
        self.logs: list[SegmentLog] = []
        self.summary: dict = {}
        self._bg: set[asyncio.Task] = set()

    async def _fetch(self, url: str, t0: float) -> tuple[int, float, float, int]:
        """Загрузка через эмулируемый канал. Возвращает (байты, ttfb, длительность, статус)."""
        start = time.monotonic()
        link = Transfer(self.trace, trace_pos=start - t0, wall_start=start, rtt_s=self.rtt_s)
        nbytes, ttfb = 0, None
        resume = start
        async with self.client.stream("GET", url) as resp:
            async for chunk in resp.aiter_raw(CHUNK):
                now = time.monotonic()
                if ttfb is None:
                    ttfb = now - start
                nbytes += len(chunk)
                # Если чтение ждало данные, сервер отдал их только сейчас: они «выходят в канал»
                # через RTT. Если данные уже лежали в сокете, сервер не опаздывал — потерь нет.
                waited = now - resume > WAIT_EPS_S
                due = link.deliver(len(chunk), now + self.rtt_s if waited else None)
                # момент доставки абсолютный: короткие паузы пропускаем, следующая их наверстает
                if due - now > MIN_SLEEP_S:
                    await asyncio.sleep(due - now)
                resume = time.monotonic()
            status = resp.status_code
        tail = link.wall - time.monotonic()
        if tail > 0:
            await asyncio.sleep(tail)
        end = max(time.monotonic(), link.wall)
        return nbytes, (ttfb or 0.0), end - start, status

    def _beacon(self, payload: dict) -> None:
        if not self.telemetry:
            return
        task = asyncio.create_task(self.client.post("/api/v1/events", json=payload))
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def run(self) -> None:
        t0 = time.monotonic()
        resp = await self.client.post("/api/v1/sessions", json={"user": self.id})
        resp.raise_for_status()
        token, manifest_url = resp.json()["token"], resp.json()["manifest"]
        mpd = await self.client.get(manifest_url)
        mpd.raise_for_status()
        bitrates, seg_s, total, media_tpl = parse_mpd(mpd.text)
        if self.max_segments:
            total = min(total, self.max_segments)

        state = abr_mod.AbrState(bitrates_kbps=bitrates, segment_seconds=seg_s, next_index=0,
                                 total_segments=total, buffer_s=0.0, now=0.0)
        buffer, playing, startup, rebuffer_total, errors = 0.0, False, None, 0.0, 0
        clock = time.monotonic()  # момент последнего обновления буфера
        for idx in range(total):
            now = time.monotonic()
            if playing:  # буфер расходуется и во время отправки телеметрии
                buffer = max(0.0, buffer - (now - clock))
            clock = now
            state.next_index, state.buffer_s, state.now = idx, buffer, now - t0
            level, wait = self.abr.choose(state)
            # общий для всех алгоритмов предел буфера
            wait = max(wait, buffer - (self.buffer_max_s - seg_s)) if playing else 0.0
            if wait > 0:
                await asyncio.sleep(wait)
            now = time.monotonic()
            if playing:
                buffer = max(0.0, buffer - (now - clock))
            clock = now
            buffer_before = buffer
            url = (media_tpl.replace("$RepresentationID$", str(level)).replace("$Number$", str(idx + 1))
                   + f"?token={token}")
            retries = 0
            while True:
                try:
                    nbytes, ttfb, dl, status = await self._fetch(url, t0)
                    if status == 200:
                        break
                except httpx.HTTPError:
                    status, nbytes, ttfb, dl = 0, 0, 0.0, 0.0
                errors += 1
                retries += 1
                if retries > 5:
                    raise RuntimeError(f"{self.id}: сегмент {idx + 1} не загружен (статус {status})")
                await asyncio.sleep(0.5)
            now = time.monotonic()
            elapsed = now - clock
            stall = max(0.0, elapsed - buffer) if playing else 0.0
            buffer = (max(0.0, buffer - elapsed) if playing else 0.0) + seg_s
            clock = now
            if not playing:
                playing, startup = True, now - t0
            rebuffer_total += stall
            thr = nbytes * 8 / 1e6 / max(dl, 1e-6)
            self.abr.on_download(state, thr)
            if state.last_level is not None and level != state.last_level:
                state.switch_times.append(now - t0)
            state.last_level = level
            state.levels.append(level)
            state.throughputs_mbps.append(thr)
            log = SegmentLog(self.id, self.abr.name, idx + 1, level, bitrates[level], nbytes,
                             round(now - dl - t0, 4), round(ttfb, 4), round(dl, 4), round(thr, 4),
                             round(buffer_before, 4), round(buffer, 4), round(stall, 4), round(max(wait, 0), 4),
                             status, retries)
            self.logs.append(log)
            self._beacon({"player": self.id, "segment": idx + 1, "level": level, "buffer_s": log.buffer_after_s,
                          "rebuffer_s": log.rebuffer_s})
        if self._bg:
            await asyncio.gather(*self._bg, return_exceptions=True)
        self.summary = {"player": self.id, "abr": self.abr.name, "trace": self.trace.name,
                        "trace_mean_mbps": round(self.trace.mean_mbps(), 4), "segments": total,
                        "startup_s": round(startup or 0.0, 4), "rebuffer_s": round(rebuffer_total, 4),
                        "session_s": round(time.monotonic() - t0, 4), "http_errors": errors}

    def dump(self, out_dir) -> None:
        import csv
        rows = [asdict(x) for x in self.logs]
        with open(out_dir / f"segments_{self.id}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]) if rows else ["player"], lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        (out_dir / f"session_{self.id}.json").write_text(json.dumps(self.summary, indent=2), encoding="utf-8")
