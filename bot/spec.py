"""Spec-fire: данные для атомарного self-push + ликвидации (liquidateWithPush, контракт v2).

Роль модуля — держать два кэша тёплыми ВНЕ горячего пути и отвечать на один вопрос за
микросекунды: «делает ли свежая подписанная цена гейтвея армленную цель ликвидируемой, и
каким пушем это доказывается on-chain?»

  * гейтвей-кэш (_gw): подписанные RedStone-пакеты по фидам из WATCH — один GET с gzip на
    ВСЕ фиды (~350KB; фильтр-параметры гейтвей игнорирует), подписи восстанавливаются один
    раз и кэшируются по base64-строке (ecrecover без coincurve стоит ~6мс — на каждом тике
    без кэша это ~200мс CPU на 2-ядерной VPS);
  * chain-кэш (_chain): getLastUpdateDetails(feedId) обоих адаптеров одним multicall —
    знаменатель масштаба цены и гейт «наш пакет строго новее хранимого» (не-новее пуш
    адаптер отвергает, и выстрел без изменения цены гарантированно ревертится HF-чеком).

Карта DRIVERS: цена фасада = произведению значений фидов (доказано eth_call 04.08 бит-в-бит:
WHYPE=A.HYPE; wstHYPE=A.wstHYPE_FUNDAMENTAL*A.HYPE, расхождение < 1e-6; UBTC=A.BTC;
UETH=B.ETH; USDT0=A.USDT; USOL=A.SOL; фасад USDH = фасад USDC — один адрес). Пуш адаптера
двигает цену пула 1:1. kHYPE/beHYPE/PT-*/USDHL/USR в карте НЕТ: их фасады не раскрывают
getPriceFeedAdapter(), а числовая сверка не бьётся ни с одной комбинацией известных фидов
(kHYPE 56.2778 против B.kHYPE 56.2733 — близко, но НЕ равно) — spec по ним не стреляет,
реактивный путь их берёт как раньше.

Оценка HF при подмене цен (plan): HF_est = HF * (f_c*(s_c-1)+1) / (f_d*(s_d-1)+1), где
s_c/s_d — произведение масштабов gw/chain фидов-драйверов НА ПУШЕННОМ адаптере (фиды другого
адаптера наш пуш не двинет — масштаб 1), f_c/f_d — доля пары (coll, debt) в total_collateral/
total_debt аккаунта (непокрытый остаток считается неподвижным). Для одноактивной позиции
формула точна; для смешанной — несмещённая оценка, страхуемая порогом C.SPEC_HF_FIRE и
on-chain гейтами контракта. Позиция coll==debt самогасится: s_c==s_d => HF_est==HF => не
стреляет (её HF цена не двигает — это класс процентного дрейфа, добыча реактивного пути).
"""
from __future__ import annotations

import threading
import time

from analysis import redstone as RS
from analysis.keccak import selector
from analysis.multicall import multicall
from analysis.protocols import TOKENS
from analysis.rpc import Rpc
from bot import config as C

SEL_LAST_UPDATE = selector("getLastUpdateDetails(bytes32)")   # 0xfb2eeb3b

ADAPTER_A = "0xe4ae88743c3834d0c492eabc47384c84bcadc6a6"
ADAPTER_B = "0x24c8964338deb5204b096039147b8e8c3aea42cc"


def _addr(sym: str) -> str:
    return TOKENS[sym]["address"].lower()


DRIVERS: dict[str, list[tuple[str, str]]] = {
    _addr("WHYPE"):   [("HYPE", ADAPTER_A)],
    _addr("wstHYPE"): [("wstHYPE_FUNDAMENTAL", ADAPTER_A), ("HYPE", ADAPTER_A)],
    _addr("UBTC"):    [("BTC", ADAPTER_A)],
    _addr("UETH"):    [("ETH", ADAPTER_B)],
    _addr("USDT0"):   [("USDT", ADAPTER_A)],
    _addr("USDe"):    [("USDe", ADAPTER_A)],
    _addr("USDC"):    [("USDC", ADAPTER_A)],
    _addr("USDH"):    [("USDC", ADAPTER_A)],
    _addr("sUSDe"):   [("sUSDe", ADAPTER_A)],
    _addr("USOL"):    [("SOL", ADAPTER_A)],
}
WATCH: list[tuple[str, str]] = sorted({fa for legs in DRIVERS.values() for fa in legs})

_lock = threading.Lock()
_gw: dict = {}        # feed -> {"pkgs": [...], "price": float, "ts_ms": int}
_chain: dict = {}     # (adapter, feed) -> {"value": int, "ts_ms": int}
_meta = {"gw_mono": 0.0, "chain_mono": 0.0, "hot": False, "err": None, "started": False}
_sig_seen: dict = {}  # signature(b64) -> восстановленный подписант (или None)


def _validated(feed: str, pkgs: list[dict]) -> tuple[list[dict], float, int] | None:
    """Пакеты фида, подписи которых восстановились в РАЗНЫХ авторизованных подписантов
    (порог адаптера — 3 на фид). None ниже порога. Возврат: (пакеты, медиана цены, max ts).
    Ключ кэша — сама подпись: она снята с keccak тела, коллизия тел под одной подписью
    невозможна без взлома ECDSA."""
    good: list[dict] = []
    seen: set[str] = set()
    vals: list[float] = []
    ts = 0
    for p in pkgs:
        ser = RS._serialize_package(p)
        if ser is None:
            continue
        body, sig = ser
        key = p.get("signature") or ""
        if key in _sig_seen:
            signer = _sig_seen[key]
        else:
            signer = RS._recover_signer(body, sig)
            if len(_sig_seen) > 8192:
                _sig_seen.clear()
            _sig_seen[key] = signer
        if signer is None or signer not in RS.AUTHORISED_SIGNERS or signer in seen:
            continue
        seen.add(signer)
        good.append(p)
        ts = max(ts, int(p["timestampMilliseconds"]))
        vals += [float(dp["value"]) for dp in p["dataPoints"] if dp.get("dataFeedId") == feed]
    if len(good) < RS.UNIQUE_SIGNERS_THRESHOLD or not vals:
        return None
    vals.sort()
    return good, vals[len(vals) // 2], ts


def _tick_gateway() -> None:
    data = RS.fetch_latest_all(timeout=3.0)
    now_ms = time.time() * 1000
    fresh: dict = {}
    for feed in {f for f, _ in WATCH}:
        pkgs = [p for p in (data.get(feed) or [])
                if now_ms - float(p.get("timestampMilliseconds", 0)) <= C.SPEC_MAX_AGE_MS]
        v = _validated(feed, pkgs)
        if v:
            fresh[feed] = {"pkgs": v[0], "price": v[1], "ts_ms": v[2]}
    with _lock:
        # update, не замена: частичный отказ гейтвея не стирает прежние фиды — их старость
        # гейтит возраст ts_ms в plan(), а не факт присутствия в кэше
        _gw.update(fresh)
        _meta["gw_mono"] = time.monotonic()


def _tick_chain(rpc: Rpc) -> None:
    calls = [(a, SEL_LAST_UPDATE + f.encode().ljust(32, b"\0").hex()) for f, a in WATCH]
    res = multicall(rpc, calls, retries=1)
    upd: dict = {}
    for (f, a), (ok, ret) in zip(WATCH, res):
        if not ok or len(ret) < 2 + 3 * 64:
            continue
        raw = ret[2:]
        # getLastUpdateDetails -> (lastDataTimestamp МИЛЛИСЕКУНДЫ, lastBlockTimestamp, lastValue)
        upd[(a, f)] = {"ts_ms": int(raw[0:64], 16), "value": int(raw[128:192], 16)}
    with _lock:
        _chain.update(upd)
        _meta["chain_mono"] = time.monotonic()


def _watch_loop() -> None:
    rpc = Rpc(C.READ_RPCS, timeout=C.RPC_TIMEOUT, retries=C.RPC_RETRIES, min_interval=0.05,
              backoff_429=0.2, hard_timeout=C.RPC_HARD_TIMEOUT, bench_sec=C.RPC_BENCH_SEC,
              slow_sec=C.RPC_SLOW_SEC)
    while True:
        t0 = time.monotonic()
        try:
            _tick_gateway()
            _meta["err"] = None
        except Exception as e:      # noqa: BLE001 — вотчер не смеет умереть от сетевой ошибки
            _meta["err"] = f"gw: {e}"
        try:
            _tick_chain(rpc)
        except Exception as e:      # noqa: BLE001
            _meta["err"] = f"chain: {e}"
        cadence = C.SPEC_POLL_HOT if _meta["hot"] else C.SPEC_POLL_COLD
        time.sleep(max(0.2, cadence - (time.monotonic() - t0)))


def start() -> None:
    """Идемпотентный запуск daemon-вотчера (вызывается из loop() executor'а)."""
    with _lock:
        if _meta["started"]:
            return
        _meta["started"] = True
    threading.Thread(target=_watch_loop, daemon=True, name="spec-watch").start()
    print(f"  spec-watch started: {len(WATCH)} feeds / 2 adapters, "
          f"poll {C.SPEC_POLL_HOT}s hot / {C.SPEC_POLL_COLD}s cold, "
          f"fire at HF_est < {C.SPEC_HF_FIRE}")


def set_hot(hot: bool) -> None:
    """Подсказка каденса: True — у кромки есть армленные цели, кэш обязан быть тёплым."""
    _meta["hot"] = bool(hot)


def staleness() -> str | None:
    """Диагноз холодных кэшей (вотчер мёртв / сеть лежит); None = всё тёплое. Мёртвый сторож
    хуже отсутствующего: армленная кромка при холодном кэше неотличима от «нет добычи»,
    поэтому executor печатает этот диагноз явно (сам — с троттлом)."""
    now = time.monotonic()
    parts = []
    if now - _meta["gw_mono"] > max(3 * C.SPEC_POLL_COLD, C.SPEC_MAX_AGE_MS / 1000):
        parts.append(f"gw {now - _meta['gw_mono']:.0f}s")
    if now - _meta["chain_mono"] > 3 * C.SPEC_POLL_COLD:
        parts.append(f"chain {now - _meta['chain_mono']:.0f}s")
    if not parts:
        return None
    err = f" ({_meta['err']})" if _meta["err"] else ""
    return "кэш холоден: " + ", ".join(parts) + err


def plan(acct: dict, t: dict) -> dict | None:
    """{"adapter","feeds","hf_est","pkgs","age_ms"}, если пуш ОДНОГО адаптера делает
    оценочный HF цели < C.SPEC_HF_FIRE; иначе None. Чистая функция над кэшами, сети не
    трогает — годится для вызова на каждой итерации по каждому арму."""
    dc = DRIVERS.get(t["coll_asset"].lower())
    dd = DRIVERS.get(t["debt_asset"].lower())
    if dc is None or dd is None:
        return None                 # актив вне доказанной карты — spec не про него
    tc = acct.get("total_collateral_base") or 0
    td = acct.get("total_debt_base") or 0
    if tc <= 0 or td <= 0:
        return None
    hf = acct["health_factor"] / 1e18
    f_c = min(1.0, t["coll_wei"] * t["coll_price"] / 10 ** t["coll_dec"] / tc)
    f_d = min(1.0, t["debt_wei"] * t["debt_price"] / 10 ** t["debt_dec"] / td)
    now_ms = time.time() * 1000
    now = time.monotonic()
    best: dict | None = None
    with _lock:
        if (now - _meta["gw_mono"] > C.SPEC_MAX_AGE_MS / 1000
                or now - _meta["chain_mono"] > 15):
            return None             # кэши не тёплые — оценке не на что опереться
        for adapter in {a for _, a in dc + dd}:
            s_c = s_d = 1.0
            feeds: list[str] = []
            # Фид масштабируется И пушится только когда пуш реально сдвинет его on-chain:
            # свежий пакет гейтвея, известное хранимое значение и «строго новее хранимого».
            # Всё остальное (чужой адаптер, старый пакет, chain свежее, пропуск multicall)
            # корректно даёт масштаб 1 — цену, которую мы не пушим, транзакция не изменит,
            # а on-chain HF уже несёт её текущее значение.
            for legs, is_coll in ((dc, True), (dd, False)):
                for f, a in legs:
                    if a != adapter:
                        continue
                    g = _gw.get(f)
                    ch = _chain.get((adapter, f))
                    if (not g or not ch or ch["value"] <= 0
                            or now_ms - g["ts_ms"] > C.SPEC_MAX_AGE_MS
                            or g["ts_ms"] <= ch["ts_ms"]):
                        continue
                    sc = g["price"] * 1e8 / ch["value"]
                    # Порог девиации адаптера (замер 04.08: принимаются только >= 0.5%).
                    # Ниже порога апдейт — молчаливый no-op (status=1, цена на месте), а
                    # выстрел по несдвинутой цене гарантированно ревертится по HF.
                    if abs(sc - 1.0) < C.SPEC_MIN_DEVIATION:
                        continue
                    if is_coll:
                        s_c *= sc
                    else:
                        s_d *= sc
                    if f not in feeds:
                        feeds.append(f)
            if not feeds:
                continue
            denom = f_d * (s_d - 1.0) + 1.0
            if denom <= 0:
                continue
            hf_est = hf * (f_c * (s_c - 1.0) + 1.0) / denom
            if hf_est < C.SPEC_HF_FIRE and (best is None or hf_est < best["hf_est"]):
                best = {"adapter": adapter, "feeds": feeds, "hf_est": hf_est,
                        "pkgs": [p for f in feeds for p in _gw[f]["pkgs"]],
                        "age_ms": max(now_ms - _gw[f]["ts_ms"] for f in feeds)}
    return best


def push_calldata(p: dict) -> bytes:
    """Калдата updateDataFeedsValuesPartial для плана. require_signers=False: пакеты уже
    проверены в _validated, а строгий режим схлопнул бы мульти-фидовый пуш (одни и те же
    пять подписантов на каждом фиде; порог пер-фид адаптер считает сам)."""
    return RS.build_push_calldata(p["feeds"], p["pkgs"], require_signers=False)
