"""Оркестрация эксперимента: план прогонов, запуск стенда, сбор сырых данных и манифестов.

    python runner/run_experiment.py configs/e1-architecture.yaml
    python runner/run_experiment.py configs/e2-abr.yaml --dry-run      # только показать план

Виды экспериментов (поле kind):
  load  — Locust-«зрители» против SUT (E1, A/A для E1);
  abr   — эмулированные плееры с разными ABR на одной трассе (E2, A/A для E2);
  joint — плееры измеряют QoE, пока Locust создаёт фоновую нагрузку (E3).

Каждый прогон: results/raw/<эксперимент>/<NNN>_<метка>/ — сырые файлы + manifest.json.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose", "-f", str(ROOT / "docker-compose.yml")]
ARCH_SERVICES = {
    "mono": ["redis", "app", "edge-mono"],
    "micro": ["redis", "auth", "catalog", "delivery", "telemetry", "edge-micro"],
}
DEFAULT_RESOURCES = {  # значения по умолчанию из docker-compose.yml
    "EDGE_CPUS": "0.25", "APP_CPUS": "1.75", "APP_WORKERS": "2", "AUTH_CPUS": "0.5",
    "CATALOG_CPUS": "0.15", "DELIVERY_CPUS": "0.75", "TELEMETRY_CPUS": "0.35",
    "REDIS_CPUS": "0.5", "LOAD_CPUS": "1.0", "PLAYERS_CPUS": "0.5",
}


def utc() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def sh(args: list[str], env: dict | None = None, check: bool = True, timeout: float | None = None,
       log: Path | None = None) -> subprocess.CompletedProcess:
    full_env = {**os.environ, **(env or {})}
    p = subprocess.run(args, env=full_env, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    if log is not None:
        with log.open("a", encoding="utf-8") as f:
            f.write(f"$ {' '.join(args)}\n{p.stdout}{p.stderr}\n[exit {p.returncode}]\n")
    if check and p.returncode != 0:
        raise RuntimeError(f"команда упала ({p.returncode}): {' '.join(args)}\n{p.stdout[-2000:]}\n{p.stderr[-2000:]}")
    return p


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- сведения о машине и коде

def host_info() -> dict:
    info = {"platform": platform.platform(), "python": platform.python_version(),
            "machine": platform.machine(), "logical_cpus": os.cpu_count(), "cpu_model": platform.processor()}
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.exists():
        for line in cpuinfo.read_text(errors="replace").splitlines():
            if line.startswith("model name"):
                info["cpu_model"] = line.split(":", 1)[1].strip()
                break
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        info["mem_total_kb"] = int(meminfo.read_text().split()[1])
    try:
        d = json.loads(sh(["docker", "info", "--format", "{{json .}}"]).stdout)
        info["docker"] = {k: d.get(k) for k in ("ServerVersion", "OperatingSystem", "KernelVersion",
                                                 "NCPU", "MemTotal", "CgroupVersion", "Architecture")}
        info["docker"]["compose"] = sh(COMPOSE[:2] + ["version", "--short"]).stdout.strip()
    except Exception as e:  # noqa: BLE001
        info["docker_error"] = repr(e)
    ci = {k: os.environ[k] for k in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA", "RUNNER_NAME",
                                     "RUNNER_OS", "ImageOS", "ImageVersion") if k in os.environ}
    if ci:
        info["ci"] = ci
    return info


def git_info() -> dict:
    def g(*a: str) -> str:
        p = sh(["git", "-C", str(ROOT), *a], check=False)
        return p.stdout.strip() if p.returncode == 0 else ""
    return {"commit": g("rev-parse", "HEAD"), "describe": g("describe", "--tags", "--always", "--dirty"),
            "dirty": bool(g("status", "--porcelain", "--untracked-files=no"))}


def applied_limits() -> dict:
    """Лимиты, реально применённые Docker к работающим контейнерам стенда (а не заявленные)."""
    ids = sh(["docker", "ps", "-q", "--filter", "label=com.docker.compose.project=stand"], check=False).stdout.split()
    if not ids:
        return {}
    out = {}
    p = sh(["docker", "inspect", "--format",
            "{{.Name}} {{.HostConfig.NanoCpus}} {{.HostConfig.Memory}}", *ids], check=False)
    for line in p.stdout.splitlines():
        name, nano, mem = line.split()
        out[name.lstrip("/")] = {"cpus": int(nano) / 1e9, "memory_mb": int(mem) // 2**20}
    return out


def images_info(services: list[str]) -> dict:
    """Какие образы реально использованы: тег из compose + ID и RepoDigest локального образа."""
    cfg = json.loads(sh(COMPOSE + ["--profile", "mono", "--profile", "micro", "--profile", "monitoring",
                                   "--profile", "tools", "config", "--format", "json"]).stdout)
    out = {}
    for name in services:
        ref = cfg["services"].get(name, {}).get("image")
        if not ref:
            continue
        p = sh(["docker", "image", "inspect", "--format", "{{json .}}", ref], check=False)
        if p.returncode == 0:
            d = json.loads(p.stdout)
            out[name] = {"ref": ref, "id": d.get("Id"), "repo_digests": d.get("RepoDigests"),
                         "created": d.get("Created")}
        else:
            out[name] = {"ref": ref, "id": None}
    return out


# ---------------------------------------------------------------- фоновый сбор docker stats

class StatsSampler(threading.Thread):
    """Раз в ~1–2 с снимает `docker stats` по всем контейнерам (CPU в % одного ядра)."""

    def __init__(self, out: Path):
        super().__init__(daemon=True)
        self.out = out
        self.stop_event = threading.Event()
        self.rows: list[str] = []

    def run(self) -> None:
        while not self.stop_event.is_set():
            t = time.time()
            p = sh(["docker", "stats", "--no-stream", "--format", "{{json .}}"], check=False, timeout=30)
            for line in p.stdout.splitlines():
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self.rows.append(f"{t:.3f},{d['Name']},{d['CPUPerc'].rstrip('%')},{d['MemUsage'].split('/')[0].strip()}")
            self.stop_event.wait(0.5)

    def finish(self) -> None:
        self.stop_event.set()
        self.join(timeout=40)
        self.out.write_text("t_unix,container,cpu_percent,mem_usage\n" + "\n".join(self.rows) + "\n", encoding="utf-8")


def export_prometheus(start: float, end: float, out: Path) -> None:
    queries = {
        "container_cpu_cores": 'sum by (name) (rate(container_cpu_usage_seconds_total{name=~"stand-.+"}[10s]))',
        "container_mem_bytes": 'max by (name) (container_memory_working_set_bytes{name=~"stand-.+"})',
        "server_rps": 'sum by (instance, handler) (rate(http_requests_total[10s]))',
        "server_p50_s": 'histogram_quantile(0.5, sum by (instance, le) (rate(http_request_duration_highr_seconds_bucket[30s])))',
        "server_p95_s": 'histogram_quantile(0.95, sum by (instance, le) (rate(http_request_duration_highr_seconds_bucket[30s])))',
        "auth_check_p50_s": 'histogram_quantile(0.5, sum by (instance, le) (rate(stand_auth_check_seconds_bucket[30s])))',
        "auth_check_p95_s": 'histogram_quantile(0.95, sum by (instance, le) (rate(stand_auth_check_seconds_bucket[30s])))',
    }
    result = {"start": start, "end": end}
    for key, q in queries.items():
        url = "http://127.0.0.1:9090/api/v1/query_range?" + urllib.parse.urlencode(
            {"query": q, "start": f"{start:.0f}", "end": f"{end:.0f}", "step": "5"})
        try:
            with urllib.request.urlopen(url, timeout=20) as r:  # noqa: S310 (локальный Prometheus)
                result[key] = json.loads(r.read())["data"]["result"]
        except Exception as e:  # noqa: BLE001
            result[key] = {"error": repr(e)}
    out.write_text(json.dumps(result), encoding="utf-8")


# ---------------------------------------------------------------- план эксперимента

def select_traces(cfg: dict, network: str, n: int, seed: int) -> list[Path]:
    folder = ROOT / cfg["traces"][network]
    files = sorted(folder.glob("*.csv"))
    if cfg.get("trace_mode") == "fixed":
        return [files[cfg.get("trace_index", 0)]] * n
    rng = random.Random(f"{seed}:{network}")
    if n > len(files):
        raise SystemExit(f"в {folder} только {len(files)} трасс, нужно {n}")
    return rng.sample(files, n)


def build_plan(cfg: dict) -> list[dict]:
    seed = cfg["seed"]
    factors = cfg.get("factors", {})
    names = list(factors)
    combos = [dict(zip(names, c, strict=True)) for c in itertools.product(*factors.values())] or [{}]
    plan: list[dict] = []
    if cfg["kind"] == "load":
        for combo in combos:
            for rep in range(cfg["repetitions"]):
                plan.append({**combo, "rep": rep})
    else:
        reps = cfg["repetitions"]
        for combo in combos:
            network = combo.get("network", cfg.get("fixed", {}).get("network"))
            for rep, trace in enumerate(select_traces(cfg, network, reps, seed)):
                plan.append({**combo, "network": network, "trace": trace.relative_to(ROOT).as_posix(), "rep": rep})
    if cfg.get("randomize_order", True):
        random.Random(seed).shuffle(plan)
    for i, item in enumerate(plan):
        item["order"] = i
        item["run_seed"] = seed * 1000 + i
    return plan


def label(item: dict) -> str:
    parts = [f"{k}-{v}" for k, v in item.items() if k in ("arch", "users", "network")]
    if "trace" in item:
        parts.append(Path(item["trace"]).stem)
    parts.append(f"r{item['rep']}")
    return "_".join(str(p) for p in parts)


# ---------------------------------------------------------------- выполнение прогона

def compose_env(cfg: dict, item: dict) -> dict:
    env = {**DEFAULT_RESOURCES, **{k: str(v) for k, v in cfg.get("resources", {}).items()}}
    env["INJECT_DELAY_MS"] = str(item.get("inject_delay_ms", cfg.get("fixed", {}).get("inject_delay_ms", 0)))
    return env


def start_sut(arch: str, env: dict, log: Path, recreate: bool = True) -> None:
    other = "micro" if arch == "mono" else "mono"
    sh(COMPOSE + ["--profile", other, "rm", "-sf"] + ARCH_SERVICES[other][1:], env=env, log=log)
    sh(COMPOSE + ["--profile", arch, "up", "-d", "--wait", "--wait-timeout", "120"]
       + (["--force-recreate"] if recreate else []) + ARCH_SERVICES[arch], env=env, log=log, timeout=300)


def locust_cmd(cfg: dict, users: int, seconds: float, rel: str, seed: int, name: str | None = None,
               warmup: float = 0.0) -> list[str]:
    load = cfg.get("load", {})
    spawn = load.get("spawn_rate", max(1, int(users / load.get("pacing_seconds", 4.0))))
    cmd = COMPOSE + ["--profile", "tools", "run", "--rm"]
    if name:
        cmd += ["--name", name]
    for k, v in {"WARMUP": warmup, "PACING": load.get("pacing_seconds", 4.0), "LEVEL": load.get("level", 1),
                 "SEED": seed, "RAW_OUT": f"/results/{rel}/requests.csv.gz"}.items():
        cmd += ["-e", f"{k}={v}"]
    cmd += ["load", "-f", "/mnt/locust/locustfile.py", "--headless", "-u", str(users), "-r", str(spawn),
            "-t", f"{int(seconds)}s", "--host", "http://edge:8080", "--csv", f"/results/{rel}/locust",
            "--only-summary", "--stop-timeout", "5", "--loglevel", "WARNING"]
    return cmd


def players_cmd(cfg: dict, item: dict, rel: str) -> list[str]:
    p = cfg.get("players", {})
    cmd = COMPOSE + ["--profile", "tools", "run", "--rm", "players", "--base-url", "http://edge:8080",
                     "--abr", ",".join(cfg["abr"]), "--trace", f"/{item['trace']}", "--out", f"/results/{rel}",
                     "--buffer-max", str(p.get("buffer_max_s", 30)), "--rtt-ms", str(p.get("rtt_ms", 80)),
                     "--seed", str(item["run_seed"])]
    if p.get("max_segments"):
        cmd += ["--max-segments", str(p["max_segments"])]
    return cmd


def run_one(cfg: dict, item: dict, exp_dir: Path, common: dict, config_path: Path) -> dict:
    run_id = f"{item['order']:03d}_{label(item)}_{uuid.uuid4().hex[:6]}"
    run_dir = exp_dir / run_id
    run_dir.mkdir(parents=True)
    run_dir.chmod(0o777)  # контейнеры пишут сюда от своих UID
    rel = run_dir.relative_to(ROOT / "results").as_posix()
    log = run_dir / "run.log"
    arch = item.get("arch", cfg.get("fixed", {}).get("arch", "mono"))
    env = compose_env(cfg, item)
    manifest = {
        "run_id": run_id, "experiment": cfg["name"], "kind": cfg["kind"],
        "config_file": config_path.relative_to(ROOT).as_posix(), "config_sha256": sha256_file(config_path),
        "params": {k: v for k, v in item.items() if k not in ("rep", "order", "run_seed")} | {"arch": arch},
        "repetition": item["rep"], "order": item["order"],
        "seed": {"experiment": cfg["seed"], "run": item["run_seed"]},
        "timing": {"warmup_s": cfg.get("warmup_seconds", 0), "duration_s": cfg.get("duration_seconds")},
        "resources": env, "fixed": cfg.get("fixed", {}), "load": cfg.get("load", {}), "players": cfg.get("players", {}),
        "abr": cfg.get("abr"), **common, "started_at": utc(),
    }
    if "trace" in item:
        manifest["trace_sha256"] = sha256_file(ROOT / item["trace"])

    # Пересоздаём SUT и Redis перед каждым прогоном: нет переноса кэшей, сессий и утечек между прогонами
    start_sut(arch, env, log, recreate=cfg.get("recreate_sut", True))
    time.sleep(cfg.get("settle_seconds", 3))
    manifest["applied_limits"] = applied_limits()
    sampler = StatsSampler(run_dir / "docker_stats.csv")
    sampler.start()
    t_start = time.time()
    status = "ok"
    try:
        if cfg["kind"] == "load":
            total = cfg["warmup_seconds"] + cfg["duration_seconds"]
            manifest["measure_window"] = {"from_s": cfg["warmup_seconds"], "to_s": total}
            sh(locust_cmd(cfg, item["users"], total, rel, item["run_seed"], warmup=cfg["warmup_seconds"]),
               env=env, log=log, timeout=total + 180)
        elif cfg["kind"] == "abr":
            sh(players_cmd(cfg, item, rel), env=env, log=log, timeout=1800)
        elif cfg["kind"] == "joint":
            bg = cfg["background"]
            name = f"stand-bg-{run_id[-6:]}"
            warm = cfg.get("warmup_seconds", 20)
            bg_cmd = locust_cmd(cfg | {"load": bg}, bg["users"], warm + 900, rel, item["run_seed"], name=name,
                                warmup=warm)
            bg_proc = subprocess.Popen(bg_cmd, env={**os.environ, **env}, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
            time.sleep(warm)  # фон выходит на установившийся режим
            try:
                sh(players_cmd(cfg, item, rel), env=env, log=log, timeout=1800)
            finally:
                sh(["docker", "stop", "-t", "15", name], check=False, log=log)
                bg_proc.wait(timeout=60)
        else:
            raise SystemExit(f"неизвестный kind {cfg['kind']!r}")
    except Exception as e:  # noqa: BLE001
        status = f"error: {e}"[:2000]
    finally:
        t_end = time.time()
        sampler.finish()
    if cfg.get("monitoring"):
        export_prometheus(t_start, t_end, run_dir / "prometheus.json")
    manifest.update({"finished_at": utc(), "wall_s": round(t_end - t_start, 3), "status": status,
                     "images": images_info(ARCH_SERVICES[arch] + (["load"] if cfg["kind"] != "abr" else [])
                                           + (["players"] if cfg["kind"] != "load" else []))})
    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("config", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", type=Path, default=ROOT / "results" / "raw")
    a = ap.parse_args()
    config_path = a.config.resolve()
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    plan = build_plan(cfg)
    print(f"{cfg['name']}: {len(plan)} прогонов", flush=True)
    if a.dry_run:
        for item in plan:
            print(" ", item["order"], label(item))
        return
    if shutil.which("docker") is None:
        raise SystemExit("нужен Docker с Compose v2")

    exp_dir = a.out / cfg["name"]
    if exp_dir.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        exp_dir.rename(exp_dir.with_name(f"{cfg['name']}.prev-{stamp}"))
    exp_dir.mkdir(parents=True)
    exp_dir.chmod(0o777)
    common = {"git": git_info(), "host": host_info(), "data_sha256": {
        "media": sha256_file(ROOT / "data" / "media" / "envivio_sizes.csv"),
        "sums": sha256_file(ROOT / "data" / "SHA256SUMS")}}
    (exp_dir / "experiment.json").write_text(json.dumps(
        {"config": cfg, "plan": plan, **common, "started_at": utc()}, indent=2, ensure_ascii=False), encoding="utf-8")

    env0 = {**DEFAULT_RESOURCES, **{k: str(v) for k, v in cfg.get("resources", {}).items()}}
    sh(COMPOSE + ["--profile", "mono", "--profile", "micro", "--profile", "tools", "build"], env=env0, timeout=1200)
    if cfg.get("monitoring"):
        sh(COMPOSE + ["--profile", "monitoring", "up", "-d", "prometheus", "cadvisor"], env=env0, timeout=300)

    failures = 0
    t0 = time.time()
    for item in plan:
        m = run_one(cfg, item, exp_dir, common, config_path)
        failures += m["status"] != "ok"
        done = item["order"] + 1
        eta = (time.time() - t0) / done * (len(plan) - done)
        print(f"[{done}/{len(plan)}] {m['run_id']} {m['status'][:80]} ({m['wall_s']:.0f} с, осталось ~{eta / 60:.0f} мин)",
              flush=True)
    sh(COMPOSE + ["--profile", "mono", "--profile", "micro", "rm", "-sf"], env=env0, check=False)
    print(f"готово: {len(plan) - failures} успешно, {failures} с ошибками; данные в {exp_dir}", flush=True)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
