"""Подготовка входных данных стенда (запускается один раз, результат хранится в git).

1. Размеры сегментов видео: берутся из артефакта Pensieve (Mao et al., SIGCOMM 2017),
   репозиторий hongzimao/pensieve, коммит PENSIEVE_COMMIT, каталоги video_server/video1..6.
   Скачиваются только размеры файлов (через GitHub API), а не сами сегменты.
2. Трассы «lte»: открытый набор 4G/LTE (van der Hooft et al., IEEE Communications
   Letters, 2016), архив logs_all.zip. Оставляем только время и пропускную способность,
   координаты GPS отбрасываем, пропускную способность масштабируем LTE_SCALE.
3. Трассы «stable»: синтетические, генерируются с фиксированным сидом.

4. Трассы «hsdpa»: 3G Norway HSDPA, тестовая нарезка Genet (320 с), 30 трасс по сиду.

Итог: data/media/envivio_sizes.csv, data/traces/{hsdpa,lte,stable}/*.csv, data/SHA256SUMS.
Повторный запуск даёт побайтно те же файлы (проверка: make data-verify).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import random
import urllib.request
import zipfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

PENSIEVE_COMMIT = "1120bb173958dc9bc9f2ebff1a8fe688b6f4e93c"
# video6 — самый низкий уровень, video1 — самый высокий (см. Manifest.mpd в артефакте)
PENSIEVE_LADDER = {6: 300, 5: 750, 4: 1200, 3: 1850, 2: 2850, 1: 4300}
SEGMENTS = 48  # 49-й сегмент в артефакте короче, Pensieve использует 48 полных
SEGMENT_SECONDS = 4.0

LTE_URL = "https://users.ugent.be/~jvdrhoof/dataset-4g/logs/logs_all.zip"
LTE_SHA256 = "1e5a6d0d2720341baaf5843a1e9962e5e5299746e71034255e2ecd2135d09732"
LTE_SCALE = 0.1  # средние 14–60 Мбит/с -> 1,4–6 Мбит/с, диапазон лестницы 0,3–4,3
LTE_MIN_SECONDS = 300  # сессия длится ~200 с, берём трассы с запасом

# Трассы «hsdpa»: 3G Norway HSDPA (Riiser et al., 2013) в той нарезке по 320 с, на которой
# тестировали Pensieve и Genet; берём тестовую выборку Genet (Xia et al., SIGCOMM 2022).
GENET_COMMIT = "5d4303bf7d817136889120eab87f05e22066add3"
HSDPA_DIR = "abr_trace/testing_trace/norway-test"
HSDPA_COUNT = 30
HSDPA_SEED = 20261118

STABLE_SEED = 20261118
STABLE_COUNT = 20
STABLE_SECONDS = 400


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "streaming-research-stand"})
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 (фиксированные адреса)
        return r.read()


def build_sizes() -> None:
    rows = []
    for folder, kbps in sorted(PENSIEVE_LADDER.items(), key=lambda x: x[1]):
        url = (
            "https://api.github.com/repos/hongzimao/pensieve/contents/"
            f"video_server/video{folder}?ref={PENSIEVE_COMMIT}"
        )
        listing = json.loads(fetch(url))
        sizes = {int(i["name"].split(".")[0]): i["size"] for i in listing if i["name"][0].isdigit()}
        level = sorted(PENSIEVE_LADDER.values()).index(kbps)
        for n in range(1, SEGMENTS + 1):
            rows.append((level, kbps, n, sizes[n]))
    out = DATA / "media" / "envivio_sizes.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["level", "bitrate_kbps", "segment", "bytes"])
        w.writerows(rows)


def write_trace(path: Path, series: list[tuple[float, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["t_s", "mbps"])
        for t, bw in series:
            w.writerow([f"{t:.3f}", f"{bw:.4f}"])


def build_lte(cache: Path | None) -> None:
    raw = cache.read_bytes() if cache else fetch(LTE_URL)
    digest = hashlib.sha256(raw).hexdigest()
    if digest != LTE_SHA256:
        raise SystemExit(f"logs_all.zip: SHA-256 {digest} не совпадает с {LTE_SHA256}")
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        for name in sorted(z.namelist()):
            if not name.endswith(".log"):
                continue
            t, series = 0.0, []
            for line in io.TextIOWrapper(z.open(name), encoding="utf-8"):
                p = line.split()
                if len(p) < 6 or int(p[5]) <= 0:
                    continue
                nbytes, ms = int(p[4]), int(p[5])
                series.append((t, nbytes * 8 / ms / 1000 * LTE_SCALE))
                t += ms / 1000
            if t < LTE_MIN_SECONDS:
                continue
            stem = Path(name).stem.replace("report_", "")
            write_trace(DATA / "traces" / "lte" / f"{stem}.csv", series)


def build_hsdpa() -> None:
    url = f"https://api.github.com/repos/GenetProject/Genet/contents/{HSDPA_DIR}?ref={GENET_COMMIT}"
    names = sorted(i["name"] for i in json.loads(fetch(url)) if i["type"] == "file")
    chosen = sorted(random.Random(HSDPA_SEED).sample(names, HSDPA_COUNT))
    out_dir = DATA / "traces" / "hsdpa"
    sources = []
    for k, name in enumerate(chosen):
        raw = fetch(f"https://raw.githubusercontent.com/GenetProject/Genet/{GENET_COMMIT}/{HSDPA_DIR}/{name}")
        series = []
        for line in raw.decode("utf-8").splitlines():
            p = line.split()
            if len(p) >= 2:
                series.append((float(p[0]), float(p[1])))
        stem = f"hsdpa_{k:03d}"
        write_trace(out_dir / f"{stem}.csv", series)
        sources.append((stem, name, hashlib.sha256(raw).hexdigest()))
    with (DATA / "traces" / "hsdpa_sources.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["trace", "genet_file", "sha256_original"])
        w.writerows(sources)


def build_stable() -> None:
    rng = np.random.default_rng(STABLE_SEED)
    for i in range(STABLE_COUNT):
        mean = rng.uniform(3.0, 6.0)  # «стабильная сеть с высокой пропускной способностью»
        noise = rng.normal(0.0, 0.05, STABLE_SECONDS)  # 5 % посекундный шум
        series = [(float(t), float(max(0.1, mean * (1 + e)))) for t, e in enumerate(noise)]
        write_trace(DATA / "traces" / "stable" / f"stable_{i:04d}.csv", series)


def write_sums() -> None:
    files = sorted(p for p in DATA.rglob("*") if p.is_file() and p.name != "SHA256SUMS")
    lines = [f"{sha256(p)}  {p.relative_to(DATA).as_posix()}" for p in files]
    (DATA / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")


def verify() -> int:
    bad = 0
    for line in (DATA / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, rel = line.split("  ", 1)
        p = DATA / rel
        if not p.exists() or sha256(p) != digest:
            print(f"FAIL {rel}")
            bad += 1
    print("data: OK" if not bad else f"data: {bad} файлов не совпадает")
    return 1 if bad else 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true", help="только проверить SHA256SUMS")
    ap.add_argument("--lte-zip", type=Path, help="локальная копия logs_all.zip")
    a = ap.parse_args()
    if a.verify:
        raise SystemExit(verify())
    build_sizes()
    build_lte(a.lte_zip)
    build_hsdpa()
    build_stable()
    write_sums()
    print("data: построено")


if __name__ == "__main__":
    main()
