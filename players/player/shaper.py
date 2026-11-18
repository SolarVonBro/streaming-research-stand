"""Эмуляция канала по трассе пропускной способности на стороне клиента.

Трасса — кусочно-постоянная функция bw(t) [Мбит/с] с шагом ~1 с (формат Pensieve:
время, пропускная способность). Байты ответа читаются из сокета по мере поступления,
а «доставленными» считаются не раньше момента, когда канал с пропускной способностью
bw(t) успел бы их передать. Плюс фиксированная задержка RTT перед первым байтом
(как LINK_RTT = 80 мс в симуляторе Pensieve).

Если сервер отдаёт медленнее трассы (перегрузка), неиспользованная ёмкость канала
пропадает — так задержка сервера честно попадает в измеренную пропускную способность.
"""
from __future__ import annotations

import bisect
import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Trace:
    name: str
    starts: list[float]       # начало каждого интервала, с
    rates: list[float]        # байт/с на интервале
    duration: float

    @classmethod
    def load(cls, path: str | Path) -> Trace:
        times, mbps = [], []
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                times.append(float(row["t_s"]))
                mbps.append(float(row["mbps"]))
        if len(times) < 2:
            raise ValueError(f"{path}: слишком короткая трасса")
        last = times[-1] - times[-2]
        rates = [m * 125_000.0 for m in mbps]  # Мбит/с -> байт/с
        if not any(rates):
            raise ValueError(f"{path}: нулевая трасса")
        return cls(Path(path).stem, times, rates, times[-1] + last)

    def mean_mbps(self) -> float:
        total = 0.0
        for i, r in enumerate(self.rates):
            end = self.starts[i + 1] if i + 1 < len(self.starts) else self.duration
            total += r * (end - self.starts[i])
        return total / self.duration / 125_000.0


class Transfer:
    """Одна загрузка сегмента по эмулируемому каналу.

    pos — время трассы (без свёртки), wall — момент на часах плеера, до которого
    канал уже «передал» учтённые байты.
    """

    def __init__(self, trace: Trace, trace_pos: float, wall_start: float, rtt_s: float):
        self.trace = trace
        self.pos = trace_pos + rtt_s
        self.wall = wall_start + rtt_s

    def _interval(self) -> tuple[float, float]:
        t = self.pos % self.trace.duration
        i = bisect.bisect_right(self.trace.starts, t) - 1
        end = self.trace.starts[i + 1] if i + 1 < len(self.trace.starts) else self.trace.duration
        return self.trace.rates[i], end - t

    def deliver(self, nbytes: int, available_at: float | None) -> float:
        """Учесть nbytes новых байт, вернуть момент (часы плеера), когда они доставлены.

        available_at — когда байты могли начать передаваться по каналу (None — уже были готовы).
        """
        if available_at is not None and available_at > self.wall:
            # сервер задержал данные: ёмкость канала за это время потеряна
            self.pos += available_at - self.wall
            self.wall = available_at
        remaining = float(nbytes)
        while remaining > 0:
            rate, span = self._interval()
            if span <= 1e-9:
                self.pos += 1e-6
                continue
            capacity = rate * span
            if rate > 0 and capacity >= remaining:
                dt = remaining / rate
                remaining = 0.0
            else:
                dt = span
                remaining -= capacity
            self.pos += dt
            self.wall += dt
        return self.wall
