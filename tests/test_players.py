"""Модульные тесты эмулятора плееров (без Docker)."""
from __future__ import annotations

import asyncio
import csv
import json
import time
from pathlib import Path

import httpx
import pytest
from player import abr
from player.session import Player, parse_mpd
from player.shaper import Trace, Transfer

LADDER = [300, 750, 1200, 1850, 2850, 4300]


def write_trace(path: Path, mbps: float | list[float], seconds: int = 400) -> Path:
    values = mbps if isinstance(mbps, list) else [mbps] * seconds
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["t_s", "mbps"])
        for t, v in enumerate(values):
            w.writerow([t, v])
    return path


def state(**kw) -> abr.AbrState:
    base = dict(bitrates_kbps=LADDER, segment_seconds=4.0, next_index=10, total_segments=48,
                buffer_s=10.0, now=40.0, last_level=2, throughputs_mbps=[], levels=[2] * 10)
    base.update(kw)
    return abr.AbrState(**base)


# --- канал --------------------------------------------------------------------------------

def test_transfer_constant_rate(tmp_path):
    """Калибровка: 1 МБ по каналу 2 Мбит/с = 4 с + RTT."""
    tr = Trace.load(write_trace(tmp_path / "c.csv", 2.0))
    link = Transfer(tr, trace_pos=0.0, wall_start=100.0, rtt_s=0.08)
    done = link.deliver(1_000_000, available_at=100.0)
    assert done == pytest.approx(100.0 + 0.08 + 4.0, abs=1e-6)


def test_transfer_piecewise_and_outage(tmp_path):
    tr = Trace.load(write_trace(tmp_path / "p.csv", [1.0, 0.0, 1.0, 1.0]))
    link = Transfer(tr, trace_pos=0.0, wall_start=0.0, rtt_s=0.0)
    # 1-я секунда отдаёт 125 000 байт, 2-я — 0 (провал), 3-я — ещё 125 000
    assert link.deliver(250_000, available_at=None) == pytest.approx(3.0)


def test_transfer_server_delay_wastes_capacity(tmp_path):
    tr = Trace.load(write_trace(tmp_path / "c.csv", 8.0))
    link = Transfer(tr, trace_pos=0.0, wall_start=0.0, rtt_s=0.0)
    # сервер отдал первый байт только через 0,5 с: ёмкость канала за это время не накапливается
    assert link.deliver(1_000_000, available_at=0.5) == pytest.approx(1.5)


def test_trace_mean(tmp_path):
    tr = Trace.load(write_trace(tmp_path / "m.csv", [1.0, 3.0]))
    assert tr.mean_mbps() == pytest.approx(2.0)


# --- алгоритмы ----------------------------------------------------------------------------

def test_rate_based_picks_highest_sustainable():
    level, wait = abr.RateBased().choose(state(throughputs_mbps=[2.2] * 5))
    assert (level, wait) == (3, 0.0)  # 0,9 × 2,2 = 1,98 Мбит/с -> 1850 кбит/с


def test_rate_based_harmonic_mean_is_conservative():
    level, _ = abr.RateBased().choose(state(throughputs_mbps=[10, 10, 10, 10, 0.5]))
    assert level <= 4


def test_bola_buffer_driven():
    b = abr.Bola()
    assert b.choose(state(buffer_s=2.0))[0] == 0
    assert b.choose(state(buffer_s=25.0))[0] == 5
    levels = [b.choose(state(buffer_s=x))[0] for x in range(0, 27)]
    assert levels == sorted(levels)  # монотонно по буферу


def test_bola_waits_above_target():
    level, wait = abr.Bola().choose(state(buffer_s=40.0))
    assert level == 5 and wait > 0


def test_robustmpc_high_and_low_bandwidth():
    m = abr.RobustMpc()
    assert m.choose(state(throughputs_mbps=[20.0] * 5, buffer_s=20.0))[0] == 5
    m2 = abr.RobustMpc()
    assert m2.choose(state(throughputs_mbps=[0.4] * 5, buffer_s=1.0, last_level=0))[0] == 0


def test_robustmpc_error_discount():
    m = abr.RobustMpc()
    s = state(throughputs_mbps=[3.0] * 5, buffer_s=8.0)
    first = m.choose(s)[0]
    m.on_download(s, 1.0)  # прогноз 3,0 при факте 1,0 -> ошибка 200 %
    assert m.choose(s)[0] < first


def test_festive_gradual_up_switch():
    f = abr.Festive(seed=1)
    # на уровне 2 провели только 1 сегмент: повышения нет
    assert f.choose(state(throughputs_mbps=[20.0] * 20, levels=[1, 2], last_level=2))[0] == 2
    # на уровне 2 провели 2 сегмента: повышение ровно на один уровень
    assert f.choose(state(throughputs_mbps=[20.0] * 20, levels=[2, 2], last_level=2))[0] == 3


def test_festive_down_switch_immediate():
    f = abr.Festive(seed=1)
    assert f.choose(state(throughputs_mbps=[1.0] * 20, last_level=4))[0] == 3


def test_make_all():
    for name in abr.ALGORITHMS:
        assert abr.make(name).name == name


# --- сессия -------------------------------------------------------------------------------

MPD = """<MPD mediaPresentationDuration="PT192S"><SegmentTemplate timescale="1" duration="4" startNumber="1"
media="/media/v/$RepresentationID$/$Number$.m4s"/>""" + "".join(
    f'<Representation id="{i}" bandwidth="{b * 1000}"/>' for i, b in enumerate(LADDER)) + "</MPD>"


def test_parse_mpd():
    assert parse_mpd(MPD) == (LADDER, 4.0, 48, "/media/v/$RepresentationID$/$Number$.m4s")


def fake_server(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/v1/sessions":
        return httpx.Response(201, json={"token": "t.x", "manifest": "/m.mpd"})
    if request.url.path == "/m.mpd":
        return httpx.Response(200, text=MPD)
    if request.url.path.startswith("/media/"):
        level = int(request.url.path.split("/")[3])
        body = b"\0" * (LADDER[level] * 1000 * 4 // 8)
        return httpx.Response(200, stream=httpx.ByteStream(body))
    return httpx.Response(202, json={})


def test_calibration_constant_link(tmp_path):
    """Известный результат: сегмент 150 000 байт по каналу 2 Мбит/с с RTT 80 мс
    грузится 0,6 + 0,08 = 0,68 с, т. е. измеренная пропускная способность 1,765 Мбит/с."""
    tr = Trace.load(write_trace(tmp_path / "c2.csv", 2.0))

    async def go() -> Player:
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake_server), base_url="http://sut") as c:
            p = Player("cal", abr.RateBased(), tr, c, buffer_max_s=30, rtt_s=0.08, max_segments=3)
            p.abr.choose = lambda s: (0, 0.0)  # фиксируем низший уровень
            await p.run()
            return p

    p = asyncio.run(go())
    for log in p.logs:
        assert log.download_s == pytest.approx(0.68, rel=0.05)
        assert log.throughput_mbps == pytest.approx(1.765, rel=0.05)


def test_short_session_no_stalls(tmp_path):
    """На канале 40 Мбит/с пять сегментов грузятся без остановок, уровень растёт."""
    tr = Trace.load(write_trace(tmp_path / "fast.csv", 40.0))

    async def go() -> Player:
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake_server), base_url="http://sut") as c:
            p = Player("p0", abr.RateBased(), tr, c, buffer_max_s=30, rtt_s=0.01, max_segments=5)
            await p.run()
            return p

    t = time.monotonic()
    p = asyncio.run(go())
    assert time.monotonic() - t < 10
    assert len(p.logs) == 5
    assert p.summary["rebuffer_s"] == 0
    assert p.logs[0].level == 0 and p.logs[-1].level == 5
    assert p.logs[1].throughput_mbps == pytest.approx(40.0, rel=0.35)
    p.dump(tmp_path)
    assert json.loads((tmp_path / "session_p0.json").read_text())["segments"] == 5
