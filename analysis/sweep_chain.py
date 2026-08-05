#!/usr/bin/env python3
"""Свип HyperEVM за N суток по сигнатурам ликвидационных событий ВСЕХ семейств протоколов
(address=None => вся цепь). Вопрос: есть ли на цепи лендинги, где поток ликвидаций НЕ взят
доминатором из белого списка RedStone-адаптеров.

Позитивный контроль: якорный блок HyperLend обязан вернуть известную ликвидацию ЧЕРЕЗ ТОТ ЖЕ
фильтр (topic0-массив, address=None) — иначе весь пустой результат по чужим протоколам
неотличим от молча обрезанного getLogs.

Покрытие ЧЕСТНО ограничено сигнатурами ниже; протокол с кастомным событием в свип не попадёт
(перечислено в отчёте). Сигнатуры, взятые по памяти (не из нашего кода), помечены mem=True —
урок 05.08: сигнатура по памяти уже дала один ложный вывод.
"""
import json, sys, time
from analysis.rpc import Rpc, get_logs_chunked
from analysis.keccak import event_topic0
from analysis import protocols as P

DAYS = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
OUT = sys.argv[2] if len(sys.argv) > 2 else "/home/claude-agent/.claude/jobs/d9c2c3f6/tmp/sweep30d.json"
CONTROL_TX = "0x6e9cce69ec33a142df79d40f0340b385d41b1d4a77a63822d27ab5a2498c0cf6"
CONTROL_BLOCK = 42266950

SIGS = {
    # canon: из нашего кода (protocols.py) — самоконтроль ниже сверяет с P.TOPIC_LIQUIDATION_CALL
    "aave_v3": (P.SIG_LIQUIDATION_CALL, False),
    "morpho_blue": ("Liquidate(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)", True),
    "euler_v2": ("Liquidate(address,address,address,uint256,uint256)", True),
    "compound_v3_coll": ("AbsorbCollateral(address,address,address,uint256,uint256)", True),
    "compound_v3_debt": ("AbsorbDebt(address,address,uint256,uint256)", True),
    "liquity_v1_trove": ("TroveLiquidated(address,uint256,uint256,uint8)", True),
    "liquity_v1_liq": ("Liquidation(uint256,uint256,uint256,uint256)", True),
}
topic_by_name = {k: event_topic0(sig) for k, (sig, _) in SIGS.items()}
assert topic_by_name["aave_v3"] == P.TOPIC_LIQUIDATION_CALL, "keccak расходится с protocols.py"
name_by_topic = {v.lower(): k for k, v in topic_by_name.items()}

r = Rpc()
head = int(r.call("eth_blockNumber", []), 16)
frm = head - int(DAYS * 86400 / 0.9836)
print(f"свип [{frm}, {head}] ~{DAYS:g} сут, {len(SIGS)} сигнатур одним topic0-массивом", flush=True)

# --- позитивный контроль ТОГО ЖЕ фильтра ---------------------------------------------
anchor = r.get_logs(None, [list(topic_by_name.values())], CONTROL_BLOCK, CONTROL_BLOCK)
assert any(l["transactionHash"].lower() == CONTROL_TX for l in anchor), \
    "контроль провален: address=None + topic0-массив не возвращает якорную ликвидацию"
print("позитивный контроль (address=None, массив топиков) ✓", flush=True)

t0 = time.time()
def _prog(hi, to_block, n):
    if hi % 400_000 < 4000:
        print(f"  …блок {hi}/{to_block}, логов {n}, {time.time()-t0:.0f}с", flush=True)
logs = get_logs_chunked(r, None, [list(topic_by_name.values())], frm, head, chunk=4000,
                        on_progress=_prog)
print(f"логов: {len(logs)} за {time.time()-t0:.0f}с", flush=True)

rows = []
for lg in logs:
    rows.append({
        "family": name_by_topic.get(lg["topics"][0].lower(), "?"),
        "contract": lg["address"].lower(),
        "block": int(lg["blockNumber"], 16),
        "tx": lg["transactionHash"],
    })
assert any(x["tx"].lower() == CONTROL_TX for x in rows), "якорь не в полном наборе — чанкер теряет окна"
json.dump(rows, open(OUT, "w"))
print(f"-> {OUT}", flush=True)

import collections
c = collections.Counter((x["family"], x["contract"]) for x in rows)
print("\nконтракты с ликвидациями за окно:")
for (fam, addr), n in c.most_common(30):
    mem = "  [сигнатура по памяти]" if SIGS[fam][1] else ""
    print(f"  {fam:18} {addr} {n:5} шт{mem}")
