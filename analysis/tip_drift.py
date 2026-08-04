#!/usr/bin/env python3
"""Сводка дрейфа tip'а релейера по неделям из data/relayer_tips.jsonl (пишет spec-вотчер,
см. bot/spec.py _log_push_tips).

Зачем: полоса SPEC_TIP_GWEI=0.02 калибрована по ряду 04.08 (p10=0.05 p50=1.5 p90=9.9 —
выше оператора, ниже релейера в 95.5% пушей). Если релейер поднимет свой tip (например,
увидев наши победы в общем блоке), его ряд поплывёт — и здесь это видно за недели до
проигранной tip-войны. Ответ на подъём — пересчёт полосы под новый ряд, не гонка вверх.

Запуск: python3 analysis/tip_drift.py [путь.jsonl]
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = sys.argv[1] if len(sys.argv) > 1 else os.path.join(REPO, "data", "relayer_tips.jsonl")


def q(a: list, p: float):
    return a[min(len(a) - 1, int(p * len(a)))]


def main() -> None:
    seen: set = set()
    weeks: dict = defaultdict(list)
    senders: dict = defaultdict(int)
    for line in open(PATH):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        # дедуп по tx: детект в вотчере может задвоить строку при гонке тредов — сырьё не чистим
        if r.get("tx") in seen or r.get("st") != 1:
            continue
        seen.add(r["tx"])
        wk = datetime.fromtimestamp(r["ts"], tz=timezone.utc).strftime("%G-W%V")
        weeks[wk].append(r["tip_gwei"])
        senders[r["from"]] += 1

    print(f"{PATH}: {len(seen)} пушей, {len(weeks)} недель")
    prev = None
    for wk in sorted(weeks):
        tips = sorted(weeks[wk])
        p50 = q(tips, .5)
        drift = f"  Δp50={p50 - prev:+.3f}" if prev is not None else ""
        print(f"{wk}: n={len(tips):4d}  p10={q(tips, .1):7.3f} p50={p50:7.3f} "
              f"p90={q(tips, .9):7.3f} max={tips[-1]:8.3f}{drift}")
        prev = p50
    print("отправители:", dict(sorted(senders.items(), key=lambda kv: -kv[1])))


if __name__ == "__main__":
    main()
