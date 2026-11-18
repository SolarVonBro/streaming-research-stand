"""Алгоритмы адаптации битрейта (ABR).

Каждый алгоритм получает состояние плеера и возвращает (уровень, пауза_перед_запросом_с).
Реализации следуют первоисточникам; где первоисточник допускает варианты, выбран вариант
из открытых эталонных реализаций (dash.js, артефакт Pensieve) — это указано в докстрингах.

* rate      — по пропускной способности: гармоническое среднее 5 последних замеров × 0,9
              (базовый алгоритм RB в Pensieve, правило ThroughputRule в dash.js);
* bola      — BOLA-BASIC (Spiteri et al., INFOCOM 2016) с параметризацией dash.js;
* robustmpc — RobustMPC (Yin et al., SIGCOMM 2015), горизонт 5, как в test/mpc.py Pensieve;
* festive   — FESTIVE (Jiang et al., CoNEXT 2012): гармоническое среднее 20 замеров,
              постепенное повышение, отложенное обновление, рандомизированный буфер.
"""
from __future__ import annotations

import itertools
import math
import random
from dataclasses import dataclass, field

import numpy as np


@dataclass
class AbrState:
    bitrates_kbps: list[int]
    segment_seconds: float
    next_index: int                 # номер следующего сегмента, с 0
    total_segments: int
    buffer_s: float
    now: float                      # часы плеера, с
    last_level: int | None = None
    throughputs_mbps: list[float] = field(default_factory=list)
    levels: list[int] = field(default_factory=list)
    switch_times: list[float] = field(default_factory=list)


def harmonic_mean(xs: list[float]) -> float:
    xs = [max(x, 1e-6) for x in xs]
    return len(xs) / sum(1.0 / x for x in xs)


def highest_below(bitrates_kbps: list[int], kbps: float) -> int:
    level = 0
    for i, b in enumerate(bitrates_kbps):
        if b <= kbps:
            level = i
    return level


class Abr:
    name = "base"

    def choose(self, s: AbrState) -> tuple[int, float]:
        raise NotImplementedError

    def on_download(self, s: AbrState, throughput_mbps: float) -> None:  # noqa: B027
        """Хук после загрузки сегмента (нужен RobustMPC для учёта ошибки прогноза)."""


class RateBased(Abr):
    name = "rate"
    window = 5
    safety = 0.9

    def choose(self, s: AbrState) -> tuple[int, float]:
        if not s.throughputs_mbps:
            return 0, 0.0
        est = harmonic_mean(s.throughputs_mbps[-self.window:])
        return highest_below(s.bitrates_kbps, est * 1000 * self.safety), 0.0


class Bola(Abr):
    """BOLA-BASIC (Spiteri et al., IEEE/ACM ToN 2020, §IV, ур. (10)–(11)) в секундах буфера,
    параметризация как в симуляторе Sabre (класс Bola):

      полезность  υ_m = ln(S_m / S_1);
      γp = 5 с (значение из оценки в статье);
      V·p = (B_max − p) / (υ_M + γp), чтобы при полном буфере выбирался высший уровень;
      уровень     m* = argmax (V·p·(υ_m + γp) − Q) / S_m;
      пауза       пока Q > V·p·(υ_M + γp) — ни одно действие не выгодно.
    Размер S_m оценивается номинальным битрейтом (так делают dash.js и Sabre).
    """

    name = "bola"
    gp = 5.0

    def __init__(self, buffer_max_s: float = 30.0):
        self.buffer_max_s = buffer_max_s
        self._params: tuple[list[float], float] | None = None

    def params(self, bitrates: list[int], segment_s: float) -> tuple[list[float], float]:
        if self._params is None:
            utilities = [math.log(b / bitrates[0]) for b in bitrates]
            vp = (self.buffer_max_s - segment_s) / (utilities[-1] + self.gp)
            self._params = (utilities, vp)
        return self._params

    def choose(self, s: AbrState) -> tuple[int, float]:
        utilities, vp = self.params(s.bitrates_kbps, s.segment_seconds)
        scores = [(vp * (u + self.gp) - s.buffer_s) / b for u, b in zip(utilities, s.bitrates_kbps, strict=True)]
        level = int(np.argmax(scores))
        delay = max(0.0, s.buffer_s - vp * (utilities[-1] + self.gp))
        return level, delay


class RobustMpc(Abr):
    """RobustMPC: максимизация QoE_lin на горизонте 5 сегментов перебором всех комбинаций.

    Прогноз = гармоническое среднее 5 последних замеров / (1 + max ошибки 5 последних прогнозов).
    QoE_lin = Σ R [Мбит/с] − μ·T_rebuf − Σ|ΔR|, μ = 4,3 (максимальный битрейт лестницы), как в Pensieve.
    Размер будущего сегмента оценивается как битрейт × длительность (клиент не знает точных размеров).
    """

    name = "robustmpc"
    horizon = 5
    window = 5
    rebuf_penalty = 4.3
    smooth_penalty = 1.0

    def __init__(self):
        self.errors: list[float] = []
        self.last_prediction: float | None = None
        self._combos: dict[tuple[int, int], np.ndarray] = {}

    def combos(self, levels: int, horizon: int) -> np.ndarray:
        key = (levels, horizon)
        if key not in self._combos:
            self._combos[key] = np.array(list(itertools.product(range(levels), repeat=horizon)), dtype=np.int64)
        return self._combos[key]

    def on_download(self, s: AbrState, throughput_mbps: float) -> None:
        if self.last_prediction is not None:
            self.errors.append(abs(self.last_prediction - throughput_mbps) / max(throughput_mbps, 1e-6))

    def choose(self, s: AbrState) -> tuple[int, float]:
        if not s.throughputs_mbps:
            self.last_prediction = None
            return 0, 0.0
        hm = harmonic_mean(s.throughputs_mbps[-self.window:])
        self.last_prediction = hm
        max_err = max(self.errors[-self.window:], default=0.0)
        bw = hm / (1.0 + max_err)

        horizon = min(self.horizon, s.total_segments - s.next_index)
        combos = self.combos(len(s.bitrates_kbps), horizon)
        rates = np.array(s.bitrates_kbps, dtype=float)[combos] / 1000.0      # Мбит/с
        dl = rates * s.segment_seconds / bw                                    # с
        buffer = np.full(len(combos), s.buffer_s)
        rebuf = np.zeros(len(combos))
        last = (s.bitrates_kbps[s.last_level] / 1000.0) if s.last_level is not None else rates[:, 0]
        smooth = np.zeros(len(combos))
        prev = np.broadcast_to(last, (len(combos),)).astype(float)
        for j in range(horizon):
            rebuf += np.maximum(dl[:, j] - buffer, 0.0)
            buffer = np.maximum(buffer - dl[:, j], 0.0) + s.segment_seconds
            smooth += np.abs(rates[:, j] - prev)
            prev = rates[:, j]
        reward = rates.sum(axis=1) - self.rebuf_penalty * rebuf - self.smooth_penalty * smooth
        return int(combos[int(np.argmax(reward)), 0]), 0.0


class Festive(Abr):
    """FESTIVE (Jiang et al., CoNEXT 2012, §3.1–3.5) для одного клиента.

    1) Оценка полосы w — гармоническое среднее последних 20 замеров (пока их меньше — по имеющимся;
       в статье до накопления замеров переключений нет, но тогда плеер ~80 с стоял бы на низшем уровне).
    2) Эталонный уровень: только соседний; понижение сразу, если b_cur > p·w (p = 0,85);
       повышение с уровня k (с нуля) — только после k сегментов на этом уровне.
    3) Отложенное обновление: из b_cur и b_ref выбирается меньший
       score = stability + α·efficiency, stability = 2^n для b_cur и 2^n + 1 для b_ref
       (n — переключений за последние 20 с), efficiency = |b / min(p·w, b_ref) − 1|, α = 12.
    4) Рандомизированное планирование: запрос, когда буфер ниже randbuf ~ U(target − δ, target + δ],
       δ = длительность сегмента, target = 30 с (пример из статьи). При общем пределе буфера плеера
       26 с рандомизация в одиночном сценарии не срабатывает — она нужна при конкуренции плееров.
    """

    name = "festive"
    window = 20
    p = 0.85
    alpha = 12.0
    stability_window_s = 20.0

    def __init__(self, target_buffer_s: float = 30.0, seed: int = 0):
        self.target_buffer_s = target_buffer_s
        self.rng = random.Random(seed)

    def choose(self, s: AbrState) -> tuple[int, float]:
        d = s.segment_seconds
        randbuf = self.target_buffer_s - d + 2 * d * (1 - self.rng.random())  # (target − δ, target + δ]
        delay = max(0.0, s.buffer_s - randbuf)
        if not s.throughputs_mbps or s.last_level is None:
            return 0, delay
        est_kbps = harmonic_mean(s.throughputs_mbps[-self.window:]) * 1000
        cur = s.last_level
        b = s.bitrates_kbps
        ref = cur
        if b[cur] > self.p * est_kbps and cur > 0:
            ref = cur - 1
        elif cur + 1 < len(b) and b[cur + 1] <= self.p * est_kbps:
            streak = 0
            for lv in reversed(s.levels):
                if lv != cur:
                    break
                streak += 1
            if streak >= cur:
                ref = cur + 1
        if ref == cur:
            return cur, delay
        n = sum(1 for t in s.switch_times if s.now - t <= self.stability_window_s)
        w = self.p * est_kbps

        def score(level: int) -> float:
            stability = 2.0 ** n + (1.0 if level == ref else 0.0)
            efficiency = abs(b[level] / min(w, b[ref]) - 1.0)
            return stability + self.alpha * efficiency

        return (ref if score(ref) < score(cur) else cur), delay


def make(name: str, seed: int = 0, buffer_max_s: float = 30.0) -> Abr:
    if name == "rate":
        return RateBased()
    if name == "bola":
        return Bola(buffer_max_s=buffer_max_s)
    if name == "robustmpc":
        return RobustMpc()
    if name == "festive":
        return Festive(seed=seed)
    raise ValueError(f"неизвестный алгоритм {name!r}")


ALGORITHMS = ("rate", "bola", "robustmpc", "festive")
