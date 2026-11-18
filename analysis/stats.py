"""Статистические процедуры анализа (без зависимостей от формата сырых данных)."""
from __future__ import annotations

import numpy as np
from scipy import stats

ALPHA = 0.05


def cliffs_delta(a, b) -> float:
    """δ Клиффа: P(a > b) − P(a < b). Положительное значение — a больше."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    if not len(a) or not len(b):
        return float("nan")
    return float(np.sign(np.subtract.outer(a, b)).mean())


def delta_magnitude(d: float) -> str:
    """Пороги Romano et al. (2006), как в задании: 0,147 / 0,33 / 0,474."""
    d = abs(d)
    if d < 0.147:
        return "пренебрежимый"
    if d < 0.33:
        return "малый"
    if d < 0.474:
        return "средний"
    return "большой"


def boot_ci(x, fn=np.median, b: int = 5000, seed: int = 0, level: float = 0.95) -> tuple[float, float]:
    x = np.asarray(x, float)
    if len(x) < 2:
        return (float("nan"), float("nan"))
    res = stats.bootstrap((x,), fn, n_resamples=b, confidence_level=level, random_state=seed, method="percentile")
    return float(res.confidence_interval.low), float(res.confidence_interval.high)


def boot_diff_median(a, b, n: int = 5000, seed: int = 0) -> tuple[float, float, float]:
    """Разность медиан (b − a) и её 95 % percentile-bootstrap интервал (независимые выборки)."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    rng = np.random.default_rng(seed)
    ia = rng.integers(0, len(a), (n, len(a)))
    ib = rng.integers(0, len(b), (n, len(b)))
    diffs = np.median(b[ib], axis=1) - np.median(a[ia], axis=1)
    return float(np.median(b) - np.median(a)), float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))


def boot_paired_median(d, n: int = 5000, seed: int = 0) -> tuple[float, float, float]:
    d = np.asarray(d, float)
    lo, hi = boot_ci(d, np.median, n, seed)
    return float(np.median(d)), lo, hi


def mann_whitney(a, b) -> float:
    a, b = np.asarray(a, float), np.asarray(b, float)
    if len(a) < 2 or len(b) < 2:
        return float("nan")
    return float(stats.mannwhitneyu(a, b, alternative="two-sided").pvalue)


def wilcoxon(a, b) -> float:
    d = np.asarray(b, float) - np.asarray(a, float)
    if len(d) < 2 or np.allclose(d, 0):
        return 1.0
    return float(stats.wilcoxon(d, zero_method="wilcox", alternative="two-sided").pvalue)


def holm(pvals: list[float]) -> list[float]:
    """Поправка Холма–Бонферрони (step-down), NaN пропускаются."""
    p = np.asarray(pvals, float)
    idx = [i for i in np.argsort(p) if not np.isnan(p[i])]
    m = len(idx)
    adj = np.full(len(p), np.nan)
    running = 0.0
    for rank, i in enumerate(idx):
        running = max(running, min(1.0, (m - rank) * p[i]))
        adj[i] = running
    return adj.tolist()


def iqr(x) -> float:
    x = np.asarray(x, float)
    return float(np.percentile(x, 75) - np.percentile(x, 25)) if len(x) else float("nan")


def cov(x) -> float:
    x = np.asarray(x, float)
    return float(np.std(x, ddof=1) / np.mean(x)) if len(x) > 1 and np.mean(x) else float("nan")


def _bisect_power(power_at, lo: float, hi: float, target: float, iters: int = 12) -> float:
    """Наименьший сдвиг с мощностью ≥ target (мощность растёт со сдвигом)."""
    if power_at(hi) < target:
        return float("nan")
    for _ in range(iters):
        mid = (lo + hi) / 2
        if power_at(mid) >= target:
            hi = mid
        else:
            lo = mid
    return float(hi)


def mde_relative(values, n1: int, n2: int, power: float = 0.8, sims: int = 400, seed: int = 0,
                 max_shift: float = 2.0) -> float:
    """Минимальный обнаружимый эффект по данным A/A (симуляция, независимые группы).

    Берём выборки с возвращением из наблюдённого шума и умножаем вторую на (1 + s).
    Возвращает наименьший относительный сдвиг s, при котором тест Манна–Уитни находит
    разницу (α = 0,05) с вероятностью ≥ power. Поиск — бисекцией по s.
    """
    v = np.asarray(values, float)

    def power_at(s: float) -> float:
        rng = np.random.default_rng(seed)  # общие случайные числа для всех s
        return float(np.mean([mann_whitney(rng.choice(v, n1), rng.choice(v, n2) * (1 + s)) < ALPHA
                              for _ in range(sims)]))

    return _bisect_power(power_at, 0.0, max_shift, power)


def mde_paired_abs(diffs, n: int, power: float = 0.8, sims: int = 400, seed: int = 0) -> float:
    """MDE для парного плана: шум — разности «близнецов» A/A (центрированные); к ним добавляется
    сдвиг δ, критерий Уилкоксона на n парах. Возвращает наименьший δ (в единицах метрики)."""
    d = np.asarray(diffs, float)
    d = d - np.median(d)
    scale = max(float(np.max(np.abs(d))), 1e-6)

    def power_at(delta: float) -> float:
        rng = np.random.default_rng(seed)
        hits = 0
        for _ in range(sims):
            x = rng.choice(d, n) + delta
            hits += (not np.allclose(x, 0)) and float(stats.wilcoxon(x).pvalue) < ALPHA
        return hits / sims

    return _bisect_power(power_at, 0.0, 10 * scale, power)


def ecdf(x):
    x = np.sort(np.asarray(x, float))
    return x, np.arange(1, len(x) + 1) / len(x)
