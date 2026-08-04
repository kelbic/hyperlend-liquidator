"""Тесты spec-fire (04.08): валидация подписанных пакетов, математика plan() (HF при подмене
цен драйверов), гейты свежести/новизны и сборка мульти-фидовой пуш-калдаты. Сеть не трогается
нигде: пакеты подписываются тестовыми ключами, кэши модуля заполняются напрямую.

Запуск: PYTHONPATH=. python3 -m pytest bot/test_spec.py -q
"""
from __future__ import annotations

import base64
import time

import pytest

import analysis.redstone as RS
from analysis.keccak import keccak
from analysis.protocols import TOKENS
from bot import config as C
from bot import executor as ex
from bot import spec

from eth_account import Account

# Три тестовых подписанта — на время теста они и есть «авторизованный набор».
_KEYS = [Account.from_key("0x" + f"{i:02x}" * 32) for i in (0x11, 0x22, 0x33)]
_SIGNERS = {a.address.lower() for a in _KEYS}

_WHYPE = TOKENS["WHYPE"]["address"].lower()
_WSTHYPE = TOKENS["wstHYPE"]["address"].lower()
_USDT0 = TOKENS["USDT0"]["address"].lower()
_KHYPE = TOKENS["kHYPE"]["address"].lower()
_A = spec.ADAPTER_A


def _mk_pkg(feed: str, value: float, ts_ms: int, acct) -> dict:
    """Пакет в формате гейтвея, подписанный тестовым ключом ровно по проволочному формату
    _serialize_package (это и есть кросс-проверка сериализации: подпись обязана
    восстановиться из НАШЕЙ сборки тела)."""
    body = (feed.encode().ljust(32, b"\0")
            + int(round(value * 1e8)).to_bytes(32, "big")
            + int(ts_ms).to_bytes(6, "big")
            + (32).to_bytes(4, "big")
            + (1).to_bytes(3, "big"))
    sig = Account._sign_hash(keccak(body), acct.key).signature
    return {"timestampMilliseconds": ts_ms,
            "signature": base64.b64encode(bytes(sig)).decode(),
            "dataPoints": [{"dataFeedId": feed, "value": value}]}


@pytest.fixture()
def signers(monkeypatch):
    monkeypatch.setattr(RS, "AUTHORISED_SIGNERS", _SIGNERS)
    spec._sig_seen.clear()
    yield
    spec._sig_seen.clear()


@pytest.fixture()
def caches(signers):
    """Чистые кэши spec с тёплыми метками; тест наполняет _gw/_chain сам."""
    with spec._lock:
        spec._gw.clear()
        spec._chain.clear()
    spec._meta["gw_mono"] = spec._meta["chain_mono"] = time.monotonic()
    yield
    with spec._lock:
        spec._gw.clear()
        spec._chain.clear()


def _feed_cache(feed: str, price: float, ts_ms: int) -> dict:
    return {"pkgs": [_mk_pkg(feed, price, ts_ms, k) for k in _KEYS],
            "price": price, "ts_ms": ts_ms}


def _acct(hf: float, tc: int = 10_000 * 10 ** 8, td: int = 8_000 * 10 ** 8) -> dict:
    return {"health_factor": int(hf * 1e18), "total_collateral_base": tc,
            "total_debt_base": td}


def _t(coll: str, debt: str, tc: int = 10_000 * 10 ** 8, td: int = 8_000 * 10 ** 8) -> dict:
    """Пара (coll, debt), полностью покрывающая позицию (f_c = f_d = 1) при ценах $1e8/юнит."""
    return {"coll_asset": coll, "debt_asset": debt,
            "coll_wei": tc, "coll_price": 10 ** 8, "coll_dec": 8,
            "debt_wei": td, "debt_price": 10 ** 8, "debt_dec": 8}


# --------------------------------------------------------------------------- _validated
def test_validated_needs_three_unique_signers(signers):
    ts = int(time.time() * 1000)
    pkgs = [_mk_pkg("HYPE", 50.0, ts, k) for k in _KEYS]
    got = spec._validated("HYPE", pkgs)
    assert got is not None
    assert len(got[0]) == 3 and got[1] == 50.0 and got[2] == ts
    # два подписанта — ниже порога адаптера, пакеты непушабельны
    assert spec._validated("HYPE", pkgs[:2]) is None
    # дубль подписанта не считается вторым голосом
    assert spec._validated("HYPE", [pkgs[0], pkgs[0], pkgs[0]]) is None


def test_validated_rejects_foreign_signer(signers):
    ts = int(time.time() * 1000)
    alien = Account.from_key("0x" + "aa" * 32)
    pkgs = [_mk_pkg("HYPE", 50.0, ts, k) for k in _KEYS[:2]] + [_mk_pkg("HYPE", 50.0, ts, alien)]
    assert spec._validated("HYPE", pkgs) is None, "чужая подпись не добирает порог"


def test_validated_median_price(signers):
    ts = int(time.time() * 1000)
    pkgs = [_mk_pkg("HYPE", v, ts, k) for v, k in zip((49.0, 50.0, 51.0), _KEYS)]
    got = spec._validated("HYPE", pkgs)
    assert got is not None and got[1] == 50.0


# --------------------------------------------------------------------------- plan()
def test_plan_ignores_feed_below_adapter_deviation_threshold(caches):
    """04.08 форк-канарейка: адаптер принимает апдейт только при |Δцены| >= 0.5% (замер по
    истории: 0.501/0.504/0.507/0.508%, ничего ниже). Пуш ниже порога — МОЛЧАЛИВЫЙ no-op
    (status=1, цена на месте), значит выстрел по нему ревертится по HF. Такой фид не смеет
    ни попадать в пуш, ни давать масштаб."""
    now_ms = int(time.time() * 1000)
    with spec._lock:
        spec._gw["HYPE"] = _feed_cache("HYPE", 50.0 * 0.996, now_ms - 5_000)   # −0.4%: мимо порога
        spec._chain[(_A, "HYPE")] = {"value": int(50.0 * 1e8), "ts_ms": now_ms - 240_000}
    assert spec.plan(_acct(1.001), _t(_WHYPE, _USDT0)) is None
    # тот же расклад, но движение выше порога — план появляется
    with spec._lock:
        spec._gw["HYPE"] = _feed_cache("HYPE", 50.0 * 0.99, now_ms - 5_000)    # −1%
    p = spec.plan(_acct(1.001), _t(_WHYPE, _USDT0))
    assert p is not None and p["feeds"] == ["HYPE"]


def test_plan_fires_on_collateral_price_drop(caches):
    """WHYPE-залог падает на 3% по свежему пакету: HF 1.01 -> est ~0.98 < порога, пуш HYPE@A."""
    now_ms = int(time.time() * 1000)
    with spec._lock:
        spec._gw["HYPE"] = _feed_cache("HYPE", 48.5, now_ms - 5_000)
        # USDT-пакет протух: такой фид не пушится и корректно даёт масштаб 1 (его on-chain
        # цену наша транзакция не изменит) — оценке по HYPE он мешать не смеет
        spec._gw["USDT"] = _feed_cache("USDT", 1.0, now_ms - 900_000)
        spec._chain[(_A, "HYPE")] = {"value": int(50.0 * 1e8), "ts_ms": now_ms - 240_000}
        spec._chain[(_A, "USDT")] = {"value": int(1.0 * 1e8), "ts_ms": now_ms - 60_000}
    p = spec.plan(_acct(1.01), _t(_WHYPE, _USDT0))
    assert p is not None, "3% падение обязано армить план"
    assert p["adapter"] == _A and p["feeds"] == ["HYPE"]
    assert p["hf_est"] == pytest.approx(1.01 * 0.97, rel=1e-6)
    assert len(p["pkgs"]) == 3


def test_plan_same_asset_self_cancels(caches):
    """coll==debt: цена сокращается, HF_est == HF >= 1 — spec по такой цели не стреляет."""
    now_ms = int(time.time() * 1000)
    with spec._lock:
        spec._gw["HYPE"] = _feed_cache("HYPE", 40.0, now_ms - 5_000)    # −20%!
        spec._chain[(_A, "HYPE")] = {"value": int(50.0 * 1e8), "ts_ms": now_ms - 240_000}
    assert spec.plan(_acct(1.01), _t(_WHYPE, _WHYPE)) is None


def test_plan_wsthype_composes_two_feeds(caches):
    """wstHYPE = wstHYPE_FUNDAMENTAL * HYPE (доказано 04.08): двигаются оба фида — масштабы
    перемножаются, и оба входят в пуш-набор."""
    now_ms = int(time.time() * 1000)
    with spec._lock:
        spec._gw["HYPE"] = _feed_cache("HYPE", 48.5, now_ms - 5_000)                  # ×0.97
        spec._gw["wstHYPE_FUNDAMENTAL"] = _feed_cache("wstHYPE_FUNDAMENTAL", 1.02,
                                                      now_ms - 5_000)                # ×0.99…
        spec._gw["USDT"] = _feed_cache("USDT", 1.0, now_ms - 5_000)
        spec._chain[(_A, "HYPE")] = {"value": int(50.0 * 1e8), "ts_ms": now_ms - 240_000}
        spec._chain[(_A, "wstHYPE_FUNDAMENTAL")] = {"value": int(1.03 * 1e8),
                                                    "ts_ms": now_ms - 240_000}
        spec._chain[(_A, "USDT")] = {"value": int(1.0 * 1e8), "ts_ms": now_ms - 240_000}
    p = spec.plan(_acct(1.01), _t(_WSTHYPE, _USDT0))
    assert p is not None
    # USDT не двигается вовсе (0%) — ниже порога адаптера, в пуш не кладётся
    assert sorted(p["feeds"]) == ["HYPE", "wstHYPE_FUNDAMENTAL"]
    est = 1.01 * (48.5 / 50.0) * (1.02 / 1.03)
    assert p["hf_est"] == pytest.approx(est, rel=1e-6)
    assert len(p["pkgs"]) == 6


def test_plan_unmapped_asset_is_none(caches):
    """kHYPE вне доказанной карты — spec не оценивает и не стреляет (реактивный путь)."""
    now_ms = int(time.time() * 1000)
    with spec._lock:
        spec._gw["HYPE"] = _feed_cache("HYPE", 40.0, now_ms - 5_000)
        spec._chain[(_A, "HYPE")] = {"value": int(50.0 * 1e8), "ts_ms": now_ms - 240_000}
    assert spec.plan(_acct(1.01), _t(_KHYPE, _USDT0)) is None


def test_plan_stale_gateway_package_is_none(caches):
    """Пакет старше SPEC_MAX_AGE_MS — опоры нет (драйвер без свежей цены => план невозможен)."""
    now_ms = int(time.time() * 1000)
    with spec._lock:
        spec._gw["HYPE"] = _feed_cache("HYPE", 48.5, now_ms - C.SPEC_MAX_AGE_MS - 1_000)
        spec._chain[(_A, "HYPE")] = {"value": int(50.0 * 1e8),
                                     "ts_ms": now_ms - C.SPEC_MAX_AGE_MS - 500_000}
    assert spec.plan(_acct(1.01), _t(_WHYPE, _USDT0)) is None


def test_plan_chain_fresher_than_package_is_none(caches):
    """Релейер уже запушил не хуже нашего: пушить нечего (feeds пуст) — плана нет. Это гейт
    от выстрела «пуш отвергнут адаптером как не-новее => цена не изменилась => HF-реверт»."""
    now_ms = int(time.time() * 1000)
    with spec._lock:
        spec._gw["HYPE"] = _feed_cache("HYPE", 48.5, now_ms - 5_000)
        spec._chain[(_A, "HYPE")] = {"value": int(48.5 * 1e8), "ts_ms": now_ms - 1_000}
    assert spec.plan(_acct(1.01), _t(_WHYPE, _USDT0)) is None


def test_plan_cold_cache_is_none(caches):
    now_ms = int(time.time() * 1000)
    with spec._lock:
        spec._gw["HYPE"] = _feed_cache("HYPE", 48.5, now_ms - 5_000)
        spec._chain[(_A, "HYPE")] = {"value": int(50.0 * 1e8), "ts_ms": now_ms - 240_000}
    spec._meta["chain_mono"] = time.monotonic() - 60    # chain-кэш остыл (вотчер умер?)
    assert spec.plan(_acct(1.01), _t(_WHYPE, _USDT0)) is None


def test_plan_partial_coverage_scales_fraction(caches):
    """Пара кроет половину позиции: непокрытый остаток неподвижен, эффект деления пополам."""
    now_ms = int(time.time() * 1000)
    with spec._lock:
        spec._gw["HYPE"] = _feed_cache("HYPE", 45.0, now_ms - 5_000)    # −10%
        spec._gw["USDT"] = _feed_cache("USDT", 1.0, now_ms - 900_000)
        spec._chain[(_A, "HYPE")] = {"value": int(50.0 * 1e8), "ts_ms": now_ms - 240_000}
        spec._chain[(_A, "USDT")] = {"value": int(1.0 * 1e8), "ts_ms": now_ms - 60_000}
    tc = 10_000 * 10 ** 8
    t = _t(_WHYPE, _USDT0, tc=tc)
    t["coll_wei"] = tc // 2                             # f_c = 0.5
    p = spec.plan(_acct(1.05), t)
    assert p is not None
    assert p["hf_est"] == pytest.approx(1.05 * (0.5 * (0.9 - 1) + 1), rel=1e-6)  # 0.9975


# --------------------------------------------------------------------------- push_calldata
def _parse_payload_tail(payload: bytes) -> tuple[int, bytes]:
    """(количество пакетов, MARKER) из хвоста RedStone-payload."""
    assert payload.endswith(RS.MARKER)
    body = payload[:-len(RS.MARKER)]
    meta_size = int.from_bytes(body[-3:], "big")
    body = body[:-3 - meta_size]
    return int.from_bytes(body[-2:], "big"), RS.MARKER


def test_push_calldata_multifeed_keeps_every_feeds_packages(caches):
    """Сердце require_signers=False: у HYPE и USDT одни и те же 3 подписанта. Строгий режим
    схлопнул бы пакеты второго фида (unique-набор считается на весь payload), и адаптер
    ревертнул бы пуш порогом. Лояльный режим обязан отдать все 6."""
    ts = int(time.time() * 1000)
    plan = {"feeds": ["HYPE", "USDT"],
            "pkgs": ([_mk_pkg("HYPE", 50.0, ts, k) for k in _KEYS]
                     + [_mk_pkg("USDT", 1.0, ts, k) for k in _KEYS])}
    cd = spec.push_calldata(plan)
    assert cd[:4] == bytes.fromhex(RS.SEL_UPDATE_PARTIAL[2:])
    # abi-голова: offset, len=2, два bytes32-идентификатора фидов
    assert int.from_bytes(cd[4 + 32:4 + 64], "big") == 2
    assert cd[4 + 64:4 + 96] == b"HYPE".ljust(32, b"\0")
    assert cd[4 + 96:4 + 128] == b"USDT".ljust(32, b"\0")
    n, _ = _parse_payload_tail(cd)
    assert n == 6, "мульти-фидовый пуш обязан нести пакеты ОБОИХ фидов"
    # контраст: строгий build_payload схлопывает по подписантам — второго фида в нём нет
    strict = RS.build_payload(plan["pkgs"], require_signers=True)
    ns, _ = _parse_payload_tail(strict)
    assert ns == 3, "строгий режим и есть та бага, от которой require_signers=False"


# --------------------------------------------------------------------------- _spec_pass (executor)
def _spec_env(monkeypatch, plan_ret):
    monkeypatch.setattr(C, "SPEC_FIRE", True)
    monkeypatch.setattr(C, "RAW_TX", True)
    monkeypatch.setattr(C, "CONTRACT", "0x" + "1" * 40)
    monkeypatch.setattr(C, "DRY_RUN", True)     # fire перехвачен; balance-гейт не дёргаем
    monkeypatch.setattr(spec, "plan", lambda acct, t: plan_ret)
    fired = []
    monkeypatch.setattr(
        ex, "fire",
        lambda t, ev, st, now, gas, push=None, spec_probe=False:
            fired.append((t, ev, push, spec_probe)))
    ex._spec_windows.clear()
    return fired


def _armed(borrower: str) -> None:
    ex._prearm[borrower] = {"t": {"borrower": borrower, "coll_asset": _WHYPE,
                                  "debt_asset": _USDT0},
                            "ev": {"net_usd": 42.0}, "ts": time.monotonic()}


_PLAN = {"adapter": _A, "feeds": ["HYPE"], "hf_est": 0.98, "age_ms": 4_000, "pkgs": []}


def test_spec_pass_probes_armed_edge_target_and_keeps_arm(monkeypatch):
    """Потребитель: окно открыто (план есть) — уходит ЗОНД (spec_probe, БЕЗ пуша), а арм
    ЖИВЁТ: окно кормится повторами зондов раз в блок до лендинга пуша релейера."""
    fired = _spec_env(monkeypatch, _PLAN)
    b = "0x" + "77" * 20
    _armed(b)
    try:
        n = ex._spec_pass({b: _acct(1.005)}, {"sent": {}}, time.time(), 0.01)
        assert n == 1 and len(fired) == 1
        t, ev, push, probe = fired[0]
        assert push is None, "потребитель не пушит — премисса self-push опровергнута форком"
        assert probe is True, "выстрел обязан уйти зондом (низкий tip, spec-учёт)"
        assert b in ex._prearm, "арм обязан пережить зонд — окно живёт повторами"
        assert ex._spec_windows[b]["probes"] == 1
    finally:
        ex._prearm.clear()
        ex._spec_windows.clear()


def test_spec_pass_caps_probes_per_window(monkeypatch):
    fired = _spec_env(monkeypatch, _PLAN)
    b = "0x" + "77" * 20
    _armed(b)
    try:
        for _ in range(C.SPEC_MAX_PROBES + 3):
            ex._spec_pass({b: _acct(1.005)}, {"sent": {}}, time.time(), 0.01)
        assert len(fired) == C.SPEC_MAX_PROBES, \
            "кап зондов на окно обязан держать плотность конечной"
    finally:
        ex._prearm.clear()
        ex._spec_windows.clear()


def test_spec_pass_window_reopens_after_gap(monkeypatch):
    """Пауза без плана дольше SPEC_WINDOW_GAP_S закрывает окно; следующее открывается со
    СВЕЖИМ капом — исчерпанное окно не смеет навсегда глушить цель."""
    fired = _spec_env(monkeypatch, _PLAN)
    b = "0x" + "77" * 20
    _armed(b)
    try:
        for _ in range(C.SPEC_MAX_PROBES + 3):
            ex._spec_pass({b: _acct(1.005)}, {"sent": {}}, time.time(), 0.01)
        assert len(fired) == C.SPEC_MAX_PROBES
        # окно отжило: сдвигаем last за порог паузы — просроченное окно закрывается
        ex._spec_windows[b]["last"] -= C.SPEC_WINDOW_GAP_S + 1
        ex._spec_pass({b: _acct(1.005)}, {"sent": {}}, time.time(), 0.01)
        assert len(fired) == C.SPEC_MAX_PROBES + 1, "новое окно — новый кап"
        assert ex._spec_windows[b]["probes"] == 1
    finally:
        ex._prearm.clear()
        ex._spec_windows.clear()


def test_spec_pass_leaves_live_targets_to_reactive_path(monkeypatch):
    """on-chain HF < 1 — пуш уже лёг, жертва живая: её берёт process_targets этой же итерации."""
    fired = _spec_env(monkeypatch, {**_PLAN, "hf_est": 0.9})
    b = "0x" + "77" * 20
    _armed(b)
    try:
        n = ex._spec_pass({b: _acct(0.99)}, {"sent": {}}, time.time(), 0.01)
    finally:
        ex._prearm.clear()
        ex._spec_windows.clear()
    assert n == 0 and not fired


def test_spec_pass_respects_reactive_dedup(monkeypatch):
    """Pending РЕАКТИВНЫЙ выстрел по borrower (ключ без #pN) гейтит зонды: двух ликвидаций
    одной цели не шлём. Собственные ключи зондов (borrower#pN) этот гейт не трогают."""
    fired = _spec_env(monkeypatch, _PLAN)
    b = "0x" + "77" * 20
    _armed(b)
    st = {"sent": {b: {"ts": time.time(), "status": "pending"}}}
    try:
        n = ex._spec_pass({b: _acct(1.005)}, st, time.time(), 0.01)
        assert n == 0 and not fired, "pending-выстрел держит цель до сеттла"
        # прошлый ЗОНД pending — НЕ гейт: ключ уникален, окно живёт повторами
        st2 = {"sent": {f"{b}#p1": {"ts": time.time(), "status": "pending", "spec": True}}}
        ex._spec_windows.clear()
        n2 = ex._spec_pass({b: _acct(1.005)}, st2, time.time(), 0.01)
        assert n2 == 1, "зонд в полёте не смеет глушить следующий зонд"
    finally:
        ex._prearm.clear()
        ex._spec_windows.clear()


def test_spec_pass_gated_off(monkeypatch):
    fired = _spec_env(monkeypatch, _PLAN)
    monkeypatch.setattr(C, "SPEC_FIRE", False)
    b = "0x" + "77" * 20
    _armed(b)
    try:
        n = ex._spec_pass({b: _acct(1.005)}, {"sent": {}}, time.time(), 0.01)
    finally:
        ex._prearm.clear()
        ex._spec_windows.clear()
    assert n == 0 and not fired, "HL_SPEC_FIRE=0 обязан быть полным откатом spec-пути"
