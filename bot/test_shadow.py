"""Offline unit tests for the shadow-race notify gate (no network). Run:
PYTHONPATH=. python3 bot/test_shadow.py

Covers the 2026-08-07 triage fix (block 42516651): the inbox wake-up threshold is the FULL
fire floor (MIN_DEBT_USD AND MIN_PROFIT_USD — dd19a4e canon), not MIN_PROFIT alone. A $363-debt
race cleared the $25 profit floor and woke the agent for a position the bot deliberately
excludes via HL_MIN_DEBT_USD=500 — sub-filter races stay in the jsonl but must not wake.
Also covers the per-tx oracle-update counter (Pyth PriceFeedUpdate / RS ValueUpdate): the
42516651 winner carried the Pyth price update INSIDE its own liquidation tx (atomic self-push).
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from bot import config as C          # noqa: E402
from bot import shadow as S          # noqa: E402


def test_fire_floor_gate() -> None:
    lo_debt = C.MIN_DEBT_USD - 1
    hi_debt = C.MIN_DEBT_USD + 1
    lo_bonus = C.MIN_PROFIT_USD - 1
    hi_bonus = C.MIN_PROFIT_USD + 1
    # оба порога взяты — будим
    assert S._above_fire_floor(hi_debt, hi_bonus)
    # ровно на порогах — будим (>=)
    assert S._above_fire_floor(C.MIN_DEBT_USD, C.MIN_PROFIT_USD)
    # прецедент 42516651: бонус выше пола, долг ниже размерного фильтра — НЕ будим
    if C.MIN_DEBT_USD > 363.08:
        assert not S._above_fire_floor(363.08, 26.14)
    assert not S._above_fire_floor(lo_debt, hi_bonus)
    # долг взят, бонус мал — НЕ будим
    assert not S._above_fire_floor(hi_debt, lo_bonus)
    # цена не добыта (None, как в записях 04.08 по USDHL до фикса символов) — НЕ будим
    assert not S._above_fire_floor(None, hi_bonus)
    assert not S._above_fire_floor(hi_debt, None)
    assert not S._above_fire_floor(None, None)
    # нулевые значения не проходят bool-гейт
    assert not S._above_fire_floor(0.0, hi_bonus)


def test_oracle_update_topics() -> None:
    # константа Pyth сверена вычислением keccak по сигнатуре события
    from analysis.keccak import event_topic0
    assert S.TOPIC_PYTH_PRICE_UPDATE == event_topic0(
        "PriceFeedUpdate(bytes32,uint64,int64,uint64)")
    assert C.TOPIC_VALUE_UPDATE in S._TOPIC_ORACLE_UPDATES
    assert S.TOPIC_PYTH_PRICE_UPDATE in S._TOPIC_ORACLE_UPDATES


def test_upd_counting_shape() -> None:
    """Счётчик upd — та же свёртка, что в _enrich: топик[0] лога в множестве апдейтов."""
    logs = [{"topics": [S.TOPIC_PYTH_PRICE_UPDATE]},
            {"topics": [C.TOPIC_VALUE_UPDATE]},
            {"topics": ["0x" + "ab" * 32]},
            {"topics": []}]
    n = sum(1 for lg in logs if lg["topics"] and lg["topics"][0] in S._TOPIC_ORACLE_UPDATES)
    assert n == 2


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok {t.__name__}")
    print(f"{len(tests)}/{len(tests)} shadow tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
