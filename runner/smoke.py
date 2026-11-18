"""Smoke-тест стенда: оба варианта SUT поднимаются, отвечают по контракту, метрики собираются.

    python runner/smoke.py            # поднимает mono, проверяет, затем micro, проверяет
    python runner/smoke.py --arch mono
Код возврата 0 — все проверки пройдены.
"""
from __future__ import annotations

import argparse
import sys
import time

import httpx
from run_experiment import COMPOSE, resources, sh, start_sut  # noqa: E402

BASE = "http://127.0.0.1:8080"


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"  [{'OK' if cond else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}", flush=True)
    return cond


def smoke_arch(arch: str, env: dict) -> bool:
    print(f"== {arch}", flush=True)
    start_sut(arch, env, log=None)  # type: ignore[arg-type]
    ok = True
    with httpx.Client(base_url=BASE, timeout=10) as c:
        r = c.get("/healthz")
        ok &= check("healthz через шлюз", r.status_code == 200, r.text[:80])
        r = c.post("/api/v1/sessions", json={"user": "smoke"})
        ok &= check("создание сессии", r.status_code == 201)
        token, manifest = r.json()["token"], r.json()["manifest"]
        r = c.get(manifest)
        ok &= check("манифест MPD", r.status_code == 200 and "<MPD" in r.text and r.text.count("<Representation") == 6)
        r = c.get(f"/media/envivio/5/1.m4s?token={token}")
        ok &= check("сегмент уровня 5 (4300 кбит/с)", r.status_code == 200 and len(r.content) > 1_000_000,
                    f"{len(r.content)} байт")
        r2 = c.get(f"/media/envivio/5/1.m4s?token={token}")
        ok &= check("сегмент детерминирован", r2.content == r.content)
        ok &= check("без токена — 401", c.get("/media/envivio/0/1.m4s").status_code == 401)
        ok &= check("чужой токен — 401", c.get("/media/envivio/0/1.m4s?token=abc.def").status_code == 401)
        ok &= check("телеметрия", c.post("/api/v1/events", json={"segment": 1}).status_code == 202)
        ok &= check("внутренний API закрыт снаружи", c.get(f"/internal/verify?token={token}").status_code == 404)
    svc = "app" if arch == "mono" else "delivery"
    out = sh(COMPOSE + ["exec", "-T", svc, "python", "-c",
                        "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/metrics').read().decode())"],
             env=env, check=False).stdout
    ok &= check(f"метрики Prometheus в {svc}", "http_requests_total" in out)
    return ok


def prometheus_ok() -> bool:
    try:
        for _ in range(20):
            r = httpx.get("http://127.0.0.1:9090/api/v1/targets", timeout=5).json()
            up = {t["labels"]["instance"]: t["health"] for t in r["data"]["activeTargets"]}
            if up.get("cadvisor:8080") == "up":
                break
            time.sleep(2)
        return check("Prometheus собирает cAdvisor", up.get("cadvisor:8080") == "up", str(up))
    except Exception as e:  # noqa: BLE001
        return check("Prometheus доступен", False, repr(e))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arch", choices=["mono", "micro", "both"], default="both")
    ap.add_argument("--no-monitoring", action="store_true")
    a = ap.parse_args()
    env = resources({})  # значения по умолчанию, переопределяемые переменными окружения
    sh(COMPOSE + ["--profile", "mono", "--profile", "micro", "--profile", "tools", "build"], env=env, timeout=1200)
    ok = True
    if not a.no_monitoring:
        sh(COMPOSE + ["--profile", "monitoring", "up", "-d", "prometheus", "cadvisor"], env=env, timeout=300)
    for arch in (["mono", "micro"] if a.arch == "both" else [a.arch]):
        ok &= smoke_arch(arch, env)
    if not a.no_monitoring:
        ok &= prometheus_ok()
    print("SMOKE: " + ("пройден" if ok else "ПРОВАЛЕН"), flush=True)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
