"""Маршруты выхода из залога: LiquidSwap + Pendle + KyberSwap под одним интерфейсом.

ЗАЧЕМ. Preflight 05.08 показал, что единственный квотер бота (LiquidSwap) не обслуживает
самые дорогие залоги книги: PT-kHYPE обеих серий — HTTP 404 на любом размере, а beHYPE
отвечает «успех» и КОНСТАНТУ 1.690 WHYPE независимо от входа (impact ровно 90%) — то есть
отравленную котировку, а не честный отказ. Главный приз книги — кит на PT-kHYPE-SEP26 —
был недостижим не по экономике, а по отсутствию маршрута.

ЧТО ДОБАВЛЕНО (оба проверены живыми запросами и симуляцией eth_call):
  * Pendle (роутер 0x8888…F946, SDK /core/v2/sdk/999/): для PT до maturity — swap через
    рынок, ПОСЛЕ maturity — redeem. Замер: 880 PT -> 877.07 WHYPE, impact 0.17%; на $1M
    impact 0.07%. Ветка выбирается по on-chain isExpired(), а не по дате в коде: дата в
    константе протухнет молча, а флаг контракта — нет.
  * KyberSwap (роутер 0x6131B5fa…, /hyperevm/api/v1/): универсальный запасной маршрут.
    Замер beHYPE: 500 -> 499.65 WHYPE. ВНИМАНИЕ: у beHYPE ~92% глубины держат лимитные
    ордера ОДНОГО маркет-мейкера, обрыв ~$59k; поэтому котировка обязательно берётся
    непосредственно перед выстрелом, а on-chain minProfit остаётся последним рубежом.

Контракт венчур-агностичен (_forceApprove(collateral, swapTarget) + swapTarget.call), так
что адаптеру достаточно вернуть адрес роутера и calldata в том же виде, что даёт liqd.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

from analysis.protocols import ADDR_TO_SYMBOL
from analysis.rpc import _run_with_deadline
from bot import config as C
from bot.liqd import LiqdError, NoRouteError, SWAP_INPUT_HAIRCUT
from bot import liqd

PENDLE_API = "https://api-v2.pendle.finance/core/v2/sdk/999"
PENDLE_ROUTER = "0x888888888889758F76e7103c6CbF23ABbF58F946"
KYBER_API = "https://aggregator-api.kyberswap.com/hyperevm/api/v1"

ROUTERS_ENABLED = os.environ.get("HL_MULTI_ROUTER", "1") == "1"
KYBER_FALLBACK = os.environ.get("HL_KYBER_FALLBACK", "1") == "1"
QUOTE_TIMEOUT = float(os.environ.get("HL_ROUTER_TIMEOUT", "20"))

# Карта Pendle-рынков HyperEVM. yt нужен ветке redeem, market — ветке swap.
# Проверено 05.08: PT-24SEP2026 живой (isExpired=false), PT-19MAR2026 уже погашаемый.
PENDLE_PT = {
    "0x50fc4edc6346f36993bb30fe60e932504ed17391": {
        "sym": "PT-kHYPE-24SEP2026",
        "market": "0xb48b0c95b2ddc464484305b7363fad5bd5b7a683",
        "yt": None,
        "underlying": "0xfD739d4e423301CE9385c1fb8850539D657C296D",   # kHYPE
    },
    "0xea84ca9849d9e76a78b91f221f84e9ca065fc9f5": {
        "sym": "PT-kHYPE-19MAR2026",
        "market": None,
        "yt": "0x8e8df024cf6d3e916be0821ff3177db6981fcad2",
        "underlying": "0xfD739d4e423301CE9385c1fb8850539D657C296D",
    },
}

# Вход второй ноги берётся с запасом от того, что обещала первая: роутер тянет ровно
# зашитый amountIn, и если первая нога отдаст чуть меньше обещанного, вторая упадёт.
LEG2_HAIRCUT = float(os.environ.get("HL_LEG2_HAIRCUT", "0.01"))

# ЗАМЕРЕННАЯ глубина выхода, $ на один выстрел (preflight 05.08 + форк-экзамен).
# Зачем потолок, если есть порог импакта: выше обрыва КОТИРОВКА ВРЁТ. На $1.15M kHYPE->WHYPE
# квотер вернул impact −0.03% (то есть «лучше оракула»), а своп в бою ревертнул в самом пуле
# (custom error 0xd93c0665) — форк-экзамен 05.08. Порог импакта такую котировку не ловит по
# построению, поэтому размер режется по факту замера, а лестница чанков сама спускается ниже.
# Ключ — символ актива, который ПРОДАЁТ последняя нога.
EXIT_CAP_USD = {
    "kHYPE": float(os.environ.get("HL_CAP_KHYPE", "600000")),    # чисто до $600k, обрыв на $700k
    "wstHYPE": float(os.environ.get("HL_CAP_WSTHYPE", "65000")),  # чисто до $65k, 9.9% на $75k
    "beHYPE": float(os.environ.get("HL_CAP_BEHYPE", "55000")),    # обрыв ~$59k, 92% глубины — один маркет-мейкер
}


class SizeBeyondDepth(NoRouteError):
    """Размер выше замеренной глубины: лестница чанков обязана спуститься, а не верить квотеру."""


def _cap_for(symbol: str | None) -> float | None:
    return EXIT_CAP_USD.get(symbol or "")


def check_depth(sell_symbol: str | None, usd: float) -> None:
    cap = _cap_for(sell_symbol)
    if cap and usd > cap:
        raise SizeBeyondDepth(f"{sell_symbol}: ${usd:,.0f} выше замеренной глубины ${cap:,.0f}")

# Удобный алиас для тестов ветки redeem (истёкшая серия).
PT_EXPIRED_SAMPLE = "0xea84ca9849D9e76a78B91F221F84e9Ca065FC9f5"

SEL_IS_EXPIRED = "0x83b4c56c"          # isExpired() — keccak("isExpired()")[:4]
_expiry_cache: dict = {}               # pt -> (expired: bool, ts)
EXPIRY_TTL = 900.0


def _get_json(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _post_json(url: str, payload: dict, timeout: float) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, body, headers={"Content-Type": "application/json",
                                                     "User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def is_pt(token: str) -> bool:
    return token.lower() in PENDLE_PT


def pt_expired(rpc, token: str) -> bool:
    """isExpired() с самого PT. Дата maturity в коде протухла бы молча — спрашиваем контракт."""
    key = token.lower()
    hit = _expiry_cache.get(key)
    if hit and time.monotonic() - hit[1] < EXPIRY_TTL:
        return hit[0]
    val = bool(PENDLE_PT[key]["yt"] and not PENDLE_PT[key]["market"])   # запасная догадка
    try:
        raw = rpc.eth_call(token, SEL_IS_EXPIRED)
        val = int(raw, 16) == 1
    except Exception:                          # noqa: BLE001 — не знаем точно, берём карту
        pass
    _expiry_cache[key] = (val, time.monotonic())
    return val


def _norm(amount_out: int, impact: float, to: str, data: str, amount_in: int, venue: str) -> dict:
    return {"ok": True, "amount_out": int(amount_out), "price_impact": float(impact or 0.0),
            "swap_target": to, "swap_calldata": data, "amount_in_used": int(amount_in),
            "venue": venue, "raw": None}


def quote_pendle(rpc, token_in: str, token_out: str, amount_in_wei: int,
                 receiver: str | None = None, timeout: float | None = None,
                 aggregator: bool = True) -> dict:
    """PT -> token_out через Pendle. До maturity — swap по рынку, после — redeem.

    aggregator=False просит МАРШРУТ БЕЗ внешнего свопа: он существует только для выхода в
    сам underlying (kHYPE), зато лёгкий (2085 байт против 4421) и, главное, влезает в газ.
    Прямой PT->WHYPE с агрегатором на форке падал по out of gas во вложенном вызове."""
    key = token_in.lower()
    meta = PENDLE_PT.get(key)
    if not meta:
        raise NoRouteError("не Pendle PT")
    receiver = receiver or C.CONTRACT
    if not receiver:
        raise NoRouteError("Pendle: получатель не задан (HL_CONTRACT пуст)")
    tmo = timeout or QUOTE_TIMEOUT
    common = {"receiver": receiver, "slippage": "0.01",
              "enableAggregator": "true" if aggregator else "false",
              "tokenOut": token_out, "amountIn": str(int(amount_in_wei))}
    if pt_expired(rpc, token_in):
        if not meta["yt"]:
            raise NoRouteError(f"Pendle: {meta['sym']} истёк, но yt неизвестен")
        url = f"{PENDLE_API}/redeem?" + urllib.parse.urlencode({**common, "yt": meta["yt"]})
    else:
        if not meta["market"]:
            raise NoRouteError(f"Pendle: {meta['sym']} не истёк, но рынок неизвестен")
        url = f"{PENDLE_API}/markets/{meta['market']}/swap?" + urllib.parse.urlencode(
            {**common, "tokenIn": token_in})
    try:
        d = _get_json(url, tmo)
    except urllib.error.HTTPError as e:
        raise NoRouteError(f"Pendle HTTP {e.code}") from e
    except (OSError, ValueError) as e:
        raise LiqdError(f"Pendle недоступен: {e}") from e
    tx = d.get("tx") or {}
    data = d.get("data") or {}
    if not tx.get("to") or not tx.get("data") or not data.get("amountOut"):
        raise NoRouteError("Pendle: ответ без calldata/amountOut")
    return _norm(int(data["amountOut"]), float(data.get("priceImpact") or 0.0),
                 tx["to"], tx["data"], amount_in_wei, "pendle")


def quote_kyber(token_in: str, token_out: str, amount_in_wei: int,
                receiver: str | None = None, timeout: float | None = None) -> dict:
    """Универсальный запасной маршрут: routes -> route/build (ключ не нужен)."""
    receiver = receiver or C.CONTRACT
    if not receiver:
        raise NoRouteError("Kyber: получатель не задан (HL_CONTRACT пуст)")
    tmo = timeout or QUOTE_TIMEOUT
    q = urllib.parse.urlencode({"tokenIn": token_in, "tokenOut": token_out,
                                "amountIn": str(int(amount_in_wei))})
    try:
        r = _get_json(f"{KYBER_API}/routes?{q}", tmo)
    except urllib.error.HTTPError as e:
        raise NoRouteError(f"Kyber HTTP {e.code}") from e
    except (OSError, ValueError) as e:
        raise LiqdError(f"Kyber недоступен: {e}") from e
    if r.get("code") != 0 or not (r.get("data") or {}).get("routeSummary"):
        raise NoRouteError(f"Kyber: {str(r.get('message'))[:60]}")
    summary = r["data"]["routeSummary"]
    try:
        b = _post_json(f"{KYBER_API}/route/build",
                       {"routeSummary": summary, "sender": receiver, "recipient": receiver,
                        "slippageTolerance": 100}, tmo)
    except urllib.error.HTTPError as e:
        raise NoRouteError(f"Kyber build HTTP {e.code}") from e
    except (OSError, ValueError) as e:
        raise LiqdError(f"Kyber build недоступен: {e}") from e
    d = b.get("data") or {}
    if b.get("code") != 0 or not d.get("data") or not d.get("routerAddress"):
        raise NoRouteError(f"Kyber build: {str(b.get('message'))[:60]}")
    # импакт по долларовым полям маршрута (Kyber не отдаёт его отдельным числом)
    try:
        ain, aout = float(summary["amountInUsd"]), float(summary["amountOutUsd"])
        impact = max(0.0, 1.0 - aout / ain) if ain > 0 else 0.0
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        impact = 0.0
    return _norm(int(d["amountOut"]), impact, d["routerAddress"], d["data"],
                 amount_in_wei, "kyber")


def quote_best(rpc, token_in: str, token_out: str, amount_in_wei: int,
               in_dec: int, out_dec: int, wall_sec: float | None = None) -> dict:
    """Лучшая котировка выхода среди доступных площадок, в формате liqd.quote.

    Порядок не «перебрать всё», а «спросить того, кто умеет»: PT обслуживает только Pendle,
    остальное — LiquidSwap, а Kyber включается, когда LiquidSwap отказал ИЛИ вернул мусор.
    Мусор — это отдельный класс: beHYPE у LiquidSwap отдаёт «успех» с константой на любой
    вход; такую котировку ловит порог импакта, но лучше просто взять живой маршрут.
    """
    if ROUTERS_ENABLED and is_pt(token_in):
        return quote_pendle(rpc, token_in, token_out, amount_in_wei)

    first_err: Exception | None = None
    best: dict | None = None
    try:
        best = liqd.quote(token_in, token_out, amount_in_wei, in_dec, out_dec, wall_sec=wall_sec)
        best.setdefault("venue", "liqd")     # площадка помечается всегда — для лога и разбора
    except (NoRouteError, LiqdError) as e:
        first_err = e

    if not (ROUTERS_ENABLED and KYBER_FALLBACK):
        if best is not None:
            return best
        raise first_err or NoRouteError("нет маршрута")

    # LiquidSwap отказал или котировка выглядит мусорной -> спросить Kyber
    garbage = best is not None and (best.get("price_impact") or 0.0) >= C.MAX_IMPACT
    if best is not None and not garbage:
        return best
    try:
        alt = quote_kyber(token_in, token_out, amount_in_wei)
    except (NoRouteError, LiqdError) as e:
        if best is not None:
            return best                      # мусорная, но пусть решает порог импакта выше
        raise first_err or e
    if best is None or alt["amount_out"] > best["amount_out"]:
        return alt
    return best


def quote_pt_two_leg(rpc, pt_token: str, debt_token: str, seized_wei: int,
                     coll_dec: int, debt_dec: int, haircut: float,
                     usd_hint: float | None = None) -> dict:
    """Выход из PT в ДВЕ ноги: PT -> underlying (Pendle, без агрегатора) -> долг (LiquidSwap).

    Одной ногой не выходит: прямой маршрут PT->WHYPE у Pendle тянет вложенный своп агрегатора
    и падает по out of gas — форк 05.08 дал SwapFailed и при 2.5M, и при 2.9M, при этом залога
    хватало (проверено запасом haircut 6%), а малый блок HyperEVM даёт всего 3M. Лёгкая ветка
    PT->underlying в бюджет влезает: замер двух ног целиком — 1,384,844 газа (46% лимита),
    долг заёмщика на форке уменьшился на $464,727.

    Если долг И ЕСТЬ underlying, вторая нога не нужна — возвращаем обычную одноногую котировку.
    Узкое место по размеру теперь ВТОРАЯ нога (kHYPE->WHYPE держит до ~$600k), и лестница
    чанков находит проходной размер сама: на f=0.35 impact второй ноги был 47%, на f=0.06 —
    0.009%.
    """
    meta = PENDLE_PT[pt_token.lower()]
    under = meta["underlying"]
    amount_in = int(seized_wei * (1.0 - haircut))
    # у двуногого выхода узкое место — ВТОРАЯ нога: продаётся underlying, а не PT
    check_depth(ADDR_TO_SYMBOL.get(under.lower()), usd_hint or 0.0)
    if under.lower() == debt_token.lower():
        return quote_pendle(rpc, pt_token, debt_token, amount_in, aggregator=False)

    leg1 = quote_pendle(rpc, pt_token, under, amount_in, aggregator=False)
    mid_in = int(leg1["amount_out"] * (1.0 - LEG2_HAIRCUT))
    leg2 = liqd.quote(under, debt_token, mid_in, 18, debt_dec)
    return {
        "ok": True,
        "amount_out": leg2["amount_out"],
        # импакт складываем: гейт MAX_IMPACT должен видеть ПОЛНУЮ просадку пути, а не одной ноги
        "price_impact": (leg1.get("price_impact") or 0.0) + (leg2.get("price_impact") or 0.0),
        "swap_target": leg1["swap_target"],
        "swap_calldata": leg1["swap_calldata"],
        "mid_asset": under,
        "swap_target2": leg2["swap_target"],
        "swap_calldata2": leg2["swap_calldata"],
        "amount_in_used": amount_in,
        "venue": "pendle+liqd",
        "raw": None,
    }


def quote_for_seized_multi(rpc, coll_token: str, debt_token: str, seized_wei: int,
                           coll_decimals: int, debt_decimals: int,
                           haircut: float = SWAP_INPUT_HAIRCUT,
                           usd_hint: float | None = None) -> dict:
    """Котировка выхода по всем площадкам, в формате liqd.quote_for_seized.

    ВАЖНО про шов: обычный путь идёт РОВНО через liqd.quote_for_seized — то есть ту же
    функцию, которую подменяют офлайн-тесты лестницы чанков. Обход этого шва увёл бы тесты
    в живую сеть и превратил бы их в интеграционные, ничего при этом не проверив.
    Kyber включается только когда LiquidSwap отказал ИЛИ вернул мусор (константу на любой
    вход — замер beHYPE 05.08), PT сразу уходит в Pendle.
    """
    if ROUTERS_ENABLED and is_pt(coll_token):
        q = quote_pt_two_leg(rpc, coll_token, debt_token, seized_wei,
                             coll_decimals, debt_decimals, haircut, usd_hint)
        q["haircut"] = haircut
        return q

    check_depth(ADDR_TO_SYMBOL.get(coll_token.lower()), usd_hint or 0.0)

    first_err: Exception | None = None
    best: dict | None = None
    try:
        best = liqd.quote_for_seized(coll_token, debt_token, seized_wei,
                                     coll_decimals, debt_decimals)
        best.setdefault("venue", "liqd")
    except (NoRouteError, LiqdError) as e:
        first_err = e

    if not (ROUTERS_ENABLED and KYBER_FALLBACK):
        if best is not None:
            return best
        raise first_err or NoRouteError("нет маршрута")

    garbage = best is not None and (best.get("price_impact") or 0.0) >= C.MAX_IMPACT
    if best is not None and not garbage:
        return best
    try:
        alt = quote_kyber(coll_token, debt_token, int(seized_wei * (1.0 - haircut)))
        alt["haircut"] = haircut
    except (NoRouteError, LiqdError) as e:
        if best is not None:
            return best              # мусорная — её отклонит порог импакта выше по стеку
        raise first_err or e
    if best is None or alt["amount_out"] > best["amount_out"]:
        return alt
    return best
