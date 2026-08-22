"""Инструмент вскрытия 22.08: ЧТО именно связывает предиктивный слой (bot/spec.py).

Два замера, оба по цепи, оба с позитивным контролем — потому что «слой молчит» само по
себе неотличимо от «добычи не было», а порог, названный по премиссе, надо сверять с
распределением, а не с воспоминанием о нём.

  census  — перепись чужих ликвидаций за окно. Каждая относится к одному из классов:
            * «реактивный»       — HF<1 уже на блоке B-1, реакция по подтверждению годится;
            * «пересечение В БЛОКЕ» — на B-1 HF>=1, а по ценам блока B (состояние ещё до
              ликвидации) HF<1: цель утопил пуш в том же блоке, реактивный путь такие не
              берёт ПО ПОСТРОЕНИЮ — это ровно класс spec-слоя.
            Для каждого пересечения считается HF_est формулой plan() и сверяется с цепью,
            и печатается, прошла бы цель гейт C.SPEC_HF_FIRE.
            Позитивный контроль состава: реконструированный HF на B-1 обязан сойтись с
            getUserAccountData того же блока, иначе событие помечается «состав не сошёлся»
            и в статистику классов НЕ идёт (молча не теряется — печатается отдельно).

  devs    — распределение девиаций ПРИНЯТЫХ пушей RedStone (шаг значения фида между
            соседними ValueUpdate). Премисса слоя — «релейер пушит только при >=0.50%,
            ниже пуша НЕ БУДЕТ» (C.SPEC_MIN_DEVIATION); здесь она проверяется замером.
            Позитивный контроль: пустая выборка = сканер слеп, вывод не годится (drpc
            молча отдаёт [] на широком окне — см. get_logs_chunked).

Запуск:  PYTHONPATH=. python3 analysis/spec_gap.py census [дней]
         PYTHONPATH=. python3 analysis/spec_gap.py devs   [часов]
Читает только архивный узел, ничего не пишет и не подписывает.
"""
from __future__ import annotations

import collections
import json
import sys

from analysis.keccak import selector
from analysis.protocols import (ADDR_TO_SYMBOL, ORACLE, POOL, POOL_DATA_PROVIDER, TOKENS,
                                TOPIC_LIQUIDATION_CALL)
from analysis.rpc import Rpc, get_logs_chunked
from bot import config as C

ARCHIVE = ["https://hyperliquid.drpc.org"]
ADAPTER_A = "0xe4ae88743c3834d0c492eabc47384c84bcadc6a6"
TOPIC_VALUE_UPDATE = C.TOPIC_VALUE_UPDATE
S_UAD = selector("getUserAccountData(address)")
S_UCFG = selector("getUserConfiguration(address)")
S_URD = selector("getUserReserveData(address,address)")
S_CFG = selector("getReserveConfigurationData(address)")
S_PX = selector("getAssetPrice(address)")
S_LIST = selector("getReservesList()")
RECON_TOL = 0.002          # допуск позитивного контроля состава (0.2% HF)


def _ea(a: str) -> str:
    return a.lower().replace("0x", "").rjust(64, "0")


class _Chain:
    """Чтения с пер-БЛОЧНЫМ кэшем. Ключ кэша конфигурации — (адрес, блок), а не адрес:
    ликвидационный порог резерва меняется во времени, и кэш «по адресу» тихо переносит
    сегодняшний LT на события двухнедельной давности."""

    def __init__(self, rpc: Rpc) -> None:
        self.r = rpc
        self._px: dict = {}
        self._lt: dict = {}
        self._list: dict = {}

    def px(self, asset: str, blk: int) -> float:
        k = (asset.lower(), blk)
        if k not in self._px:
            self._px[k] = int(self.r.eth_call(ORACLE, S_PX + _ea(asset), hex(blk)), 16) / 1e8
        return self._px[k]

    def lt(self, asset: str, blk: int) -> float:
        k = (asset.lower(), blk)
        if k not in self._lt:
            d = self.r.eth_call(POOL_DATA_PROVIDER, S_CFG + _ea(asset), hex(blk))[2:]
            self._lt[k] = int(d[2 * 64:3 * 64], 16) / 1e4
        return self._lt[k]

    def reserves(self, blk: int) -> list[str]:
        if blk not in self._list:
            d = self.r.eth_call(POOL, S_LIST, hex(blk))[2:]
            n = int(d[64:128], 16)
            self._list[blk] = ["0x" + d[128 + i * 64 + 24:128 + (i + 1) * 64] for i in range(n)]
        return self._list[blk]

    def account(self, user: str, blk: int) -> dict:
        d = self.r.eth_call(POOL, S_UAD + _ea(user), hex(blk))[2:]
        w = [int(d[i * 64:(i + 1) * 64], 16) for i in range(6)]
        return {"coll_usd": w[0] / 1e8, "debt_usd": w[1] / 1e8, "hf": w[5] / 1e18,
                "tc_base": w[0], "td_base": w[1]}

    def position(self, user: str, blk: int) -> list[dict]:
        bm = int(self.r.eth_call(POOL, S_UCFG + _ea(user), hex(blk)), 16)
        out = []
        for i, a in enumerate(self.reserves(blk)):
            if not ((bm >> (2 * i)) & 3):
                continue
            sym = ADDR_TO_SYMBOL.get(a.lower())
            if sym is None:                       # актив вне реестра — состав не полон
                out.append({"a": a, "sym": None})
                continue
            d = self.r.eth_call(POOL_DATA_PROVIDER, S_URD + _ea(a) + _ea(user), hex(blk))[2:]
            w = [int(d[j * 64:(j + 1) * 64], 16) for j in range(9)]
            out.append({"a": a, "sym": sym, "dec": TOKENS[sym]["decimals"],
                        "at": w[0], "db": w[1] + w[2], "coll_on": bool(w[8] & 1)})
        return out


def _hf_from(pos: list[dict], ch: _Chain, px_blk: int, lt_blk: int) -> float | None:
    if any(p["sym"] is None for p in pos):
        return None
    c = sum(p["at"] / 10 ** p["dec"] * ch.px(p["a"], px_blk) * ch.lt(p["a"], lt_blk)
            for p in pos if p["coll_on"])
    d = sum(p["db"] / 10 ** p["dec"] * ch.px(p["a"], px_blk) for p in pos)
    return c / d if d > 0 else None


def _hf_est(acct: dict, pos: list[dict], ev: dict, ch: _Chain, blk: int) -> float | None:
    """HF_est формулой bot/spec.py::plan() — доли пары (coll, debt) и масштабы их цен."""
    by = {p["a"].lower(): p for p in pos if p["sym"]}
    pc, pd = by.get(ev["coll"].lower()), by.get(ev["debt"].lower())
    if not pc or not pd or acct["tc_base"] <= 0 or acct["td_base"] <= 0:
        return None
    px_c0, px_d0 = ch.px(ev["coll"], blk - 1), ch.px(ev["debt"], blk - 1)
    f_c = min(1.0, pc["at"] / 10 ** pc["dec"] * px_c0 / acct["coll_usd"])
    f_d = min(1.0, pd["db"] / 10 ** pd["dec"] * px_d0 / acct["debt_usd"])
    s_c = ch.px(ev["coll"], blk) / px_c0 if px_c0 else 1.0
    s_d = ch.px(ev["debt"], blk) / px_d0 if px_d0 else 1.0
    denom = f_d * (s_d - 1.0) + 1.0
    return acct["hf"] * (f_c * (s_c - 1.0) + 1.0) / denom if denom > 0 else None


def census(days: float = 14.0) -> dict:
    r = Rpc(ARCHIVE, timeout=30, retries=4)
    ch = _Chain(r)
    head = r.block_number()
    frm = head - int(days * 24 * 3600)          # HyperEVM: малый блок ~1с
    print(f"# окно [{frm},{head}] (~{days} сут), гейт C.SPEC_HF_FIRE={C.SPEC_HF_FIRE}, "
          f"размерный фильтр ${C.MIN_DEBT_USD:,.0f}")
    logs = get_logs_chunked(r, POOL, [TOPIC_LIQUIDATION_CALL], frm, head)
    if not logs:
        raise SystemExit("СКАНЕР ПУСТ: ликвидаций не найдено — до вывода проверить окно/узел "
                         "(пустой ответ drpc на широком окне неотличим от отсутствия событий)")
    print(f"# LiquidationCall: {len(logs)}")
    cls = collections.Counter()
    crossings, unresolved = [], []
    for lg in logs:
        b = int(lg["blockNumber"], 16)
        d = lg["data"][2:]
        ev = {"block": b, "tx": lg["transactionHash"],
              "coll": "0x" + lg["topics"][1][-40:], "debt": "0x" + lg["topics"][2][-40:],
              "victim": "0x" + lg["topics"][3][-40:],
              "cover": int(d[0:64], 16), "seized": int(d[64:128], 16),
              "liquidator": "0x" + d[128:192][-40:]}
        try:
            acct = ch.account(ev["victim"], b - 1)
            if acct["debt_usd"] <= 0:
                cls["позиции на B-1 нет"] += 1
                continue
            pos = ch.position(ev["victim"], b - 1)
            recon = _hf_from(pos, ch, b - 1, b - 1)
            if recon is None or abs(recon - acct["hf"]) > RECON_TOL * max(1.0, acct["hf"]):
                cls["состав не сошёлся"] += 1
                unresolved.append({**ev, "hf_prev": acct["hf"], "debt_usd": acct["debt_usd"],
                                   "hf_recon": recon})
                continue
            post = _hf_from(pos, ch, b, b - 1)
            if acct["hf"] < 1.0:
                cls["реактивный (HF<1 уже на B-1)"] += 1
            elif post is not None and post < 1.0:
                cls["пересечение В БЛОКЕ"] += 1
                est = _hf_est(acct, pos, ev, ch, b)
                crossings.append({**ev, "hf_prev": acct["hf"], "hf_post": post, "hf_est": est,
                                  "debt_usd": acct["debt_usd"],
                                  "gate": bool(est is not None and est < C.SPEC_HF_FIRE),
                                  "size_ok": acct["debt_usd"] >= C.MIN_DEBT_USD})
            else:
                cls["иное (ценой не пересекал)"] += 1
        except Exception as e:                   # noqa: BLE001
            cls[f"ошибка чтения"] += 1
            unresolved.append({**ev, "err": str(e)[:160]})
    print("\n# классы:")
    for k, v in cls.most_common():
        print(f"  {v:>3}  {k}")
    print(f"\n# пересечения В БЛОКЕ (класс предиктивного слоя): {len(crossings)}")
    for x in sorted(crossings, key=lambda x: -x["debt_usd"]):
        e = f"{x['hf_est']:.6f}" if x["hf_est"] is not None else "?"
        err = (f"{x['hf_est'] - x['hf_post']:+.6f}" if x["hf_est"] is not None else "?")
        print(f"  блок {x['block']} долг ${x['debt_usd']:>10,.0f} "
              f"HF {x['hf_prev']:.6f} -> {x['hf_post']:.6f} | HF_est={e} (ошибка {err}) | "
              f"гейт {C.SPEC_HF_FIRE}: {'ОГОНЬ' if x['gate'] else 'МОЛЧАНИЕ'} | "
              f"размер: {'прошёл' if x['size_ok'] else 'ниже фильтра'} | {x['liquidator'][:10]}")
    if unresolved:
        # НЕ молчаливое усечение: что выпало из знаменателя, обязано быть названо
        print(f"\n# вне статистики ({len(unresolved)}) — долги: "
              f"{sorted(round(u.get('debt_usd', 0), 2) for u in unresolved)}")
    return {"classes": dict(cls), "crossings": crossings, "unresolved": unresolved}


def devs(hours: float = 8.0) -> dict:
    r = Rpc(ARCHIVE, timeout=30, retries=4)
    head = r.block_number()
    frm = head - int(hours * 3600)
    logs = get_logs_chunked(r, ADAPTER_A, [TOPIC_VALUE_UPDATE], frm, head)
    if not logs:
        raise SystemExit("СКАНЕР ПУСТ: ни одного ValueUpdate — вывод о девиациях не годится")
    print(f"# ValueUpdate адаптера A за ~{hours}ч: {len(logs)}; "
          f"премисса слоя C.SPEC_MIN_DEVIATION={C.SPEC_MIN_DEVIATION:.4f}")
    last: dict = {}
    per: dict = collections.defaultdict(list)
    for lg in sorted(logs, key=lambda l: (int(l["blockNumber"], 16), int(l["logIndex"], 16))):
        d = lg["data"][2:]
        val = int(d[0:64], 16)
        feed = bytes.fromhex(d[64:128]).rstrip(b"\x00").decode("ascii", "replace")
        if feed in last and last[feed] > 0:
            per[feed].append(abs(val - last[feed]) / last[feed])
        last[feed] = val
    allv: list[float] = []
    for f, ds in sorted(per.items(), key=lambda kv: -len(kv[1])):
        if len(ds) < 3:
            continue
        a = sorted(x * 100 for x in ds)
        allv += a
        below = sum(1 for x in a if x < C.SPEC_MIN_DEVIATION * 100)
        print(f"  {f:<28} n={len(a):>3} мин={a[0]:.4f}% медиана={a[len(a)//2]:.4f}% "
              f"макс={a[-1]:.4f}% ниже порога: {below}/{len(a)}")
    allv.sort()
    below = sum(1 for x in allv if x < C.SPEC_MIN_DEVIATION * 100)
    print(f"  ИТОГО n={len(allv)}: ниже порога {below} ({below * 100 // max(1, len(allv))}%), "
          f"минимальный ПРИНЯТЫЙ шаг {allv[0]:.4f}%" if allv else "  выборки нет")
    return {"per_feed": {k: sorted(v) for k, v in per.items()}, "all": allv}


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "census"
    arg = float(sys.argv[2]) if len(sys.argv) > 2 else None
    if what == "census":
        census(arg or 14.0)
    elif what == "devs":
        devs(arg or 8.0)
    else:
        raise SystemExit(__doc__)
