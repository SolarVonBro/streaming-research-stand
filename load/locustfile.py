"""Нагрузка для эксперимента E1 (и фоновая нагрузка E3): «зрители» видеоплатформы.

Каждый пользователь Locust — зритель: создаёт сессию, получает манифест и затем раз в
PACING секунд (длительность сегмента) запрашивает очередной сегмент фиксированного уровня
и отправляет событие телеметрии. Так воспроизводится установившийся режим просмотра:
интенсивность = users × 2 / PACING запросов в секунду (полуоткрытая модель, constant_pacing).

Прогрев: статистика Locust сбрасывается через WARMUP секунд после старта, поэтому в CSV
попадает только окно измерения. Сырые задержки окна измерения пишутся в requests.csv.gz
(для ECDF и проверки перцентилей Locust).
"""
from __future__ import annotations

import gzip
import os
import random
import time

import gevent
from locust import FastHttpUser, constant_pacing, events, task

PACING = float(os.environ.get("PACING", "4.0"))
LEVEL = int(os.environ.get("LEVEL", "1"))
SEGMENTS = int(os.environ.get("SEGMENTS", "48"))
WARMUP = float(os.environ.get("WARMUP", "0"))
RAW_OUT = os.environ.get("RAW_OUT", "")
SEED = int(os.environ.get("SEED", "0"))

_rng = random.Random(SEED)
_state = {"measuring": WARMUP <= 0, "t0": None}
_raw: list[tuple] = []


@events.test_start.add_listener
def _on_start(environment, **_):
    _state["t0"] = time.time()
    if WARMUP > 0:
        def reset():
            environment.stats.reset_all()
            _raw.clear()
            _state["measuring"] = True
            _state["t0"] = time.time()
        gevent.spawn_later(WARMUP, reset)


@events.request.add_listener
def _on_request(request_type, name, response_time, response_length, exception, **_):
    if _state["measuring"]:
        _raw.append((round(time.time() - _state["t0"], 4), name, round(response_time, 3),
                     response_length, 0 if exception is None else 1))


@events.quitting.add_listener
def _on_quit(environment, **_):
    if not RAW_OUT:
        return
    with gzip.open(RAW_OUT, "wt", encoding="utf-8") as f:
        f.write("t_s,name,response_ms,bytes,failed\n")
        for row in _raw:
            f.write(",".join(map(str, row)) + "\n")


class Viewer(FastHttpUser):
    wait_time = constant_pacing(PACING)

    def on_start(self):
        r = self.client.post("/api/v1/sessions", json={"user": "locust"}, name="/api/v1/sessions")
        data = r.json()
        self.token = data["token"]
        self.client.get(data["manifest"], name="/api/v1/videos/[id]/manifest.mpd")
        # зрители начинают с разных мест видео
        self.segment = _rng.randint(1, SEGMENTS)

    @task
    def watch(self):
        self.client.get(f"/media/envivio/{LEVEL}/{self.segment}.m4s?token={self.token}", name="/media/[segment]")
        self.client.post("/api/v1/events", json={"segment": self.segment, "level": LEVEL, "buffer_s": 20.0},
                         name="/api/v1/events")
        self.segment = self.segment % SEGMENTS + 1
