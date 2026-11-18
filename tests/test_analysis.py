"""Сквозной тест анализа на синтетических сырых данных в формате раннера."""
from __future__ import annotations

import gzip
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "analysis"))
import stats as st  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RES = {"LOAD_CPUS": "1.0", "PLAYERS_CPUS": "0.5"}


def manifest(exp, kind, order, params, rep):
    return {"run_id": f"{order:03d}_x", "experiment": exp, "kind": kind, "order": order, "params": params,
            "repetition": rep, "status": "ok", "resources": RES, "timing": {"duration_s": 10},
            "measure_window": {"from_s": 0, "to_s": 10}}


def write_load_run(d: Path, exp, order, params, rep, median_ms, rng):
    d.mkdir(parents=True)
    lat = rng.lognormal(np.log(median_ms), 0.4, 400)
    with gzip.open(d / "requests.csv.gz", "wt", encoding="utf-8") as f:
        f.write("t_s,name,response_ms,bytes,failed\n")
        for i, x in enumerate(lat):
            f.write(f"{i * 0.02:.3f},/media/[segment],{x:.3f},375000,0\n")
            f.write(f"{i * 0.02:.3f},/api/v1/events,{x / 5:.3f},20,0\n")
    (d / "docker_stats.csv").write_text("t_unix,container,cpu_percent,mem_usage\n"
                                        "1,stand-load-run-abc,35.0,50MiB\n1,stand-app-1,120.0,90MiB\n", encoding="utf-8")
    (d / "manifest.json").write_text(json.dumps(manifest(exp, "load", order, params, rep)), encoding="utf-8")


def write_abr_run(d: Path, exp, order, params, rep, abrs, rng, kind="abr"):
    d.mkdir(parents=True)
    for i, abr in enumerate(abrs):
        pid = f"p{i}_{abr}"
        base = {"rate": 2, "bola": 3, "robustmpc": 3, "festive": 1}[abr]
        rows = ["player,abr,index,level,bitrate_kbps,bytes,t_request_s,ttfb_s,download_s,throughput_mbps,"
                "buffer_before_s,buffer_after_s,rebuffer_s,wait_s,status,retries"]
        for n in range(12):
            lvl = int(np.clip(base + rng.integers(-1, 2), 0, 5))
            br = [300, 750, 1200, 1850, 2850, 4300][lvl]
            reb = float(rng.exponential(0.2)) if rng.random() < 0.2 else 0.0
            rows.append(f"{pid},{abr},{n + 1},{lvl},{br},{br * 500},{n * 4},0.01,1.0,{br / 1000},5,9,{reb:.3f},0,200,0")
        (d / f"segments_{pid}.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
        (d / f"session_{pid}.json").write_text(json.dumps(
            {"player": pid, "abr": abr, "trace": "t", "trace_mean_mbps": 2.0, "segments": 12, "startup_s": 0.5,
             "rebuffer_s": 0.1, "session_s": 50, "http_errors": 0}), encoding="utf-8")
    (d / "players_meta.json").write_text(json.dumps({"cpu_share_of_core": 0.05}), encoding="utf-8")
    (d / "docker_stats.csv").write_text("t_unix,container,cpu_percent,mem_usage\n1,stand-players-run-x,5.0,40MiB\n",
                                        encoding="utf-8")
    m = manifest(exp, kind, order, params, rep)
    (d / "manifest.json").write_text(json.dumps(m), encoding="utf-8")


def test_stats_basics():
    assert st.cliffs_delta([2, 3], [0, 1]) == 1.0
    assert st.delta_magnitude(0.5) == "большой"
    assert st.holm([0.01, 0.04, 0.03]) == [0.03, 0.06, 0.06]
    lo, hi = st.boot_ci(np.arange(20.0))
    assert lo < 9.5 < hi


def test_full_analysis(tmp_path):
    rng = np.random.default_rng(1)
    raw = tmp_path / "raw"
    order = 0
    for arch, users, med in [("mono", 100, 20), ("micro", 100, 26), ("mono", 300, 40), ("micro", 300, 70)]:
        for rep in range(5):
            write_load_run(raw / "e1-architecture" / f"{order:03d}", "e1-architecture", order,
                           {"arch": arch, "users": users}, rep, med, rng)
            order += 1
    for rep in range(6):
        write_load_run(raw / "aa-e1" / f"{rep:03d}", "aa-e1", rep, {"arch": "mono", "users": 300}, rep, 40, rng)
    for i, (delay, med) in enumerate([(0, 20), (20, 40)] * 3):
        write_load_run(raw / "calibration-e1" / f"{i:03d}", "calibration-e1", i,
                       {"arch": "mono", "users": 100, "inject_delay_ms": delay}, i, med, rng)
    order = 0
    for net in ("stable", "hsdpa"):
        for rep in range(5):
            write_abr_run(raw / "e2-abr" / f"{order:03d}", "e2-abr", order,
                          {"arch": "mono", "network": net, "trace": f"data/traces/{net}/t{rep}.csv"}, rep,
                          ["rate", "bola", "robustmpc", "festive"], rng)
            order += 1
    for rep in range(6):
        write_abr_run(raw / "aa-e2" / f"{rep:03d}", "aa-e2", rep,
                      {"arch": "mono", "network": "hsdpa", "trace": "data/traces/hsdpa/t0.csv"}, rep, ["bola", "bola"], rng)
    order = 0
    for arch in ("mono", "micro"):
        for rep in range(5):
            d = raw / "e3-joint" / f"{order:03d}"
            write_abr_run(d, "e3-joint", order, {"arch": arch, "network": "hsdpa",
                                                 "trace": f"data/traces/hsdpa/t{rep}.csv"}, rep, ["rate", "bola"], rng,
                          kind="joint")
            with gzip.open(d / "requests.csv.gz", "wt", encoding="utf-8") as f:
                f.write("t_s,name,response_ms,bytes,failed\n0,/media/[segment],30,1,0\n1,/media/[segment],50,1,0\n")
            order += 1
    out = tmp_path / "out"
    p = subprocess.run([sys.executable, str(ROOT / "analysis" / "analyze.py"), "--raw", str(raw), "--out", str(out)],
                       capture_output=True, text=True, encoding="utf-8", env={"PYTHONUTF8": "1", **dict(__import__("os").environ)})
    assert p.returncode == 0, p.stderr[-3000:]
    summary = (out / "processed" / "summary.md").read_text(encoding="utf-8")
    for section in ("E1.", "A/A-тест E1", "Калибровка", "E2.", "A/A-тест E2", "E3.", "Стабильность"):
        assert section in summary
    for fig in ("e1-architecture_p95_box.png", "e1-architecture_ecdf.png", "aa_e1.png", "calibration.png",
                "e2-abr_qoe_box.png", "e2-abr_components.png", "aa_e2.png", "e3_qoe_paired.png"):
        assert (out / "figures" / fig).exists(), fig
    data = json.loads((out / "processed" / "summary.json").read_text(encoding="utf-8"))
    # медиана micro заметно больше mono -> δ Клиффа положительная
    assert all(r["δ Клиффа"] > 0 for r in data["e1-architecture_tests"])
