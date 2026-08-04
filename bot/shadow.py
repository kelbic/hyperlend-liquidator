"""Shadow-race телеметрия (04.08, ярус 1): каждая ЧУЖАЯ ликвидация пула — запись в
data/shadow_races.jsonl с полной анатомией блока.

Зачем: это единственный датасет, по которому решаются два открытых вопроса гонки —
  1) tip-политика: какие чаевые реально платит поле и где в блоке сидит победитель
     (гипотеза «сэндвича»: оракул-push тоже tx с tip; tip ВЫШЕ пуша = исполнение ДО
     обновления цены = реверт — реверты 10.10 платили 13-15k gwei и проигрывали);
  2) вилка predict: проигрываем ли мы гонки на подтверждении (тогда огонь по прогнозу)
     или хватает реакции (тогда огонь по подтверждению, ноль ревертов).

Дизайн под горячий цикл: инлайн-часть — один дешёвый getLogs раз в SHADOW_EVERY_SEC и
сдвиг чекпойнта; вся тяжесть (чеки, блок, цены) — в daemon-потоке, как alert_async.
Ошибка обогащения пишет сырое событие с пометкой err — молча не теряем ничего.

Адресат сигналов — АГЕНТ (инбокс ~/.fleet-watch), не человек: чужое взятие бот не
отработает, но и человек по нему не действует — это вход триажа (доктрина 03.08).
Откат: HL_SHADOW=0."""
from __future__ import annotations

import json
import os
import sys
import threading
import time

from bot import config as C
from analysis.protocols import (POOL, ORACLE, TOKENS, ADDR_TO_SYMBOL, DECIMALS,
                                TOPIC_LIQUIDATION_CALL)

SEL_GET_ASSET_PRICE = "0xb3596f07"          # getAssetPrice(address) -> uint256 (base 1e8)
_state = {"last_tick": 0.0}
_price_cache: dict = {}                     # asset -> (price_usd, monotonic_ts)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load_ckpt() -> int:
    try:
        return int(json.load(open(C.SHADOW_CKPT))["last_block"])
    except Exception:
        return 0


def _save_ckpt(block: int) -> None:
    tmp = C.SHADOW_CKPT + ".tmp"
    json.dump({"last_block": block}, open(tmp, "w"))
    os.replace(tmp, C.SHADOW_CKPT)


def _append(rec: dict) -> None:
    with open(C.SHADOW_FILE, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _asset_price_usd(rpc, asset: str) -> float | None:
    """Цена из НАШЕГО же оракула (1e8): то, по чему протокол реально считает HF."""
    a = asset.lower()
    hit = _price_cache.get(a)
    if hit and time.monotonic() - hit[1] < 60:
        return hit[0]
    try:
        r = rpc.eth_call(ORACLE, SEL_GET_ASSET_PRICE + a[2:].rjust(64, "0"))
        px = int(r, 16) / 1e8
        _price_cache[a] = (px, time.monotonic())
        return px
    except Exception:
        return None


def _notify_inbox(text: str, key: str) -> None:
    """Инбокс агента; недоступность нотификатора — лог, не падение (телеметрия важнее)."""
    try:
        fw = os.path.expanduser("~/.fleet-watch")
        if fw not in sys.path:
            sys.path.insert(0, fw)
        from notify import notify
        notify(text, source="hyperlend-shadow", hil=False, key=key, dedup_sec=6 * 3600)
    except Exception as e:                   # noqa: BLE001
        print(f"  shadow: inbox недоступен ({e}); событие только в jsonl")


def _decode(lg: dict) -> dict:
    d = lg["data"][2:]
    return {"block": int(lg["blockNumber"], 16), "tx": lg["transactionHash"],
            "coll": "0x" + lg["topics"][1][-40:], "debt": "0x" + lg["topics"][2][-40:],
            "victim": "0x" + lg["topics"][3][-40:],
            "cover": int(d[0:64], 16), "seized": int(d[64:128], 16),
            "liquidator": "0x" + d[128:192][-40:]}


def _enrich(rpc, events: list[dict], our_view: dict, st_snapshot: dict) -> None:
    """Тяжёлая часть в daemon-потоке: чеки всех tx блока, цены, гипотеза push."""
    for e in events:
        try:
            blk = rpc.get_block(e["block"], True)
            base = int(blk.get("baseFeePerGas", "0x0"), 16)
            txs = blk.get("transactions", [])[:12]
            anatomy, win = [], None
            for t in txs:
                rc = rpc.call("eth_getTransactionReceipt", [t["hash"]])
                row = {"idx": int(t["transactionIndex"], 16),
                       "to": (t.get("to") or "")[:14], "from": t["from"][:14],
                       "gas": int(rc["gasUsed"], 16), "st": int(rc["status"], 16),
                       "tip_gwei": round((int(rc["effectiveGasPrice"], 16) - base) / 1e9, 2),
                       "inb": len(t.get("input", "0x")) // 2 - 1}
                anatomy.append(row)
                if t["hash"] == e["tx"]:
                    win = row
            push_idx = [r["idx"] for r in anatomy
                        if r["st"] == 1 and r["inb"] >= 600 and 100_000 <= r["gas"] <= 600_000
                        and r["to"].lower() != POOL[:14].lower()]
            dpx = _asset_price_usd(rpc, e["debt"])
            dec = DECIMALS.get(e["debt"].lower())
            debt_usd = (e["cover"] / 10 ** dec * dpx) if (dpx and dec is not None) else None
            sym_c = ADDR_TO_SYMBOL.get(e["coll"].lower(), e["coll"][:10])
            bonus_bps = TOKENS.get(sym_c, {}).get("bonus_bps", 11000)
            bonus_usd = (debt_usd * max(0, bonus_bps - 10000) / 1e4 * 0.9) if debt_usd else None
            bal = st_snapshot.get("balance_hype")
            rec = {"iso": _now_iso(), **e,
                   "coll_sym": sym_c, "debt_sym": ADDR_TO_SYMBOL.get(e["debt"].lower(), e["debt"][:10]),
                   "debt_usd": round(debt_usd, 2) if debt_usd else None,
                   "bonus_usd_est": round(bonus_usd, 2) if bonus_usd else None,
                   "base_gwei": round(base / 1e9, 3),
                   "win": win, "push_idx": push_idx, "block_txs": anatomy,
                   "ours": {"in_book": our_view.get("in_book", {}).get(e["victim"].lower()),
                            "in_hot": our_view.get("in_hot", {}).get(e["victim"].lower()),
                            "floor_ok": (bal is not None
                                         and bal * 1e18 >= C.GAS_LIMIT * 2 * base) or None,
                            "guard": st_snapshot.get("guard")}}
            _append(rec)
            if bonus_usd and bonus_usd >= C.MIN_PROFIT_USD:
                _notify_inbox(
                    f"🏁 чужая ликвидация выше нашего пола: {rec['debt_sym']}->{sym_c} "
                    f"${debt_usd:,.0f} (бонус ~${bonus_usd:,.0f}) блок {e['block']} "
                    f"tip {win['tip_gwei'] if win else '?'} gwei, победитель {e['liquidator'][:10]}; "
                    f"жертва in_hot={rec['ours']['in_hot']} — разобрать по shadow_races.jsonl",
                    key=f"shadow:{e['tx'][:18]}")
        except Exception as ex:              # noqa: BLE001 — телеметрия не смеет ронять поток
            _append({"iso": _now_iso(), "err": str(ex)[:200], **e})


def tick(rpc, book: dict, hs: dict, st: dict) -> None:
    """Инлайн-вход из горячего цикла: троттлится сам, тяжесть уводит в поток."""
    if not C.SHADOW:
        return
    now = time.monotonic()
    if now - _state["last_tick"] < C.SHADOW_EVERY_SEC:
        return
    _state["last_tick"] = now
    try:
        head = rpc.block_number()
        frm = _load_ckpt() + 1
        if frm <= 1:
            frm = head - 300                 # первый запуск: ~5 минут истории, не архив
        if frm > head:
            return
        frm = max(frm, head - 5000)          # после простоя не тащить дни: окно ограничено
        logs = rpc.get_logs(POOL, [TOPIC_LIQUIDATION_CALL], frm, head)
        _save_ckpt(head)
    except Exception as e:                   # noqa: BLE001
        print(f"  shadow: скан не прошёл ({e}); чекпойнт не сдвинут")
        return
    ours = (C.CONTRACT or "").lower()
    events = [ev for ev in (_decode(lg) for lg in logs)
              if ev["liquidator"].lower() != ours]
    if not events:
        return
    victims = {e["victim"].lower() for e in events}
    hot = {a.lower() for a in hs.get("hot", [])}
    borrowers = {a.lower() for a in book.get("borrowers", [])}
    our_view = {"in_book": {v: v in borrowers for v in victims},
                "in_hot": {v: v in hot for v in victims}}
    snap = {"balance_hype": st.get("balance_hype"),
            "guard": "ok" if st.get("consec_reverts", 0) < C.MAX_CONSEC_REVERTS else "tripped"}
    threading.Thread(target=_enrich, args=(rpc, events, our_view, snap), daemon=True).start()
    print(f"  shadow: {len(events)} чужих ликвидаций в [{frm},{head}] -> обогащение в фоне")
