"""Сравнение выводов двух независимых запусков эксперимента (повторяемость) или двух машин.

    python analysis/compare_runs.py results/processed/summary.json results/repeat/processed/summary.json \
        --labels "запуск 1" "запуск 2" --out results/processed/repeatability.md
    # другая машина: сопоставить эксперименты с разными именами
    python analysis/compare_runs.py A.json B.json --map e1-architecture=local-e1 --map e2-abr=local-e2

Сравниваются не отдельные числа, а выводы: направление и величина эффектов (δ Клиффа),
значимость после поправки Холма, относительная разность медиан.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def load(p: str) -> dict:
    return json.loads(Path(p).read_text(encoding="utf-8"))


def fmt(v, f="{:.3g}") -> str:
    if v is None:
        return "—"
    if isinstance(v, float | int):
        return f.format(v)
    return str(v)


def e1_rows(d: dict, name: str) -> dict:
    return {int(r["users"]): r for r in d.get(f"{name}_tests", [])}


def e2_rows(d: dict, name: str) -> dict:
    out = {}
    for net, block in d.get(name, {}).items():
        for r in block.get("medians", []):
            out[(net, r["алгоритм"])] = r
        out[(net, "_friedman")] = block.get("friedman_p")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--labels", nargs=2, default=["A", "B"])
    ap.add_argument("--map", action="append", default=[], help="имя_в_A=имя_в_B")
    ap.add_argument("--out", required=True)
    a_args = ap.parse_args()
    A, B = load(a_args.a), load(a_args.b)
    la, lb = a_args.labels
    mp = dict(x.split("=", 1) for x in a_args.map)
    e1a, e1b = "e1-architecture", mp.get("e1-architecture", "e1-architecture")
    e2a, e2b = "e2-abr", mp.get("e2-abr", "e2-abr")
    lines = [f"# Сравнение запусков: {la} и {lb}", "",
             "Файл создан `analysis/compare_runs.py`. Совпадение выводов: одинаковое направление эффекта "
             "(знак δ Клиффа) и одинаковое решение о значимости после поправки Холма.", ""]

    ra, rb = e1_rows(A, e1a), e1_rows(B, e1b)
    if ra and rb:
        lines += ["## E1: p95 задержки сегмента, микросервисы относительно монолита", "",
                  f"| Уровень нагрузки ({la} / {lb}) | Δ, % ({la}) | Δ, % ({lb}) | δ ({la}) | δ ({lb}) | p Холма ({la}) | p Холма ({lb}) | Вывод совпал |",
                  "|---|---|---|---|---|---|---|---|"]
        for (ua, x), (ub, y) in zip(sorted(ra.items()), sorted(rb.items()), strict=False):
            same = (x["δ Клиффа"] > 0) == (y["δ Клиффа"] > 0) and (x["p (Холм)"] < 0.05) == (y["p (Холм)"] < 0.05)
            lines.append(f"| {ua} / {ub} | {fmt(x['Δ, %'], '{:.0f}')} | {fmt(y['Δ, %'], '{:.0f}')} | "
                         f"{fmt(x['δ Клиффа'], '{:+.2f}')} | {fmt(y['δ Клиффа'], '{:+.2f}')} | "
                         f"{fmt(x['p (Холм)'], '{:.2g}')} | {fmt(y['p (Холм)'], '{:.2g}')} | {'да' if same else 'нет'} |")
        lines.append("")

    qa, qb = e2_rows(A, e2a), e2_rows(B, e2b)
    if qa and qb:
        lines += ["## E2: медианный QoE по алгоритмам", "",
                  f"| Сеть | Алгоритм | QoE ({la}) | QoE ({lb}) | Ребуф., % ({la}) | Ребуф., % ({lb}) | Порядок по QoE совпал |",
                  "|---|---|---|---|---|---|---|"]
        nets = sorted({k[0] for k in qa} & {k[0] for k in qb})
        for net in nets:
            algos = [k[1] for k in qa if k[0] == net and not k[1].startswith("_")]
            rank_a = sorted(algos, key=lambda x: -qa[(net, x)]["QoE_lin на сегмент"])
            rank_b = sorted([x for x in algos if (net, x) in qb], key=lambda x: -qb[(net, x)]["QoE_lin на сегмент"])
            for alg in algos:
                if (net, alg) not in qb:
                    continue
                x, y = qa[(net, alg)], qb[(net, alg)]
                lines.append(f"| {net} | {alg} | {fmt(x['QoE_lin на сегмент'])} | {fmt(y['QoE_lin на сегмент'])} | "
                             f"{fmt(x['доля ребуферизации, %'])} | {fmt(y['доля ребуферизации, %'])} | "
                             f"{'да' if rank_a == rank_b else 'нет'} |")
            lines.append(f"| {net} | p Фридмана | {fmt(qa.get((net, '_friedman')), '{:.2g}')} | "
                         f"{fmt(qb.get((net, '_friedman')), '{:.2g}')} | | | |")
        lines.append("")

    for key, title in (("aa_e1", "A/A E1"), ("aa_e2", "A/A E2"), ("calibration", "Калибровка E1")):
        if key in A and key in B:
            lines += [f"## {title}", "", f"| Показатель | {la} | {lb} |", "|---|---|---|"]
            for k in A[key]:
                lines.append(f"| {k} | {fmt(A[key][k])} | {fmt(B[key].get(k))} |")
            lines.append("")
    if "e3" in A and "e3" in B:
        lines += ["## E3: парная разность QoE (микросервисы − монолит)", "",
                  f"| Алгоритм | Δ ({la}) | Δ ({lb}) | δ ({la}) | δ ({lb}) | p Холма ({la}) | p Холма ({lb}) |",
                  "|---|---|---|---|---|---|---|"]
        for x, y in zip(A["e3"], B["e3"], strict=False):
            lines.append(f"| {x['алгоритм']} | {fmt(x['медиана парной Δ'])} | {fmt(y['медиана парной Δ'])} | "
                         f"{fmt(x['δ Клиффа'], '{:+.2f}')} | {fmt(y['δ Клиффа'], '{:+.2f}')} | "
                         f"{fmt(x['p (Холм)'], '{:.2g}')} | {fmt(y['p (Холм)'], '{:.2g}')} |")
        lines.append("")
    Path(a_args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a_args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
