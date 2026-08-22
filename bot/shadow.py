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
# Оракул-апдейты, которые победитель может везти В СВОЕЙ tx (вскрытие 07.08, блок 42516651):
# USDHL прайсится Pyth-адаптером, Pyth = пермишенлесс pull => победитель кладёт свежую цену
# первым логом собственной ликвидации и пересекает HF<1 атомарно — отдельного пуш-tx нет,
# push_idx пуст, реакция по подтверждённому состоянию не выигрывает такие гонки в принципе.
TOPIC_PYTH_PRICE_UPDATE = "0xd06a6b7f4918494b3719217d1802786c1f5112a6c1d88fe2cfec00b4584f6aec"
_TOPIC_ORACLE_UPDATES = {TOPIC_PYTH_PRICE_UPDATE, C.TOPIC_VALUE_UPDATE}
ANATOMY_MAX_TX = 12                         # потолок разбора блока; превышение помечается trunc
_state = {"last_tick": 0.0}
_price_cache: dict = {}                     # asset -> (price_usd, monotonic_ts)


def _above_fire_floor(debt_usd: float | None, bonus_usd: float | None) -> bool:
    """Пол «деньги прошли мимо» = наш собственный пол огня ЦЕЛИКОМ (канон dd19a4e):
    и размер (MIN_DEBT_USD — ниже него позиция сознательно не в hot-set), и профит
    (MIN_PROFIT_USD). None (цену добыть не удалось) агента не будит — запись в jsonl
    остаётся безусловной, триаж дособерёт.

    Названный предел: debt_usd здесь = ПОГАШЕННЫЙ cover, не полный долг позиции. Для
    одиночного резерва cover ~= долг (50%-закрытия начинаются с резервного долга >=$2000,
    т.е. cover >=$1000 > фильтра); утечка узкая — мультирезервный заёмщик с полным долгом
    >=$500, у которого погашенная нога попала в ~[$278,$500), будет подавлен. Дешёвого
    чтения полного долга ДО события у обогащения нет (архив)."""
    return bool(debt_usd and bonus_usd
                and debt_usd >= C.MIN_DEBT_USD and bonus_usd >= C.MIN_PROFIT_USD)


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


def _push_indices(anatomy: list[dict]) -> list[int]:
    """Индексы tx блока, которые РЕАЛЬНО двинули оракул — по факту события в квитанции
    (upd>0), а не по силуэту tx.

    Прежняя эвристика (`st==1 and inb>=600 and 100k<=gas<=600k and to!=POOL`) на блоке
    43827262 промахнулась в обе стороны: настоящий пуш RedStone (idx 1, adapter
    0xe4ae8874, релейер 0x2327c3cd) имел inb=581 и НЕ прошёл порог 600, а помечен был
    посторонний idx 4 с inb=1540 и upd=0. Счётчик upd — тот же самый честный признак,
    которым 07.08 опознан атомарный self-push; здесь он просто применён и к соседям.

    Названный предел: видны пуши тех оракулов, чьи топики в _TOPIC_ORACLE_UPDATES
    (RedStone ValueUpdate + Pyth PriceFeedUpdate). Оба — это ровно те оракулы, которыми
    HyperLend прайсит свои резервы (getSourceOfAsset, замер 07.08); пуш третьего оракула
    остался бы невидимым, и это надо будет закрывать топиком, а не силуэтом."""
    return [r["idx"] for r in anatomy if r.get("upd")]


def _bonus_usd(debt_usd: float | None, seized_usd: float | None,
               coll_sym: str) -> tuple[float | None, str]:
    """Валовая премия победителя. ЗАМЕР сильнее модели: (seized - cover) в долларах — это
    то, что ликвидатор реально получил сверх погашенного, со всеми поправками уже внутри
    (close factor, liquidationProtocolFee, eMode-бонус, пыльные добивания). Модель
    `debt*(bonus_bps-1)*0.9` остаётся ФОЛБЭКОМ на случай, когда цену залога добыть не
    удалось: она угадывает конфигурационный бонус и протокольную комиссию, а на 43827262
    сошлась с замером ($2,491.5) лишь потому, что обе поправки там совпали с прошитыми."""
    if debt_usd and seized_usd:
        return max(0.0, seized_usd - debt_usd), "measured"
    if debt_usd:
        bps = TOKENS.get(coll_sym, {}).get("bonus_bps", 11000)
        return debt_usd * max(0, bps - 10000) / 1e4 * 0.9, "modelled"
    return None, "none"


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
            all_txs = blk.get("transactions", [])
            txs = all_txs[:ANATOMY_MAX_TX]
            anatomy, win, shift = [], None, False
            for pos, t in enumerate(txs):
                rc = rpc.call("eth_getTransactionReceipt", [t["hash"]])
                # idx берётся из КВИТАНЦИИ, не из позиции в листинге блока (вскрытие 22.08,
                # блок 43827262): rpc.hyperliquid.xyz/evm и drpc опускают системные tx
                # HyperCore (gasPrice=0, gasUsed=0) из eth_getBlockByNumber, а квитанция
                # несёт каноничный индекс. У официального узла листинг и квитанция съезжают
                # согласованно (он перенумеровывает) — расхождение фиксируется флагом
                # idx_shift, чтобы неоднозначность жила В ДАННЫХ, а не в комментарии.
                idx = int(rc["transactionIndex"], 16)
                if idx != pos:
                    shift = True
                row = {"idx": idx,
                       "to": (t.get("to") or "")[:14], "from": t["from"][:14],
                       "gas": int(rc["gasUsed"], 16), "st": int(rc["status"], 16),
                       "tip_gwei": round((int(rc["effectiveGasPrice"], 16) - base) / 1e9, 2),
                       "inb": len(t.get("input", "0x")) // 2 - 1,
                       # оракул-апдейты внутри tx (Pyth PriceFeedUpdate / RS ValueUpdate):
                       # upd>0 у победителя = атомарный self-push, гонка была невыигрываема
                       "upd": sum(1 for lg in rc.get("logs", [])
                                  if lg["topics"] and lg["topics"][0] in _TOPIC_ORACLE_UPDATES)}
                anatomy.append(row)
                if t["hash"] == e["tx"]:
                    win = row
            push_idx = _push_indices(anatomy)
            dpx = _asset_price_usd(rpc, e["debt"])
            dec = DECIMALS.get(e["debt"].lower())
            debt_usd = (e["cover"] / 10 ** dec * dpx) if (dpx and dec is not None) else None
            sym_c = ADDR_TO_SYMBOL.get(e["coll"].lower(), e["coll"][:10])
            cpx = _asset_price_usd(rpc, e["coll"])
            cdec = DECIMALS.get(e["coll"].lower())
            seized_usd = (e["seized"] / 10 ** cdec * cpx) if (cpx and cdec is not None) else None
            bonus_usd, bonus_src = _bonus_usd(debt_usd, seized_usd, sym_c)
            bal = st_snapshot.get("balance_hype")
            rec = {"iso": _now_iso(), **e,
                   "coll_sym": sym_c, "debt_sym": ADDR_TO_SYMBOL.get(e["debt"].lower(), e["debt"][:10]),
                   "debt_usd": round(debt_usd, 2) if debt_usd else None,
                   "seized_usd": round(seized_usd, 2) if seized_usd else None,
                   "bonus_usd_est": round(bonus_usd, 2) if bonus_usd else None,
                   "bonus_src": bonus_src,
                   "base_gwei": round(base / 1e9, 3),
                   "win": win, "push_idx": push_idx, "block_txs": anatomy,
                   "ntx": len(all_txs), "trunc": len(all_txs) > ANATOMY_MAX_TX,
                   "idx_shift": shift,
                   "ours": {"in_book": our_view.get("in_book", {}).get(e["victim"].lower()),
                            "in_hot": our_view.get("in_hot", {}).get(e["victim"].lower()),
                            "size_ok": (debt_usd >= C.MIN_DEBT_USD) if debt_usd else None,
                            "floor_ok": (bal is not None
                                         and bal * 1e18 >= C.GAS_LIMIT * 2 * base) or None,
                            "guard": st_snapshot.get("guard")}}
            _append(rec)
            if _above_fire_floor(debt_usd, bonus_usd):
                self_push = f", self-push x{win['upd']}" if win and win.get("upd") else ""
                _notify_inbox(
                    f"🏁 чужая ликвидация выше нашего пола: {rec['debt_sym']}->{sym_c} "
                    f"${debt_usd:,.0f} (бонус ~${bonus_usd:,.0f}) блок {e['block']} "
                    f"tip {win['tip_gwei'] if win else '?'} gwei{self_push}, "
                    f"победитель {e['liquidator'][:10]}; "
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
