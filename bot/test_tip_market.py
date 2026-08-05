"""Политика чаевых: рынок — драйвер, доля приза — потолок.

Замер 05.08 (21 ликвидация HyperLend за 6 суток + 19 блоков целиком):
  * порядок внутри блока по tip подтверждён — 398 пар «раньше = больше чаевых» против 93;
  * верх блока стоит медиану 2.0 gwei (p75 = 5.0, максимум за окно 20.02);
  * победители ликвидаций платили 0.0–5.01 gwei.
Прежняя формула, где ДРАЙВЕРОМ была доля приза, на ручейке назначала абсурд: приз $200 ->
115 gwei (в 5.7 раза выше любого наблюдавшегося платежа), приз $1,000 -> 575 gwei (в 28 раз).
Тесты фиксируют оба свойства новой политики: не переплачивать в штиль и не терять каскад.

Запуск: PYTHONPATH=. python3 -m pytest bot/test_tip_market.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("HL_CONTRACT", "0x" + "11" * 20)

from bot import config as C          # noqa: E402
from bot import executor as E        # noqa: E402


def _set_market(gwei: float):
    E._market_tip_cache["gwei"] = gwei
    E._market_tip_cache["ts"] = 1e18          # свежий кэш: рефреш не полезет в сеть


def tip_gwei(net_usd: float, market: float) -> float:
    _set_market(market)
    return E._tip_wei(net_usd, {"balance_hype": 10.0}) / 1e9


def test_calm_market_pays_floor_not_prize_fraction():
    """Штиль (верх блока ~0.1 gwei) + мелкий приз: платим пол, а не 4.7% приза."""
    assert tip_gwei(200.0, market=0.11) == C.TIP_MIN_GWEI


def test_old_formula_would_have_overpaid():
    """Контроль: прежняя политика на том же призе назначила бы кратно больше рынка."""
    old = min(C.TIP_MAX_GWEI, max(C.TIP_MIN_GWEI,
                                  200.0 * C.TIP_PRIZE_FRAC / (C.HYPE_USD * C.GAS_UNITS_EST * 1e-9)))
    assert old > 100                                   # ~115 gwei
    assert tip_gwei(200.0, market=0.11) < old / 20     # новая политика в 20+ раз дешевле


def test_market_drives_the_bid_up():
    """Рынок ожил (верх блока 2 gwei): на среднем призе ставим кратно рынку."""
    t = tip_gwei(2_000.0, market=2.0)
    assert t == 2.0 * C.TIP_MARKET_MULT


def test_big_prize_pays_ceiling_without_asking_the_market():
    """Крупный приз: метрика рынка ЗАПАЗДЫВАЕТ (медиана 20 блоков), а $80 чаевых против
    риска потерять $190k — плохая сделка. Платим потолок бюджета сразу."""
    assert tip_gwei(190_000.0, market=0.11) == C.TIP_MAX_GWEI
    assert tip_gwei(190_000.0, market=400.0) == C.TIP_MAX_GWEI


def test_market_still_drives_mid_prizes():
    """Ниже порога крупного приза рынок остаётся драйвером."""
    assert C.TIP_BIG_PRIZE_USD > 1000
    assert tip_gwei(1000.0, market=2.0) == 2.0 * C.TIP_MARKET_MULT


def test_prize_ceiling_binds_on_small_target_in_a_storm():
    """Шторм при мелком призе: рынок хочет много, но потолок приза не пускает переплату."""
    t = tip_gwei(50.0, market=400.0)
    ceiling = 50.0 * C.TIP_PRIZE_FRAC / (C.HYPE_USD * C.GAS_UNITS_EST * 1e-9)
    assert t <= max(C.TIP_MIN_GWEI, ceiling) + 1e-9
    assert t < 400.0 * C.TIP_MARKET_MULT


def test_never_below_floor_even_at_zero_market():
    """Пустой рынок (или несостоявшийся замер) не должен ронять нас в конец блока."""
    assert tip_gwei(2_000.0, market=0.0) == C.TIP_MIN_GWEI


def test_rollback_switch_restores_old_formula(monkeypatch):
    monkeypatch.setattr(C, "TIP_MARKET", False)
    t = tip_gwei(200.0, market=0.11)
    assert t > 100          # прежнее поведение вернулось целиком


def test_refresh_keeps_last_value_when_rpc_fails():
    """Отказ чтения оставляет прошлый замер: обнуление отправило бы нас в конец блока в шторм."""
    class Boom:
        def call(self, *a, **k):
            raise RuntimeError("rpc down")

    _set_market(7.5)
    E._market_tip_cache["ts"] = 0.0          # просрочить кэш, чтобы рефреш реально пошёл
    E.refresh_market_tip(Boom())
    assert E._market_tip_cache["gwei"] == 7.5
