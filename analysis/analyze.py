"""Анализ: сырые данные results/raw -> таблицы results/processed и графики results/figures.

    python analysis/analyze.py                 # все найденные эксперименты
    python analysis/analyze.py --raw results/raw --out results

Единица наблюдения:
  * E1, A/A-E1, калибровка, пилот — один прогон (p95 задержки сегмента за окно измерения);
  * E2, A/A-E2, E3 — одна сессия плеера (QoE за всё видео); сессии разных алгоритмов
    на одной трассе связаны (парный план).
Отдельные запросы не считаются независимыми наблюдениями: внутри прогона они зависят
от общей очереди сервера, прогрева и фоновых процессов.
"""
from __future__ import annotations

import argparse
import gzip
import itertools
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import stats as st  # noqa: E402
from scipy import stats as sps  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REBUF_PENALTY = 4.3  # μ в QoE_lin: максимальный битрейт лестницы, Мбит/с (Pensieve, MPC)
SEGMENT_S = 4.0
SEED = 20261118

# Палитра (справочная палитра навыка dataviz, светлая тема): фиксированный порядок слотов
C = {"blue": "#2a78d6", "orange": "#eb6834", "aqua": "#1baf7a", "yellow": "#eda100",
     "ink": "#0b0b0b", "ink2": "#52514e", "grid": "#e4e3df", "surface": "#fcfcfb"}
ARCH_COLOR = {"mono": C["blue"], "micro": C["orange"]}
ABR_ORDER = ["rate", "bola", "robustmpc", "festive"]
ABR_COLOR = dict(zip(ABR_ORDER, [C["blue"], C["orange"], C["aqua"], C["yellow"]], strict=True))
ABR_LABEL = {"rate": "Rate", "bola": "BOLA", "robustmpc": "RobustMPC", "festive": "FESTIVE"}
ARCH_LABEL = {"mono": "монолит", "micro": "микросервисы"}

plt.rcParams.update({
    "figure.facecolor": C["surface"], "axes.facecolor": C["surface"], "savefig.facecolor": C["surface"],
    "axes.edgecolor": C["ink2"], "axes.labelcolor": C["ink"], "xtick.color": C["ink2"], "ytick.color": C["ink2"],
    "axes.grid": True, "grid.color": C["grid"], "grid.linewidth": 0.8, "axes.spines.top": False,
    "axes.spines.right": False, "font.size": 10, "axes.titlesize": 11, "legend.frameon": False,
    "figure.dpi": 110,
})


# ===================================================================== загрузка

def experiments(raw: Path) -> dict[str, list[tuple[Path, dict]]]:
    out = {}
    for exp in sorted(p for p in raw.iterdir() if p.is_dir() and ".prev-" not in p.name):
        runs = []
        for run in sorted(p for p in exp.iterdir() if p.is_dir()):
            mf = run / "manifest.json"
            if mf.exists():
                runs.append((run, json.loads(mf.read_text(encoding="utf-8"))))
        if runs:
            out[exp.name] = runs
    return out


def container_cpu(run: Path, pattern: str, skip_start_s: float = 5.0) -> tuple[float, float]:
    """Средняя и максимальная загрузка CPU контейнеров по шаблону имени (% одного ядра).

    Первые skip_start_s секунд жизни каждого контейнера отбрасываются: первый замер
    `docker stats` захватывает запуск интерпретатора и импорт библиотек, а не работу.
    """
    f = run / "docker_stats.csv"
    if not f.exists():
        return float("nan"), float("nan")
    df = pd.read_csv(f)
    df = df[df["container"].str.contains(pattern, regex=True)]
    if not df.empty:
        first = df.groupby("container")["t_unix"].transform("min")
        df = df[df["t_unix"] - first >= skip_start_s]
    if df.empty:
        return float("nan"), float("nan")
    per_t = df.groupby("t_unix")["cpu_percent"].sum()
    return float(per_t.mean()), float(per_t.max())


def load_run_row(run: Path, m: dict) -> dict | None:
    raw = run / "requests.csv.gz"
    if not raw.exists():
        return None
    with gzip.open(raw, "rt", encoding="utf-8") as f:
        req = pd.read_csv(f)
    seg = req[req["name"] == "/media/[segment]"]
    window = m.get("measure_window", {})
    dur = (window.get("to_s", 0) - window.get("from_s", 0)) or m["timing"].get("duration_s") or 1
    res = m["resources"]
    load_mean, load_max = container_cpu(run, r"load-run")
    sut_pat = r"stand-(app|auth|catalog|delivery|telemetry|edge-mono|edge-micro)-"
    sut_mean, _ = container_cpu(run, sut_pat)
    row = {
        "experiment": m["experiment"], "run_id": m["run_id"], "order": m["order"], "rep": m["repetition"],
        **{k: v for k, v in m["params"].items() if k not in ("trace", "network")},
        "status": m["status"],
        "p50_ms": float(np.percentile(seg["response_ms"], 50)) if len(seg) else np.nan,
        "p95_ms": float(np.percentile(seg["response_ms"], 95)) if len(seg) else np.nan,
        "p99_ms": float(np.percentile(seg["response_ms"], 99)) if len(seg) else np.nan,
        "rps": len(req) / dur, "seg_rps": len(seg) / dur,
        "error_rate": float(req["failed"].mean()) if len(req) else np.nan,
        "requests": len(req),
        "gen_cpu_mean_frac": load_mean / (100 * float(res["LOAD_CPUS"])),
        "gen_cpu_max_frac": load_max / (100 * float(res["LOAD_CPUS"])),
        "sut_cpu_cores": sut_mean / 100,
    }
    row.update(prometheus_row(run, m))
    lc = run / "locust_stats.csv"
    if lc.exists():
        ls = pd.read_csv(lc)
        r = ls[ls["Name"] == "/media/[segment]"]
        if len(r):
            row["locust_p95_ms"] = float(r.iloc[0]["95%"])
    return row


def prometheus_row(run: Path, m: dict) -> dict:
    """Серверная сторона из экспорта Prometheus: медиана по времени прогона для сервиса выдачи."""
    f = run / "prometheus.json"
    if not f.exists():
        return {}
    pj = json.loads(f.read_text(encoding="utf-8"))
    inst = "app:8000" if m["params"].get("arch") == "mono" else "delivery:8000"
    out = {}
    for key, col in (("auth_check_p50_s", "auth_p50_ms"), ("auth_check_p95_s", "auth_p95_ms"),
                     ("server_p50_s", "server_p50_ms"), ("server_p95_s", "server_p95_ms")):
        series = pj.get(key)
        if not isinstance(series, list):
            continue
        for sr in series:
            if sr["metric"].get("instance") == inst:
                vals = [float(v[1]) for v in sr["values"] if v[1] not in ("NaN", "+Inf")]
                if vals:
                    out[col] = 1000 * float(np.median(vals))
    return out


def qoe_from_segments(seg: pd.DataFrame) -> dict:
    r = seg["bitrate_kbps"].to_numpy() / 1000.0
    n = len(r)
    rebuf = float(seg["rebuffer_s"].sum())
    smooth = float(np.abs(np.diff(r)).sum()) if n > 1 else 0.0
    return {
        "qoe": (r.sum() - REBUF_PENALTY * rebuf - smooth) / n,
        "bitrate_kbps": float(seg["bitrate_kbps"].mean()),
        "rebuffer_s": rebuf,
        "rebuffer_ratio_pct": 100 * rebuf / (n * SEGMENT_S),
        "switches": int((np.diff(seg["level"].to_numpy()) != 0).sum()),
        "smoothness_mbps": smooth / max(n - 1, 1),
        "ttfb_median_ms": 1000 * float(seg["ttfb_s"].median()),
        "segments": n,
    }


def player_rows(run: Path, m: dict) -> list[dict]:
    rows = []
    meta = json.loads((run / "players_meta.json").read_text()) if (run / "players_meta.json").exists() else {}
    pl_mean, pl_max = container_cpu(run, r"players-run")
    for sj in sorted(run.glob("session_*.json")):
        s = json.loads(sj.read_text(encoding="utf-8"))
        pid = s["player"]
        seg_file = run / f"segments_{pid}.csv"
        if "error" in s or not seg_file.exists():
            rows.append({"run_id": m["run_id"], "player": pid, "abr": s.get("abr"), "error": s.get("error", "нет данных")})
            continue
        seg = pd.read_csv(seg_file)
        rows.append({
            "experiment": m["experiment"], "run_id": m["run_id"], "order": m["order"], "rep": m["repetition"],
            "arch": m["params"].get("arch"), "network": m["params"].get("network"),
            "trace": Path(m["params"]["trace"]).stem, "player": pid, "abr": s["abr"],
            "trace_mean_mbps": s["trace_mean_mbps"], "startup_s": s["startup_s"], "http_errors": s["http_errors"],
            **qoe_from_segments(seg),
            "gen_cpu_share_core": meta.get("cpu_share_of_core"),
            "gen_cpu_max_frac": pl_max / (100 * float(m["resources"]["PLAYERS_CPUS"])),
            "status": m["status"],
        })
    return rows


# ===================================================================== графики

def jitter(n: int, seed: int = 0, width: float = 0.08) -> np.ndarray:
    return np.random.default_rng(seed).uniform(-width, width, n)


def boxes(ax, groups: list[np.ndarray], positions, colors, labels=None, width=0.55):
    bp = ax.boxplot(groups, positions=positions, widths=width, patch_artist=True, showfliers=False,
                    medianprops={"color": C["ink"], "linewidth": 1.6}, whiskerprops={"color": C["ink2"]},
                    capprops={"color": C["ink2"]}, boxprops={"linewidth": 0.8})
    for patch, col in zip(bp["boxes"], colors, strict=True):
        patch.set_facecolor(col + "40")
        patch.set_edgecolor(col)
    for i, (g, pos, col) in enumerate(zip(groups, positions, colors, strict=True)):
        ax.scatter(pos + jitter(len(g), i), g, s=22, color=col, edgecolor=C["surface"], linewidth=1.2, zorder=3)
    if labels is not None:
        ax.set_xticks(positions, labels)


def save(fig, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path.name


# ===================================================================== отчёт

class Report:
    def __init__(self):
        self.lines: list[str] = []
        self.data: dict = {}

    def h(self, text: str) -> None:
        self.lines += ["", f"## {text}", ""]

    def p(self, text: str) -> None:
        self.lines += [text, ""]

    def table(self, df: pd.DataFrame, floatfmt: str = "{:.3g}") -> None:
        cols = list(df.columns)
        self.lines.append("| " + " | ".join(map(str, cols)) + " |")
        self.lines.append("|" + "|".join("---" for _ in cols) + "|")
        for _, r in df.iterrows():
            cells = []
            for c in cols:
                v = r[c]
                if isinstance(v, float | np.floating):
                    cells.append("—" if np.isnan(v) else floatfmt.format(v))
                else:
                    cells.append(str(v))
            self.lines.append("| " + " | ".join(cells) + " |")
        self.lines.append("")


def fmt_ci(lo: float, hi: float, f: str = "{:.1f}") -> str:
    return f"[{f.format(lo)}; {f.format(hi)}]"


# ===================================================================== E1-подобные (нагрузка)

def summarize_groups(df: pd.DataFrame, keys: list[str], metric: str) -> pd.DataFrame:
    rows = []
    for k, g in df.groupby(keys):
        k = k if isinstance(k, tuple) else (k,)
        lo, hi = st.boot_ci(g[metric], seed=SEED)
        rows.append({**dict(zip(keys, k, strict=True)), "n": len(g), "медиана": g[metric].median(),
                     "IQR": st.iqr(g[metric]), "95 % ДИ медианы": fmt_ci(lo, hi), "CoV": st.cov(g[metric])})
    return pd.DataFrame(rows)


def analyze_e1(df: pd.DataFrame, rep: Report, fig_dir: Path) -> None:
    rep.h("E1. Архитектура сервера под нагрузкой (монолит vs микросервисы)")
    for metric, title in (("p95_ms", "p95 задержки сегмента, мс"), ("p50_ms", "p50 задержки сегмента, мс"),
                          ("p99_ms", "p99 задержки сегмента, мс"),
                          ("rps", "пропускная способность, запросов/с"), ("error_rate", "доля ошибок"),
                          ("sut_cpu_cores", "CPU SUT, ядер")):
        rep.p(f"**{title}** (единица — прогон):")
        rep.table(summarize_groups(df, ["users", "arch"], metric))
    diag = [c for c in ("server_p50_ms", "server_p95_ms", "auth_p50_ms", "auth_p95_ms") if c in df.columns]
    if diag:
        rep.p("**Разложение задержки на стороне сервера** (Prometheus, медиана по прогонам, мс): "
              "server — обработчик выдачи сегмента целиком, auth — проверка токена внутри него.")
        rep.table(df.groupby(["users", "arch"])[diag].median().reset_index())
    rows, pvals = [], []
    for users, g in df.groupby("users"):
        a = g[g["arch"] == "mono"]["p95_ms"]
        b = g[g["arch"] == "micro"]["p95_ms"]
        p = st.mann_whitney(a, b)
        d = st.cliffs_delta(b, a)
        diff, lo, hi = st.boot_diff_median(a, b, seed=SEED)
        pvals.append(p)
        rows.append({"users": users, "медиана mono": a.median(), "медиана micro": b.median(),
                     "Δ (micro − mono), мс": diff, "95 % ДИ Δ": fmt_ci(lo, hi),
                     "Δ, %": 100 * diff / a.median(), "p (M–W)": p, "δ Клиффа": d,
                     "величина": st.delta_magnitude(d)})
    adj = st.holm(pvals)
    for r, pa in zip(rows, adj, strict=True):
        r["p (Холм)"] = pa
    res = pd.DataFrame(rows)
    rep.p("**Проверка H1₀ (p95 не зависит от архитектуры)** — тест Манна–Уитни, двусторонний, "
          "поправка Холма на два уровня нагрузки:")
    rep.table(res)
    rep.data["e1_tests"] = res.to_dict(orient="records")
    # взаимодействие: растёт ли относительный проигрыш с нагрузкой
    ratios = {}
    for users, g in df.groupby("users"):
        a = g[g["arch"] == "mono"]["p95_ms"].to_numpy()
        b = g[g["arch"] == "micro"]["p95_ms"].to_numpy()
        rng = np.random.default_rng(SEED)
        boots = [np.median(rng.choice(b, len(b))) / np.median(rng.choice(a, len(a))) for _ in range(4000)]
        ratios[users] = (np.median(b) / np.median(a), np.percentile(boots, 2.5), np.percentile(boots, 97.5))
    rep.p("**Отношение медиан p95 micro/mono** (bootstrap 95 % ДИ): " + "; ".join(
        f"{u} зрителей — {r[0]:.2f} {fmt_ci(r[1], r[2], '{:.2f}')}" for u, r in ratios.items()))
    rep.data["e1_ratios"] = {str(k): v for k, v in ratios.items()}

    levels = sorted(df["users"].unique())
    fig, ax = plt.subplots(figsize=(7, 4))
    pos, groups, cols, ticks, tlabels = [], [], [], [], []
    for i, u in enumerate(levels):
        for j, arch in enumerate(("mono", "micro")):
            groups.append(df[(df["users"] == u) & (df["arch"] == arch)]["p95_ms"].to_numpy())
            pos.append(i * 2.5 + j)
            cols.append(ARCH_COLOR[arch])
        ticks.append(i * 2.5 + 0.5)
        tlabels.append(f"{u} зрителей")
    boxes(ax, groups, pos, cols)
    ax.set_xticks(ticks, tlabels)
    ax.set_ylabel("p95 задержки сегмента за прогон, мс")
    ax.set_title("E1: p95 по прогонам (точка = прогон)")
    for arch in ("mono", "micro"):
        ax.scatter([], [], color=ARCH_COLOR[arch], label=ARCH_LABEL[arch])
    ax.legend(loc="upper left")
    save(fig, fig_dir / "e1_p95_box.png")


def ecdf_plot(runs: list[tuple[Path, dict]], fig_dir: Path, name: str, title: str) -> None:
    pooled: dict[tuple, list] = {}
    for run, m in runs:
        f = run / "requests.csv.gz"
        if not f.exists():
            continue
        with gzip.open(f, "rt", encoding="utf-8") as fh:
            req = pd.read_csv(fh)
        key = (m["params"].get("arch"), m["params"].get("users"))
        pooled.setdefault(key, []).append(req[req["name"] == "/media/[segment]"]["response_ms"].to_numpy())
    if not pooled:
        return
    fig, ax = plt.subplots(figsize=(7, 4))
    for (arch, users), parts in sorted(pooled.items(), key=lambda x: (x[0][1], x[0][0])):
        x, y = st.ecdf(np.concatenate(parts))
        ls = "-" if users == max(k[1] for k in pooled) else "--"
        ax.plot(x, y, ls, color=ARCH_COLOR.get(arch, C["ink2"]), linewidth=2,
                label=f"{ARCH_LABEL.get(arch, arch)}, {users} зрителей")
    ax.set_xscale("log")
    ax.axhline(0.95, color=C["ink2"], linewidth=0.8, linestyle=":")
    ax.text(ax.get_xlim()[0], 0.955, " p95", color=C["ink2"], fontsize=8, va="bottom")
    ax.set_xlabel("задержка запроса сегмента, мс (лог. шкала)")
    ax.set_ylabel("доля запросов")
    ax.set_title(title)
    ax.legend(loc="lower right")
    save(fig, fig_dir / name)


def stability(df: pd.DataFrame, keys: list[str], metric: str, rep: Report, fig_dir: Path, name: str,
              title: str) -> dict:
    d = df.copy()
    d["norm"] = d[metric] / d.groupby(keys)[metric].transform("median")
    rho, p = sps.spearmanr(d["order"], d["norm"])
    fig, ax = plt.subplots(figsize=(7, 3.2))
    ax.scatter(d["order"], d["norm"], s=24, color=C["blue"], edgecolor=C["surface"], linewidth=1.2)
    if len(d) > 2:
        k, b = np.polyfit(d["order"], d["norm"], 1)
        xs = np.array([d["order"].min(), d["order"].max()])
        ax.plot(xs, k * xs + b, color=C["ink2"], linewidth=1.2, linestyle="--", label=f"тренд, ρ Спирмена = {rho:.2f}")
        ax.legend(loc="upper right")
    ax.axhline(1.0, color=C["ink2"], linewidth=0.8)
    ax.set_xlabel("порядковый номер прогона")
    ax.set_ylabel(f"{metric} / медиана конфигурации")
    ax.set_title(title)
    save(fig, fig_dir / name)
    return {"spearman_rho": float(rho), "p": float(p), "n": int(len(d))}


def analyze_aa_load(df: pd.DataFrame, rep: Report, fig_dir: Path) -> None:
    rep.h("A/A-тест E1 (одна конфигурация, 10 прогонов)")
    v = df["p95_ms"].to_numpy()
    idx = np.random.default_rng(SEED).permutation(len(v))
    half = len(v) // 2
    a, b = v[idx[:half]], v[idx[half:]]
    p = st.mann_whitney(a, b)
    d = st.cliffs_delta(b, a)
    all_p = [st.mann_whitney(v[list(c)], np.delete(v, list(c))) for c in itertools.combinations(range(len(v)), half)]
    frac = float(np.mean(np.asarray(all_p) < 0.05))
    mde = st.mde_relative(v, 10, 10, seed=SEED)
    rep.table(pd.DataFrame([{
        "n": len(v), "медиана p95, мс": np.median(v), "IQR": st.iqr(v), "CoV": st.cov(v),
        "группа A (медиана)": np.median(a), "группа B (медиана)": np.median(b), "p (M–W)": p, "δ Клиффа": d,
        "доля разбиений с p<0,05": frac, "MDE при n=10+10": f"{100 * mde:.1f} %",
    }]))
    rep.p("Значимой разницы между случайными половинами быть не должно (p > 0,05); доля из всех "
          f"{len(all_p)} разбиений с p < 0,05 должна быть порядка 5 %. MDE — минимальный относительный "
          "сдвиг p95, который план 10 + 10 прогонов обнаруживает с мощностью 80 % при таком шуме.")
    rep.data["aa_e1"] = {"p": p, "delta": d, "cov": st.cov(v), "mde_rel": mde, "frac_sig_splits": frac}
    fig, ax = plt.subplots(figsize=(4.5, 3.6))
    boxes(ax, [a, b], [0, 1], [C["blue"], C["blue"]], ["группа A", "группа B"])
    ax.set_ylabel("p95 задержки сегмента, мс")
    ax.set_title(f"A/A E1: p = {p:.2f}, δ = {d:+.2f}")
    save(fig, fig_dir / "aa_e1.png")


def analyze_calibration(df: pd.DataFrame, rep: Report, fig_dir: Path) -> None:
    rep.h("Калибровка: внесённая задержка 20 мс")
    a = df[df["inject_delay_ms"] == 0]["p50_ms"]
    b = df[df["inject_delay_ms"] == 20]["p50_ms"]
    diff, lo, hi = st.boot_diff_median(a, b, seed=SEED)
    rep.table(pd.DataFrame([{"n (0 мс)": len(a), "n (20 мс)": len(b), "медиана p50 при 0 мс": a.median(),
                             "медиана p50 при 20 мс": b.median(), "Δ, мс": diff, "95 % ДИ Δ": fmt_ci(lo, hi),
                             "p (M–W)": st.mann_whitney(a, b)}]))
    rep.p("Стенд считается откалиброванным, если 95 % ДИ разности медиан p50 содержит 20 мс "
          "(эффект известен заранее).")
    rep.data["calibration"] = {"diff_ms": diff, "ci": [lo, hi]}
    fig, ax = plt.subplots(figsize=(4.5, 3.6))
    boxes(ax, [a.to_numpy(), b.to_numpy()], [0, 1], [C["blue"], C["orange"]], ["0 мс", "+20 мс"])
    ax.set_ylabel("p50 задержки сегмента, мс")
    ax.set_title(f"Калибровка: Δ = {diff:.1f} мс")
    save(fig, fig_dir / "calibration.png")


def analyze_pilot(df: pd.DataFrame, rep: Report, fig_dir: Path) -> None:
    rep.h("Пилот: выбор уровней нагрузки")
    rep.table(df.sort_values(["arch", "users"])[["arch", "users", "p50_ms", "p95_ms", "rps", "error_rate",
                                                 "sut_cpu_cores", "gen_cpu_max_frac"]])
    fig, axes = plt.subplots(1, 2, figsize=(9, 3.6))
    for arch in ("mono", "micro"):
        g = df[df["arch"] == arch].sort_values("users")
        axes[0].plot(g["users"], g["p95_ms"], "-o", color=ARCH_COLOR[arch], label=ARCH_LABEL[arch], linewidth=2)
        axes[1].plot(g["users"], g["rps"], "-o", color=ARCH_COLOR[arch], label=ARCH_LABEL[arch], linewidth=2)
    axes[0].set_yscale("log")
    axes[0].set_ylabel("p95 задержки сегмента, мс (лог.)")
    axes[1].set_ylabel("запросов в секунду")
    for ax in axes:
        ax.set_xlabel("число зрителей")
        ax.legend()
    axes[0].set_title("Пилот: задержка")
    axes[1].set_title("Пилот: пропускная способность")
    save(fig, fig_dir / "pilot_e1.png")


def generator_check(df: pd.DataFrame, col: str, rep: Report, what: str) -> None:
    worst = df[col].max()
    rep.p(f"**Генератор не узкое место ({what}):** максимальная загрузка CPU генератора за прогон, доля лимита — "
          f"медиана {df[col].median():.2f}, максимум {worst:.2f} (порог 0,80). "
          + ("Порог не превышен ни в одном прогоне." if worst < 0.8 else
             f"Порог превышен в {int((df[col] >= 0.8).sum())} прогонах — результаты этих прогонов под сомнением."))


# ===================================================================== E2-подобные (плееры)

def paired_table(df: pd.DataFrame, metric: str, algos: list[str]) -> pd.DataFrame:
    wide = df.pivot_table(index="trace", columns="abr", values=metric, aggfunc="mean")[algos].dropna()
    rows, pvals = [], []
    for x, y in itertools.combinations(algos, 2):
        p = st.wilcoxon(wide[x], wide[y])
        med, lo, hi = st.boot_paired_median(wide[y] - wide[x], seed=SEED)
        d = st.cliffs_delta(wide[y], wide[x])
        pvals.append(p)
        rows.append({"пара": f"{ABR_LABEL[y]} − {ABR_LABEL[x]}", "медиана парной Δ": med, "95 % ДИ": fmt_ci(lo, hi, "{:.3f}"),
                     "p (Уилкоксон)": p, "δ Клиффа": d, "величина": st.delta_magnitude(d)})
    for r, pa in zip(rows, st.holm(pvals), strict=True):
        r["p (Холм)"] = pa
    return pd.DataFrame(rows)


def analyze_e2(df: pd.DataFrame, rep: Report, fig_dir: Path) -> None:
    rep.h("E2. Алгоритмы адаптации битрейта")
    algos = [a for a in ABR_ORDER if a in set(df["abr"])]
    metrics = [("qoe", "QoE_lin на сегмент"), ("bitrate_kbps", "средний битрейт, кбит/с"),
               ("rebuffer_ratio_pct", "доля ребуферизации, %"), ("switches", "число переключений"),
               ("startup_s", "начальная задержка, с")]
    rep.data["e2"] = {}
    for net in sorted(df["network"].unique()):
        g = df[df["network"] == net]
        rep.p(f"### Профиль сети «{net}» (трасс: {g['trace'].nunique()})")
        rows = []
        for a in algos:
            x = g[g["abr"] == a]
            row = {"алгоритм": ABR_LABEL[a], "n": len(x)}
            for m, title in metrics:
                row[title] = x[m].median()
            lo, hi = st.boot_ci(x["qoe"], seed=SEED)
            row["95 % ДИ медианы QoE"] = fmt_ci(lo, hi, "{:.3f}")
            rows.append(row)
        rep.p("Медианы по сессиям:")
        rep.table(pd.DataFrame(rows))
        wide = g.pivot_table(index="trace", columns="abr", values="qoe")[algos].dropna()
        fr = sps.friedmanchisquare(*[wide[a] for a in algos]) if len(algos) > 2 and len(wide) > 1 else None
        if fr is not None:
            rep.p(f"Тест Фридмана по QoE (блоки — трассы, n = {len(wide)}): χ² = {fr.statistic:.2f}, p = {fr.pvalue:.4g}.")
        for m, title in metrics[:4]:
            rep.p(f"Попарные сравнения: **{title}** (Уилкоксон для связанных выборок, поправка Холма на 6 пар):")
            rep.table(paired_table(g, m, algos))
        rep.data["e2"][net] = {"friedman_p": None if fr is None else float(fr.pvalue),
                               "qoe_paired": paired_table(g, "qoe", algos).to_dict(orient="records"),
                               "medians": rows}

    nets = sorted(df["network"].unique())
    fig, axes = plt.subplots(1, len(nets), figsize=(5 * len(nets), 4), sharey=True, squeeze=False)
    for ax, net in zip(axes[0], nets, strict=True):
        g = df[df["network"] == net]
        wide = g.pivot_table(index="trace", columns="abr", values="qoe")[algos]
        for _, r in wide.iterrows():
            ax.plot(range(len(algos)), r.to_numpy(), color=C["grid"], linewidth=1, zorder=1)
        boxes(ax, [wide[a].dropna().to_numpy() for a in algos], list(range(len(algos))),
              [ABR_COLOR[a] for a in algos], [ABR_LABEL[a] for a in algos])
        ax.set_title(f"сеть «{net}»: QoE по сессиям (линия = трасса)")
    axes[0][0].set_ylabel("QoE_lin на сегмент")
    save(fig, fig_dir / "e2_qoe_box.png")

    fig, axes = plt.subplots(3, len(nets), figsize=(5 * len(nets), 8), squeeze=False)
    for col, net in enumerate(nets):
        g = df[df["network"] == net]
        for row, (m, title) in enumerate(metrics[1:4]):
            ax = axes[row][col]
            boxes(ax, [g[g["abr"] == a][m].to_numpy() for a in algos], list(range(len(algos))),
                  [ABR_COLOR[a] for a in algos], [ABR_LABEL[a] for a in algos])
            ax.set_ylabel(title)
            if row == 0:
                ax.set_title(f"сеть «{net}»")
    save(fig, fig_dir / "e2_components.png")


def timeline_plot(runs: list[tuple[Path, dict]], fig_dir: Path) -> None:
    """Иллюстрация поведения алгоритмов на одной трассе LTE (первый по порядку прогон)."""
    cand = [(r, m) for r, m in runs if m["params"].get("network") == "lte" and m["status"] == "ok"]
    if not cand:
        return
    run, m = sorted(cand, key=lambda x: x[1]["params"]["trace"])[0]
    trace = pd.read_csv(ROOT / m["params"]["trace"])
    files = sorted(run.glob("segments_*.csv"))
    fig, axes = plt.subplots(len(files), 1, figsize=(8, 1.9 * len(files)), sharex=True, squeeze=False)
    for ax, f in zip(axes[:, 0], files, strict=True):
        seg = pd.read_csv(f)
        abr = seg["abr"].iloc[0]
        t_end = (seg["t_request_s"] + seg["download_s"]).max() + 5
        tr = trace[trace["t_s"] <= t_end]
        ax.step(tr["t_s"], tr["mbps"], where="post", color=C["ink2"], linewidth=1, label="полоса трассы")
        ax.step(seg["t_request_s"], seg["bitrate_kbps"] / 1000, where="post", color=ABR_COLOR.get(abr, C["blue"]),
                linewidth=2, label=f"битрейт {ABR_LABEL.get(abr, abr)}")
        for _, s in seg[seg["rebuffer_s"] > 0].iterrows():
            ax.axvspan(s["t_request_s"], s["t_request_s"] + s["download_s"], color=C["orange"], alpha=0.12, linewidth=0)
        ax.set_ylabel("Мбит/с")
        ax.legend(loc="upper right", fontsize=8, ncols=2)
    axes[-1, 0].set_xlabel("время сессии, с (заливка — загрузка с остановкой)")
    fig.suptitle(f"Поведение алгоритмов на трассе {Path(m['params']['trace']).stem}", fontsize=11)
    save(fig, fig_dir / "e2_timeline.png")


def analyze_aa_players(df: pd.DataFrame, rep: Report, fig_dir: Path) -> None:
    rep.h("A/A-тест E2 (BOLA × 2 на одной трассе, 10 прогонов)")
    df = df.sort_values(["order", "player"])
    p0 = df[df["player"].str.startswith("p0")].set_index("order")
    p1 = df[df["player"].str.startswith("p1")].set_index("order")
    common = p0.index.intersection(p1.index)
    rows = []
    for m, title in (("qoe", "QoE_lin"), ("bitrate_kbps", "битрейт, кбит/с"), ("rebuffer_ratio_pct", "ребуферизация, %"),
                     ("switches", "переключения")):
        d = p1.loc[common, m] - p0.loc[common, m]
        rows.append({"метрика": title, "медиана (близнец 1)": p0.loc[common, m].median(),
                     "медиана (близнец 2)": p1.loc[common, m].median(), "медиана разности": d.median(),
                     "p (Уилкоксон)": st.wilcoxon(p0.loc[common, m], p1.loc[common, m]),
                     "разброс между прогонами (IQR близнеца 1)": st.iqr(p0.loc[common, m])})
    rep.table(pd.DataFrame(rows))
    v = p0.loc[common, "qoe"].to_numpy()
    idx = np.random.default_rng(SEED).permutation(len(v))
    a, b = v[idx[: len(v) // 2]], v[idx[len(v) // 2:]]
    twin = (p1.loc[common, "qoe"] - p0.loc[common, "qoe"]).to_numpy()
    mde = st.mde_paired_abs(np.concatenate([twin, -twin]), 10, seed=SEED)
    rep.p(f"Случайное разбиение 10 прогонов на две группы по 5 (QoE): p (M–W) = {st.mann_whitney(a, b):.2f}, "
          f"δ Клиффа = {st.cliffs_delta(b, a):+.2f}. MDE для парного сравнения на 10 трассах "
          f"(шум = разности близнецов): {mde:.3f} QoE на сегмент.")
    rep.data["aa_e2"] = {"mde_qoe_abs": mde, "twin_median_diff": float(np.median(twin)),
                         "split_p": st.mann_whitney(a, b)}
    fig, ax = plt.subplots(figsize=(4.6, 4))
    ax.scatter(p0.loc[common, "qoe"], p1.loc[common, "qoe"], s=30, color=C["orange"], edgecolor=C["surface"])
    lim = [min(v.min(), p1.loc[common, "qoe"].min()) - 0.02, max(v.max(), p1.loc[common, "qoe"].max()) + 0.02]
    ax.plot(lim, lim, color=C["ink2"], linewidth=1, linestyle="--")
    ax.set_xlabel("QoE близнеца 1")
    ax.set_ylabel("QoE близнеца 2")
    ax.set_title("A/A E2: точка = прогон")
    save(fig, fig_dir / "aa_e2.png")


def analyze_e3(df: pd.DataFrame, bg: pd.DataFrame, rep: Report, fig_dir: Path) -> None:
    rep.h("E3. Совместный вопрос: архитектура сервера и QoE зрителей")
    if not bg.empty:
        rep.p("Фоновая нагрузка (Locust, сегменты), p95 по прогонам:")
        rep.table(summarize_groups(bg, ["arch"], "p95_ms"))
    rows, pvals = [], []
    for abr in [a for a in ABR_ORDER if a in set(df["abr"])]:
        g = df[df["abr"] == abr]
        wide = g.pivot_table(index="trace", columns="arch", values="qoe").dropna()
        ttfb = g.pivot_table(index="trace", columns="arch", values="ttfb_median_ms").dropna()
        if not {"mono", "micro"} <= set(wide.columns):
            continue
        p = st.wilcoxon(wide["mono"], wide["micro"])
        med, lo, hi = st.boot_paired_median(wide["micro"] - wide["mono"], seed=SEED)
        d = st.cliffs_delta(wide["micro"], wide["mono"])
        pvals.append(p)
        rows.append({"алгоритм": ABR_LABEL[abr], "n трасс": len(wide), "QoE mono": wide["mono"].median(),
                     "QoE micro": wide["micro"].median(), "медиана парной Δ": med, "95 % ДИ": fmt_ci(lo, hi, "{:.3f}"),
                     "p (Уилкоксон)": p, "δ Клиффа": d, "величина": st.delta_magnitude(d),
                     "TTFB mono, мс": ttfb["mono"].median(), "TTFB micro, мс": ttfb["micro"].median()})
    for r, pa in zip(rows, st.holm(pvals), strict=True):
        r["p (Холм)"] = pa
    res = pd.DataFrame(rows)
    rep.table(res)
    rep.data["e3"] = res.to_dict(orient="records")
    algos = [a for a in ABR_ORDER if a in set(df["abr"])]
    fig, axes = plt.subplots(1, len(algos), figsize=(4.5 * len(algos), 3.8), sharey=True, squeeze=False)
    for ax, abr in zip(axes[0], algos, strict=True):
        wide = df[df["abr"] == abr].pivot_table(index="trace", columns="arch", values="qoe").dropna()
        for _, r in wide.iterrows():
            ax.plot([0, 1], [r["mono"], r["micro"]], color=C["grid"], linewidth=1, zorder=1)
        boxes(ax, [wide["mono"].to_numpy(), wide["micro"].to_numpy()], [0, 1], [C["blue"], C["orange"]],
              [ARCH_LABEL["mono"], ARCH_LABEL["micro"]])
        ax.set_title(f"{ABR_LABEL[abr]}: QoE при фоне 900 зрителей")
    axes[0][0].set_ylabel("QoE_lin на сегмент")
    save(fig, fig_dir / "e3_qoe_paired.png")


# ===================================================================== main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", type=Path, default=ROOT / "results" / "raw")
    ap.add_argument("--out", type=Path, default=ROOT / "results")
    a = ap.parse_args()
    processed, fig_dir = a.out / "processed", a.out / "figures"
    processed.mkdir(parents=True, exist_ok=True)
    rep = Report()
    rep.lines += ["# Сводка результатов", "", "Файл создан скриптом `analysis/analyze.py` из `results/raw`; "
                  "вручную не редактируется."]
    exps = experiments(a.raw)
    if not exps:
        raise SystemExit(f"нет данных в {a.raw}")
    rep.p("Эксперименты: " + ", ".join(f"`{k}` ({len(v)} прогонов)" for k, v in exps.items()))
    failed = {k: [m["run_id"] for _, m in v if m["status"] != "ok"] for k, v in exps.items()}
    if any(failed.values()):
        rep.p("**Прогоны с ошибками:** " + json.dumps({k: v for k, v in failed.items() if v}, ensure_ascii=False))
    stab = {}
    for name, runs in exps.items():
        kind = runs[0][1]["kind"]
        base = name.split("-")[0] if not name.startswith("ci-") else name
        if kind == "load":
            rows = [r for r in (load_run_row(run, m) for run, m in runs) if r]
            df = pd.DataFrame(rows)
            df.to_csv(processed / f"{name}_runs.csv", index=False)
            if name.startswith("pilot"):
                analyze_pilot(df, rep, fig_dir)
                continue
            if name.startswith("calibration"):
                analyze_calibration(df, rep, fig_dir)
            elif name.startswith("aa"):
                analyze_aa_load(df, rep, fig_dir)
            else:
                analyze_e1(df, rep, fig_dir)
                ecdf_plot(runs, fig_dir, f"{base}_ecdf.png", "E1: ECDF задержки сегмента (все прогоны конфигурации)")
            generator_check(df, "gen_cpu_max_frac", rep, name)
            keys = [k for k in ("arch", "users", "inject_delay_ms") if k in df.columns]
            stab[name] = stability(df, keys, "p95_ms", rep, fig_dir, f"{name}_stability.png",
                                   f"{name}: p95 относительно медианы конфигурации по порядку прогонов")
        else:
            rows = [r for run, m in runs for r in player_rows(run, m)]
            df = pd.DataFrame(rows)
            df.to_csv(processed / f"{name}_sessions.csv", index=False)
            ok = df[df.get("error").isna()] if "error" in df else df
            if name.startswith("aa"):
                analyze_aa_players(ok, rep, fig_dir)
            elif kind == "joint":
                bg = pd.DataFrame([r for r in (load_run_row(run, m) for run, m in runs) if r])
                bg.to_csv(processed / f"{name}_background.csv", index=False)
                analyze_e3(ok, bg, rep, fig_dir)
            else:
                analyze_e2(ok, rep, fig_dir)
                timeline_plot(runs, fig_dir)
            gen = ok.groupby("run_id")[["gen_cpu_max_frac", "gen_cpu_share_core", "order"]].max().reset_index()
            generator_check(gen, "gen_cpu_max_frac", rep, name)
            keys = [k for k in ("arch", "abr", "network", "player") if k in ok.columns]
            stab[name] = stability(ok.dropna(subset=["qoe"]).assign(qoe_shift=lambda d: d["qoe"] + 10), keys,
                                   "qoe_shift", rep, fig_dir, f"{name}_stability.png",
                                   f"{name}: QoE (+10) относительно медианы группы по порядку прогонов")
    rep.h("Стабильность во времени (тренд метрики по порядку прогонов)")
    rep.table(pd.DataFrame([{"эксперимент": k, **v} for k, v in stab.items()]))
    rep.p("Нет систематического дрейфа, если |ρ| мал и p > 0,05. Для QoE к значению прибавлено 10, "
          "чтобы нормировка на медиану не делила на число около нуля.")
    rep.data["stability"] = stab
    (processed / "summary.md").write_text("\n".join(rep.lines) + "\n", encoding="utf-8")
    (processed / "summary.json").write_text(json.dumps(rep.data, indent=2, ensure_ascii=False, default=float),
                                            encoding="utf-8")
    print(f"готово: {processed / 'summary.md'}, графики в {fig_dir}")


if __name__ == "__main__":
    main()
