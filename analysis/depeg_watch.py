"""Депег-вотч LST-ряда HyperEVM: сколько сжатия отделяет китов от ликвидации.

ЗАЧЕМ ИМЕННО ЭТО. Крупная книга hyperlend — плечевой стейкинг: залог kHYPE/wstHYPE/beHYPE/PT
против долга WHYPE. У такой позиции HF не зависит от цены HYPE вовсе (обе ноги двигаются
вместе) — она зависит ТОЛЬКО от ОТНОШЕНИЯ цены залога к цене WHYPE. Отношение LST/HYPE в
норме монотонно растёт (капают стейкинг-награды), поэтому его СЖАТИЕ — единственный триггер,
способный уронить китов, и единственный, который стоит караулить заранее.

Арифметика запаса точная и простая. Для позиции «LST против WHYPE» HF линеен по отношению:
если отношение сожмётся в f раз, то HF' = HF × f. Значит ликвидация наступает при
    сжатие_до_падения = 1 − 1/HF
(HF 1.0640 -> 6.0%; сходится с наблюдением «киты падают на 5-6%»).

Что делает вотч: раз в час пишет снимок отношений и запас каждого кита, сравнивает текущее
отношение с максимумом скользящего окна (для растущего ряда максимум и есть база), и когда
просадка от базы съедает заметную долю запаса ближайшего кита — будит АГЕНТА (не человека:
он тут ничего не подписывает — канон [[alerts-only-where-human-acts]]).

Только чтение: оракул + пул. Ни подписи, ни отправки.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.aave import SEL_GET_ASSET_PRICE, SEL_GET_USER_ACCOUNT_DATA, decode_user_account_data
from analysis.multicall import multicall
from analysis.protocols import ADDR_TO_SYMBOL, ORACLE, ORACLE_BASE_UNIT, POOL, TOKENS
from analysis.rpc import Rpc

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
HIST_PATH = os.path.join(DATA_DIR, "depeg_history.jsonl")

# Ряд, чьё отношение к WHYPE решает судьбу плечевой книги.
LST = ["kHYPE", "wstHYPE", "beHYPE", "PT-kHYPE-24SEP2026", "PT-kHYPE-19MAR2026"]
BASE = "WHYPE"

WINDOW_H = float(os.environ.get("HL_DEPEG_WINDOW_H", "168"))     # окно базы, часов (7 суток)
DRAWDOWN_ALERT = float(os.environ.get("HL_DEPEG_DRAWDOWN", "0.015"))   # 1.5% от базы
HEADROOM_FRAC = float(os.environ.get("HL_DEPEG_HEADROOM_FRAC", "0.30"))  # доля съеденного запаса
MIN_DEBT_USD = float(os.environ.get("HL_DEPEG_MIN_DEBT", "100000"))    # кто считается китом


def read_ratios(rpc: Rpc) -> dict:
    """{символ: отношение цены к WHYPE} по ценам оракула, одним multicall."""
    syms = [BASE] + LST
    addrs = [TOKENS[s]["address"] for s in syms]
    res = multicall(rpc, [(ORACLE, SEL_GET_ASSET_PRICE + a[2:].rjust(64, "0")) for a in addrs])
    px = {}
    for s, (ok, ret) in zip(syms, res):
        if ok and len(ret) >= 66:
            px[s] = int(ret[2:66], 16) / ORACLE_BASE_UNIT
    base = px.get(BASE)
    if not base:
        raise RuntimeError("нет цены WHYPE — база отношения недоступна")
    return {"prices": px, "ratios": {s: px[s] / base for s in LST if s in px}}


def whale_headroom(rpc: Rpc, borrowers: list[str]) -> list[dict]:
    """Для каждого заёмщика: HF и сжатие отношения, после которого он под водой (1 − 1/HF).

    Оценка ВЕРНА для чистой связки «LST-залог против WHYPE-долга» и КОНСЕРВАТИВНА для
    смешанных: доля залога вне LST-ряда двигается вместе с долгом, поэтому реальный запас
    у смешанной позиции больше, а не меньше — мы не проспим падение, максимум разбудим зря.
    """
    if not borrowers:
        return []
    res = multicall(rpc, [(POOL, SEL_GET_USER_ACCOUNT_DATA + b[2:].rjust(64, "0"))
                          for b in borrowers])
    out = []
    for b, (ok, ret) in zip(borrowers, res):
        if not (ok and len(ret) >= 2 + 6 * 64):
            continue
        a = decode_user_account_data(ret)
        debt = a.get("total_debt_base", 0) / ORACLE_BASE_UNIT
        hf = a["health_factor"] / 1e18
        if debt < MIN_DEBT_USD or hf <= 0 or hf > 5:
            continue
        out.append({"borrower": b, "hf": round(hf, 4), "debt_usd": round(debt),
                    "headroom": round(1.0 - 1.0 / hf, 4) if hf > 1 else 0.0})
    out.sort(key=lambda r: r["headroom"])
    return out


def load_history(hours: float) -> list[dict]:
    if not os.path.exists(HIST_PATH):
        return []
    cutoff = time.time() - hours * 3600
    rows = []
    with open(HIST_PATH) as f:
        for ln in f:
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if r.get("ts", 0) >= cutoff:
                rows.append(r)
    return rows


def assess(cur: dict, hist: list[dict]) -> dict:
    """Просадка каждого отношения от максимума окна. Для растущего ряда максимум = база."""
    out = {}
    for s, r in cur["ratios"].items():
        past = [h["ratios"][s] for h in hist if s in (h.get("ratios") or {})]
        peak = max(past + [r])
        out[s] = {"ratio": round(r, 6), "peak": round(peak, 6),
                  "drawdown": round(1.0 - r / peak, 5) if peak else 0.0,
                  "samples": len(past)}
    return out


def notify_agent(text: str, key: str) -> None:
    """Тревога адресована АГЕНТУ: пре-арм и разбор — его работа, человеку тут нечего подписывать."""
    try:
        sys.path.insert(0, os.path.expanduser("~/.fleet-watch"))
        from notify import notify                                    # type: ignore
        notify(text, source="hyperlend-depeg", hil=False, key=key, dedup_sec=3600)
    except Exception as e:                                           # noqa: BLE001
        print(f"  [notify] не доставлено: {type(e).__name__}: {e}")


def main() -> int:
    rpc = Rpc()
    cur = read_ratios(rpc)
    hist = load_history(WINDOW_H)
    dd = assess(cur, hist)

    book_path = os.path.join(DATA_DIR, "hotset.json")
    hot = []
    try:
        hot = sorted({a.lower() for a in json.load(open(book_path))["hot"]})
    except Exception:                                                # noqa: BLE001
        pass
    whales = whale_headroom(rpc, hot)

    row = {"ts": int(time.time()), "prices": {k: round(v, 6) for k, v in cur["prices"].items()},
           "ratios": {k: round(v, 8) for k, v in cur["ratios"].items()},
           "whales": whales[:5]}
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HIST_PATH, "a") as f:
        f.write(json.dumps(row) + "\n")

    print(f"отношения к {BASE} (просадка от максимума окна {WINDOW_H:.0f}ч):")
    for s, d in sorted(dd.items()):
        print(f"  {s:<20} {d['ratio']:.6f}  пик {d['peak']:.6f}  "
              f"просадка {d['drawdown'] * 100:+.3f}%  проб {d['samples']}")
    if whales:
        print(f"\nкиты (долг ≥ ${MIN_DEBT_USD:,.0f}), запас до воды = 1 − 1/HF:")
        for w in whales[:5]:
            print(f"  {w['borrower'][:12]}… HF={w['hf']:.4f} долг ${w['debt_usd']:,} "
                  f"запас {w['headroom'] * 100:.2f}%")
    else:
        print("\nкитов в горячем наборе нет")

    worst = max((d["drawdown"] for d in dd.values()), default=0.0)
    nearest = whales[0]["headroom"] if whales else None
    eaten = (worst / nearest) if (nearest and nearest > 0) else 0.0
    print(f"\nхудшая просадка {worst * 100:.3f}%; "
          f"ближайший запас {nearest * 100:.2f}%; съедено {eaten * 100:.1f}% запаса"
          if nearest else f"\nхудшая просадка {worst * 100:.3f}%")

    if worst >= DRAWDOWN_ALERT or (nearest and eaten >= HEADROOM_FRAC):
        names = ", ".join(f"{s} {d['drawdown'] * 100:+.2f}%" for s, d in dd.items()
                          if d["drawdown"] >= DRAWDOWN_ALERT / 2) or "—"
        notify_agent(
            f"📉 hyperlend: сжатие LST-ряда {worst * 100:.2f}% ({names}). "
            f"Ближайший кит {whales[0]['borrower'][:10]}… HF={whales[0]['hf']:.4f}, "
            f"запас {nearest * 100:.2f}% — съедено {eaten * 100:.0f}%. "
            f"Проверить пре-арм и глубину выхода kHYPE→WHYPE.",
            key="hl-depeg")
        print("→ агент разбужен")
    return 0


if __name__ == "__main__":
    sys.exit(main())
