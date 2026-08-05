#!/usr/bin/env python3
"""Кто забирает поток ликвидаций HyperLend: прямой скан LiquidationCall за N суток.

Классификация победы: АТОМАРНАЯ (в той же tx есть лог адаптера RedStone = push+liquidate,
для нас недостижимо — список апдейтеров вшит в байткод адаптера, см. STATE.md 05.08) vs
РЕАКТИВНАЯ (цену двигал кто-то другой, окно открыто всем). Это единственная метрика,
которая отвечает «есть ли нам что ловить», — размер потока сам по себе не отвечает.

Позитивный контроль обязателен: getLogs на публичных узлах HyperEVM молча отдаёт [] на
слишком широком окне, и пустой результат неотличим от тихого рынка. Скан падает, если
заведомо известная ликвидация не попала в набор.

    python3 -m analysis.scan_liquidations [СУТОК] [ВЫХОД.json]
"""
import json, sys, time
from concurrent.futures import ThreadPoolExecutor
from analysis.rpc import Rpc, get_logs_chunked
from analysis import protocols as P

DAYS = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
ADAPTERS = {"0xe4ae88743c3834d0c492eabc47384c84bcadc6a6",
            "0x24c8964338deb5204b096039147b8e8c3aea42cc"}
CONTROL_TX = "0x6e9cce69ec33a142df79d40f0340b385d41b1d4a77a63822d27ab5a2498c0cf6"
CONTROL_BLOCK = 42266950  # блок этой ликвидации (журнал гонок data/shadow_races.jsonl)
OURS = {"0x0cf80b56c78013d63741b31ba01811cec8ca088c",
        "0x5c20f458a14849673ec1aec407f6ed22f82d07af",
        "0xcbab63aa7f8fa7f15445e85e64b2ade4feec2bd6"}

r = Rpc()
head = int(r.call("eth_blockNumber", []), 16)
frm = head - int(DAYS * 86400 / 0.9836)
print(f"скан [{frm}, {head}] ~{DAYS} сут", flush=True)
logs = get_logs_chunked(r, P.POOL, [P.TOPIC_LIQUIDATION_CALL], frm, head, chunk=4000,
                        on_progress=lambda *a: None)
print(f"LiquidationCall: {len(logs)}", flush=True)

rows = []
for lg in logs:
    d = bytes.fromhex(lg["data"][2:])
    rows.append({
        "block": int(lg["blockNumber"], 16), "tx": lg["transactionHash"],
        "coll": "0x" + lg["topics"][1][-40:], "debt": "0x" + lg["topics"][2][-40:],
        "victim": "0x" + lg["topics"][3][-40:],
        "cover": int.from_bytes(d[0:32], "big"), "seized": int.from_bytes(d[32:64], "big"),
        "liquidator": "0x" + d[64:96].hex()[-40:],
    })

# --- ПОЗИТИВНЫЙ КОНТРОЛЬ -------------------------------------------------------------
# Тихий рынок и молча обрезанный getLogs дают ОДИН И ТОТ ЖЕ пустой ответ, поэтому «сошлось
# 0 с 0» — не контроль, а та же тишина в двух экземплярах. Якорь берётся ВСЕГДА: узкое
# чтение блока с заведомо известной ликвидацией обязано её вернуть — это доказывает, что
# эндпоинт, фильтр и разбор работают ИМЕННО СЕЙЧАС.
anchor = r.get_logs(P.POOL, [P.TOPIC_LIQUIDATION_CALL], CONTROL_BLOCK, CONTROL_BLOCK)
assert any(l["transactionHash"].lower() == CONTROL_TX for l in anchor), \
    f"контроль провален: блок {CONTROL_BLOCK} не отдаёт известную ликвидацию {CONTROL_TX[:12]}… — "
print(f"позитивный контроль: якорный блок {CONTROL_BLOCK} отдал известную ликвидацию ✓", flush=True)
if frm <= CONTROL_BLOCK <= head:
    assert any(x["tx"].lower() == CONTROL_TX for x in rows), \
        "контроль провален: якорь внутри окна, но чанкер его потерял — окна теряются молча"
    print("  ...и она же найдена в полном наборе ✓", flush=True)

def receipt(x):
    for _ in range(3):
        try:
            rc = r.call("eth_getTransactionReceipt", [x["tx"]])
            tx = r.call("eth_getTransactionByHash", [x["tx"]])
            return rc, tx
        except Exception:
            time.sleep(0.3)
    return None, None

def enrich(x):
    rc, tx = receipt(x)
    if rc:
        addrs = {l["address"].lower() for l in rc["logs"]}
        x["atomic"] = bool(addrs & ADAPTERS)
        x["to"] = (tx.get("to") or "").lower()
        x["from"] = tx["from"].lower()
        x["gas"] = int(rc["gasUsed"], 16)
    return x

with ThreadPoolExecutor(max_workers=8) as ex:
    rows = list(ex.map(enrich, rows))

# --- цены/децималы для оценки приза ---
assets = sorted({x["coll"] for x in rows} | {x["debt"] for x in rows})
from analysis.multicall import multicall
from analysis.keccak import selector
px = multicall(r, [(P.ORACLE, selector("getAssetPrice(address)") + a[2:].rjust(64, "0")) for a in assets])
dc = multicall(r, [(a, selector("decimals()")) for a in assets])
price = {a: (int(v[2:66], 16) if ok and len(v) >= 66 else 0) for a, (ok, v) in zip(assets, px)}
dec = {a: (int(v[2:66], 16) if ok and len(v) >= 66 else 18) for a, (ok, v) in zip(assets, dc)}
for x in rows:
    x["seized_usd"] = x["seized"] / 10 ** dec[x["coll"]] * price[x["coll"]] / 1e8
    x["cover_usd"] = x["cover"] / 10 ** dec[x["debt"]] * price[x["debt"]] / 1e8
    x["bonus_usd"] = x["seized_usd"] - x["cover_usd"]
json.dump(rows, open(sys.argv[2] if len(sys.argv) > 2 else "/tmp/liq.json", "w"))
print(f"сохранено {len(rows)}", flush=True)
