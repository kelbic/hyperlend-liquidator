"""Preflight китовых выходов: где рвётся роут ВЫХОДА из залога, до каскада, а не в каскаде.

Зачем. Модель бонуса считает прибыль по цене оракула, а забирает её DEX. Урок Base-флота:
обрыв роута вскрывается на РАЗМЕРЕ, а не на модели — preflight weETH показал отсутствие роута
уже на $207k при живом «бонусе» в модели. У hyperlend вся крупная книга — плечевой стейкинг
(долг WHYPE против kHYPE/wstHYPE/beHYPE/PT-kHYPE), и главный приз книги, PT-kHYPE-SEP26
($333k бонуса), сидит на самой тяжёлой ноге: ликвидность Pendle тоньше LST, и путь
PT -> kHYPE -> стейбл против прямого PT-пула заранее неизвестен.

Что меряет. По каждой ноге лестницу размеров: сколько реально отдаёт квотер, какой у него
price impact и на каком размере роут ИСЧЕЗАЕТ. Выход — таблица + JSON для дальнейшего
разбора; сравнение «эффективная цена выхода vs цена оракула» и есть настоящая маржа.

Только чтение: HTTP-квоты и eth_call цен. Ни подписи, ни отправки, кошелёк не трогается.
"""
from __future__ import annotations

import json
import sys
import time

sys.path.insert(0, "/home/claude-agent/hyperlend-liquidator")

from analysis.protocols import ORACLE, TOKENS                     # noqa: E402
from analysis.rpc import Rpc                                      # noqa: E402
from bot import liqd                                              # noqa: E402

ORACLE_BASE_UNIT = 1e8
SEL_GET_ASSET_PRICE = "0xb3596f07"

# Ноги: (залог, во что выходим). Долг китов — WHYPE, поэтому WHYPE-нога боевая; стейбл-нога
# показывает, во что упрётся реализация прибыли.
LEGS = [
    ("kHYPE", "WHYPE"), ("kHYPE", "USDT0"),
    ("PT-kHYPE-24SEP2026", "WHYPE"), ("PT-kHYPE-24SEP2026", "USDT0"),
    ("PT-kHYPE-19MAR2026", "WHYPE"),
    ("wstHYPE", "WHYPE"),
    ("beHYPE", "WHYPE"),
]
# Лестница в долларах: ищем не «работает ли», а ГДЕ рвётся.
LADDER_USD = [50_000, 100_000, 200_000, 300_000, 500_000]


def asset_price_usd(rpc: Rpc, addr: str) -> float:
    raw = rpc.eth_call(ORACLE, SEL_GET_ASSET_PRICE + "00" * 12 + addr[2:].lower())
    return int(raw, 16) / ORACLE_BASE_UNIT


def run(ladder=LADDER_USD, legs=LEGS, out_path=None):
    rpc = Rpc()
    prices = {}
    for sym in {s for leg in legs for s in leg}:
        prices[sym] = asset_price_usd(rpc, TOKENS[sym]["address"])
        print(f"  цена {sym:<20} ${prices[sym]:,.4f}")
    print()

    rows = []
    for coll, out_sym in legs:
        c, o = TOKENS[coll], TOKENS[out_sym]
        p_in, p_out = prices[coll], prices[out_sym]
        print(f"=== {coll} -> {out_sym} ===")
        for usd in ladder:
            amount_in = int(usd / p_in * 10 ** c["decimals"])
            rec = {"coll": coll, "out": out_sym, "usd": usd, "amount_in": amount_in}
            t0 = time.time()
            try:
                q = liqd.quote(c["address"], o["address"], amount_in,
                               c["decimals"], o["decimals"], wall_sec=60.0)
                got_usd = q["amount_out"] / 10 ** o["decimals"] * p_out
                # ПОТЕРЯ ПРОТИВ ОРАКУЛА — то, что модель бонуса не видит
                loss = 1.0 - got_usd / usd
                rec.update(ok=True, out_usd=got_usd, loss=loss,
                           impact=q.get("price_impact"), sec=time.time() - t0)
                print(f"  ${usd:>7,} -> ${got_usd:>10,.0f}  потеря {loss * 100:6.2f}%  "
                      f"impact {(q.get('price_impact') or 0) * 100:5.2f}%  {time.time() - t0:.1f}с")
            except Exception as e:                                  # noqa: BLE001
                rec.update(ok=False, error=f"{type(e).__name__}: {e}"[:160],
                           sec=time.time() - t0)
                print(f"  ${usd:>7,} -> ОБРЫВ: {type(e).__name__}: {str(e)[:90]}")
            rows.append(rec)
        print()

    if out_path:
        json.dump({"ts": int(time.time()), "prices": prices, "rows": rows},
                  open(out_path, "w"), indent=1)
        print(f"→ {out_path}")
    return rows


if __name__ == "__main__":
    run(out_path=(sys.argv[1] if len(sys.argv) > 1 else None))
