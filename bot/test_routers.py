"""Выбор площадки выхода: кто умеет — тот и котирует.

Замеры 05.08, на которых стоит логика:
  * PT-kHYPE обеих серий LiquidSwap не знает вовсе (HTTP 404 на любом размере от $1k);
  * beHYPE LiquidSwap отдаёт «успех» и КОНСТАНТУ 1.690 WHYPE на любой вход (impact ровно
    90%) — отравленная котировка, а не отказ: её нельзя отличить от честной по коду возврата;
  * Pendle закрывает PT (impact 0.058% даже на $1M), Kyber — beHYPE (обрыв ~$59k).

Тесты офлайн: сетевые функции подменяются, проверяется ИМЕННО маршрутизация.
Запуск: PYTHONPATH=. python3 -m pytest bot/test_routers.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HL_CONTRACT", "0x" + "22" * 20)

from bot import config as C          # noqa: E402
from bot import liqd                 # noqa: E402
from bot import routers as R         # noqa: E402

PT = "0x50fC4EDC6346F36993Bb30Fe60E932504Ed17391"
BEHYPE = "0xd8FC8F0b03eBA61F64D08B0bef69d80916E5DdA9"
KHYPE = "0xfD739d4e423301CE9385c1fb8850539D657C296D"
WHYPE = "0x5555555555555555555555555555555555555555"


class FakeRpc:
    """isExpired(): 0 = живой PT."""

    def __init__(self, expired=False):
        self.expired = expired
        self.calls = 0

    def eth_call(self, to, data, *a, **k):
        self.calls += 1
        return "0x" + ("01" if self.expired else "00").rjust(64, "0")


def _liqd_ok(out_wei):
    def f(coll, debt, seized, cd, dd):
        return {"amount_out": out_wei, "price_impact": 0.001, "swap_target": "0xliqd",
                "swap_calldata": "0xaa", "amount_in_used": seized}
    return f


def _liqd_garbage(coll, debt, seized, cd, dd):
    """Точный слепок поведения на beHYPE: успех, константа, импакт 90%."""
    return {"amount_out": int(1.69e18), "price_impact": 0.90, "swap_target": "0xliqd",
            "swap_calldata": "0xaa", "amount_in_used": seized}


def _liqd_no_route(coll, debt, seized, cd, dd):
    raise liqd.NoRouteError("no route")


def test_pt_exits_in_two_legs_and_never_asks_liquidswap_for_the_pt(monkeypatch):
    """PT продаётся Pendle в underlying, и только потом underlying — в долговой актив.

    Одной ногой не выходит: прямой PT->WHYPE у Pendle тянет вложенный своп агрегатора и
    падает по out of gas (форк 05.08: SwapFailed при 2.5M и при 2.9M, залога хватало).
    """
    def spy(*a, **k):
        raise AssertionError("PT не должен уходить в LiquidSwap первой ногой")

    monkeypatch.setattr(liqd, "quote_for_seized", spy)
    seen = {}

    def fake_pendle(rpc, ti, to, amt, **k):
        seen["aggregator"] = k.get("aggregator")
        seen["token_out"] = to
        return R._norm(int(amt * 0.98), 0.0006, R.PENDLE_ROUTER, "0xpendle", amt, "pendle")

    monkeypatch.setattr(R, "quote_pendle", fake_pendle)
    monkeypatch.setattr(liqd, "quote",
                        lambda ti, to, amt, dl, dr, **k: {"amount_out": int(amt * 1.01),
                                                          "price_impact": 0.002,
                                                          "swap_target": "0xliqd",
                                                          "swap_calldata": "0xbb",
                                                          "amount_in_used": amt})
    q = R.quote_for_seized_multi(FakeRpc(), PT, WHYPE, 10 ** 20, 18, 18)
    assert q["venue"] == "pendle+liqd"
    assert q["swap_target"] == R.PENDLE_ROUTER and q["swap_target2"] == "0xliqd"
    assert q["mid_asset"].lower() == KHYPE.lower(), "промежуточный актив — underlying PT"
    assert seen["aggregator"] is False, "первая нога обязана идти БЕЗ агрегатора (газ)"
    assert seen["token_out"].lower() == KHYPE.lower()


def test_two_leg_impact_is_summed_so_the_gate_sees_the_whole_path(monkeypatch):
    """Порог MAX_IMPACT должен видеть просадку ОБЕИХ ног, иначе путь пролезет по половине."""
    monkeypatch.setattr(R, "quote_pendle",
                        lambda rpc, ti, to, amt, **k: R._norm(amt, 0.02, R.PENDLE_ROUTER,
                                                              "0xa", amt, "pendle"))
    monkeypatch.setattr(liqd, "quote",
                        lambda ti, to, amt, dl, dr, **k: {"amount_out": amt, "price_impact": 0.03,
                                                          "swap_target": "0xliqd",
                                                          "swap_calldata": "0xbb",
                                                          "amount_in_used": amt})
    q = R.quote_for_seized_multi(FakeRpc(), PT, WHYPE, 10 ** 20, 18, 18)
    assert abs(q["price_impact"] - 0.05) < 1e-9


def test_pt_with_debt_equal_to_underlying_stays_single_leg(monkeypatch):
    """Если долг И ЕСТЬ underlying, второй хоп не нужен — лишняя нога это лишний газ и риск."""
    monkeypatch.setattr(R, "quote_pendle",
                        lambda rpc, ti, to, amt, **k: R._norm(amt, 0.001, R.PENDLE_ROUTER,
                                                              "0xa", amt, "pendle"))

    def boom(*a, **k):
        raise AssertionError("вторая нога не нужна при долге в underlying")

    monkeypatch.setattr(liqd, "quote", boom)
    q = R.quote_for_seized_multi(FakeRpc(), PT, KHYPE, 10 ** 20, 18, 18)
    assert not q.get("swap_target2")


def test_expired_pt_uses_redeem_branch(monkeypatch):
    monkeypatch.setattr(C, "CONTRACT", "0x" + "22" * 20)   # конфиг мог быть импортирован раньше
    urls = []
    monkeypatch.setattr(R, "_get_json", lambda url, tmo: (
        urls.append(url) or {"tx": {"to": R.PENDLE_ROUTER, "data": "0xbb"},
                             "data": {"amountOut": "123", "priceImpact": 0.001}}))
    R._expiry_cache.clear()
    R.quote_pendle(FakeRpc(expired=True), R.PT_EXPIRED_SAMPLE, WHYPE, 10 ** 18)
    assert "/redeem?" in urls[0] and "yt=" in urls[0]


def test_live_pt_uses_market_swap_branch(monkeypatch):
    monkeypatch.setattr(C, "CONTRACT", "0x" + "22" * 20)
    urls = []
    monkeypatch.setattr(R, "_get_json", lambda url, tmo: (
        urls.append(url) or {"tx": {"to": R.PENDLE_ROUTER, "data": "0xbb"},
                             "data": {"amountOut": "123", "priceImpact": 0.001}}))
    R._expiry_cache.clear()
    R.quote_pendle(FakeRpc(expired=False), PT, WHYPE, 10 ** 18)
    assert "/markets/" in urls[0] and "/swap?" in urls[0]


def test_expiry_is_read_from_chain_not_from_a_hardcoded_date(monkeypatch):
    """Дата maturity в константе протухла бы молча — ветку выбирает контракт."""
    R._expiry_cache.clear()
    rpc = FakeRpc(expired=True)
    assert R.pt_expired(rpc, PT) is True
    assert rpc.calls == 1
    assert R.pt_expired(rpc, PT) is True          # второй раз — из кэша, без сети
    assert rpc.calls == 1


def test_garbage_quote_falls_back_to_kyber(monkeypatch):
    monkeypatch.setattr(liqd, "quote_for_seized", _liqd_garbage)
    monkeypatch.setattr(R, "quote_kyber",
                        lambda ti, to, amt, **k: R._norm(int(499e18), 0.019, "0xkyber",
                                                         "0xcc", amt, "kyber"))
    q = R.quote_for_seized_multi(FakeRpc(), BEHYPE, WHYPE, 500 * 10 ** 18, 18, 18)
    assert q["venue"] == "kyber" and q["amount_out"] == int(499e18)


def test_no_route_falls_back_to_kyber(monkeypatch):
    monkeypatch.setattr(liqd, "quote_for_seized", _liqd_no_route)
    monkeypatch.setattr(R, "quote_kyber",
                        lambda ti, to, amt, **k: R._norm(42, 0.01, "0xkyber", "0xcc", amt, "kyber"))
    assert R.quote_for_seized_multi(FakeRpc(), BEHYPE, WHYPE, 10 ** 18, 18, 18)["venue"] == "kyber"


def test_healthy_liqd_quote_is_kept_and_kyber_not_called(monkeypatch):
    monkeypatch.setattr(liqd, "quote_for_seized", _liqd_ok(10 ** 18))

    def boom(*a, **k):
        raise AssertionError("Kyber не должен вызываться при здоровой котировке")

    monkeypatch.setattr(R, "quote_kyber", boom)
    q = R.quote_for_seized_multi(FakeRpc(), KHYPE, WHYPE, 10 ** 18, 18, 18)
    assert q["venue"] == "liqd"


def test_both_venues_dead_propagates_no_route(monkeypatch):
    monkeypatch.setattr(liqd, "quote_for_seized", _liqd_no_route)

    def dead(*a, **k):
        raise liqd.NoRouteError("kyber тоже не знает")

    monkeypatch.setattr(R, "quote_kyber", dead)
    with pytest.raises(liqd.NoRouteError):
        R.quote_for_seized_multi(FakeRpc(), BEHYPE, WHYPE, 10 ** 18, 18, 18)


def test_rollback_switch_keeps_only_liquidswap(monkeypatch):
    monkeypatch.setattr(R, "ROUTERS_ENABLED", False)
    monkeypatch.setattr(R, "KYBER_FALLBACK", False)
    monkeypatch.setattr(liqd, "quote_for_seized", _liqd_garbage)

    def boom(*a, **k):
        raise AssertionError("при откате площадки не подключаются")

    monkeypatch.setattr(R, "quote_kyber", boom)
    q = R.quote_for_seized_multi(FakeRpc(), BEHYPE, WHYPE, 10 ** 18, 18, 18)
    assert q["venue"] == "liqd"          # мусор пропущен наверх — его отклонит порог импакта
    assert q["price_impact"] >= C.MAX_IMPACT


# --- потолок глубины ---------------------------------------------------------------------
# Выше обрыва квотер ВРЁТ: на $1.15M kHYPE->WHYPE он вернул impact −0.03% («лучше оракула»),
# а своп ревертнул в самом пуле (0xd93c0665, форк-экзамен 05.08). Порог импакта такую
# котировку не ловит по построению — режем по замеру.

def test_size_beyond_measured_depth_is_refused_so_the_ladder_goes_smaller(monkeypatch):
    monkeypatch.setattr(liqd, "quote_for_seized",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("не должны спрашивать")))
    with pytest.raises(liqd.NoRouteError):
        R.quote_for_seized_multi(FakeRpc(), KHYPE, WHYPE, 10 ** 22, 18, 18,
                                 usd_hint=1_150_000.0)


def test_size_within_depth_passes_through(monkeypatch):
    monkeypatch.setattr(liqd, "quote_for_seized", _liqd_ok(10 ** 18))
    q = R.quote_for_seized_multi(FakeRpc(), KHYPE, WHYPE, 10 ** 20, 18, 18, usd_hint=400_000.0)
    assert q["venue"] == "liqd"


def test_two_leg_depth_is_checked_on_the_underlying_not_the_pt(monkeypatch):
    """У PT узкое место — вторая нога: продаётся kHYPE, а глубины PT у Pendle с запасом."""
    monkeypatch.setattr(R, "quote_pendle",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("до Pendle не дойдёт")))
    with pytest.raises(liqd.NoRouteError):
        R.quote_for_seized_multi(FakeRpc(), PT, WHYPE, 10 ** 22, 18, 18, usd_hint=1_150_000.0)


def test_no_hint_means_no_cap(monkeypatch):
    """Без подсказки о размере режем только по квотеру — отсутствие цены не смеет ломать путь."""
    monkeypatch.setattr(liqd, "quote_for_seized", _liqd_ok(10 ** 18))
    assert R.quote_for_seized_multi(FakeRpc(), KHYPE, WHYPE, 10 ** 24, 18, 18)["venue"] == "liqd"
