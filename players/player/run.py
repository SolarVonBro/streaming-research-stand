"""Запуск группы плееров в одном прогоне.

Каждый плеер получает свой алгоритм ABR и свою копию эмулируемого канала по одной и той же
трассе — так алгоритмы сравниваются на идентичных сетевых условиях (парный план).

Пример:
  python -m player.run --base-url http://edge:8080 --abr rate,bola,robustmpc,festive \
      --trace /data/traces/lte/bus_0001.csv --out /results/run1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import resource
import time
from pathlib import Path

import httpx

from . import abr as abr_mod
from .session import Player
from .shaper import Trace


async def main_async(a: argparse.Namespace) -> int:
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    trace = Trace.load(a.trace)
    names = [x.strip() for x in a.abr.split(",") if x.strip()]
    limits = httpx.Limits(max_connections=4 * len(names) + 8, max_keepalive_connections=4 * len(names))
    async with httpx.AsyncClient(base_url=a.base_url, timeout=30.0, limits=limits) as client:
        players = [
            Player(f"p{i}_{name}", abr_mod.make(name, seed=a.seed + i, buffer_max_s=a.buffer_max), trace, client,
                   buffer_max_s=a.buffer_max, rtt_s=a.rtt_ms / 1000, max_segments=a.max_segments,
                   telemetry=not a.no_telemetry)
            for i, name in enumerate(names)
        ]
        cpu0, t0 = time.process_time(), time.monotonic()
        results = await asyncio.gather(*(p.run() for p in players), return_exceptions=True)
        cpu, wall = time.process_time() - cpu0, time.monotonic() - t0
    failed = 0
    for p, r in zip(players, results, strict=True):
        if isinstance(r, Exception):
            failed += 1
            p.summary = {"player": p.id, "abr": p.abr.name, "trace": trace.name, "error": repr(r)}
        p.dump(out)
    meta = {
        "players": len(players), "failed": failed, "trace": trace.name, "trace_file": os.path.basename(a.trace),
        "wall_s": round(wall, 3), "cpu_s": round(cpu, 3),
        # доля одного ядра, занятая эмулятором: проверка «генератор не узкое место»
        "cpu_share_of_core": round(cpu / wall, 4) if wall else None,
        "max_rss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "args": vars(a),
    }
    (out / "players_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(json.dumps({k: meta[k] for k in ("players", "failed", "wall_s", "cpu_share_of_core")}))
    return 1 if failed else 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Эмулированные DASH-плееры")
    ap.add_argument("--base-url", default="http://edge:8080")
    ap.add_argument("--abr", default=",".join(abr_mod.ALGORITHMS))
    ap.add_argument("--trace", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--buffer-max", type=float, default=30.0)
    ap.add_argument("--rtt-ms", type=float, default=80.0)
    ap.add_argument("--max-segments", type=int, default=None)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--no-telemetry", action="store_true")
    raise SystemExit(asyncio.run(main_async(ap.parse_args())))


if __name__ == "__main__":
    main()
