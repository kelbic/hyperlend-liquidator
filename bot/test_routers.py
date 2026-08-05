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


def test_pt_goes_to_pendle_and_never_asks_liquidswap(monkeypatch):
    seen = {"liqd": 0}

    def spy(*a, **k):
        seen["liqd"] += 1
        raise AssertionError("PT не должен уходить в LiquidSwap")

    monkeypatch.setattr(liqd, "quote_for_seized", spy)
    monkeypatch.setattr(R, "quote_pendle",
                        lambda rpc, ti, to, amt, **k: R._norm(amt, 0.0006, R.PENDLE_ROUTER,
                                                              "0xpendle", amt, "pendle"))
    q = R.quote_for_seized_multi(FakeRpc(), PT, WHYPE, 10 ** 20, 18, 18)
    assert q["venue"] == "pendle" and q["swap_target"] == R.PENDLE_ROUTER
    assert seen["liqd"] == 0


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
