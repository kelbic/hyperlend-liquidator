"""Offline unit tests for the pure Aave-v3 sizing math (no network). Run: python3 -m pytest -q
or python3 analysis/test_aave.py."""
from __future__ import annotations

from analysis.aave import close_factor, decode_user_account_data, size_liquidation
from analysis.protocols import ORACLE_BASE_UNIT


def test_close_factor_small_debt_full_close():
    # debt < $2000 -> 100% regardless of HF
    assert close_factor(1500 * ORACLE_BASE_UNIT, int(0.99e18)) == 1.0


def test_close_factor_deep_underwater_full_close():
    # HF < 0.95 -> 100%
    assert close_factor(50000 * ORACLE_BASE_UNIT, int(0.90e18)) == 1.0


def test_close_factor_shallow_half():
    # big debt, HF in (0.95, 1.0) -> 50%
    assert close_factor(50000 * ORACLE_BASE_UNIT, int(0.98e18)) == 0.5


def test_size_liquidation_stable_debt_hype_collateral():
    # borrower: 200 WHYPE collateral @ $64.60, 5000 USDT0 debt @ $1.00, 10% bonus, 50% close.
    # WHYPE 18dec price 6460044434 (1e8); USDT0 6dec price 1e8.
    coll_wei = 200 * 10 ** 18
    debt_wei = 5000 * 10 ** 6
    s = size_liquidation(debt_wei, 6, 100_000_000, coll_wei, 18, 6_460_044_434, 11000, 0.5)
    # 50% of $5000 debt = $2500; collateral value = 200*64.6 = $12,920 -> not the binding cap.
    assert s["debt_to_cover"] > 0 and s["seized"] > 0
    # repaid ~ $2500 (minus 0.5% safety), seized value ~ repaid * 1.10
    assert 2450 < s["repaid_usd"] < 2500
    assert 0.09 < (s["seized_usd"] / s["repaid_usd"] - 1.0) < 0.11  # ~10% bonus realized in value


def test_size_liquidation_collateral_constrained():
    # tiny collateral relative to debt -> debtToCover capped by collateral, not close factor.
    coll_wei = 1 * 10 ** 18            # 1 WHYPE ~ $64.60 collateral
    debt_wei = 100000 * 10 ** 6        # $100k USDT0 debt
    s = size_liquidation(debt_wei, 6, 100_000_000, coll_wei, 18, 6_460_044_434, 11000, 0.5)
    # collateral (incl. bonus) caps repay to ~ $64.60 / 1.10 ~ $58.7
    assert s["repaid_usd"] < 60
    assert s["seized"] <= coll_wei     # never seize more than exists


def test_size_liquidation_zero_on_bad_inputs():
    s = size_liquidation(0, 6, 1, 1, 18, 1, 11000, 0.5)
    assert s["debt_to_cover"] == 0 and s["seized"] == 0


def test_decode_user_account_data():
    # 6 words: coll, debt, avail, liqThreshold, ltv, hf
    words = [325041_00000000, 274777_00000000, 0, 7520, 6000, 1029100000000000000]
    hexs = "0x" + "".join(w.to_bytes(32, "big").hex() for w in words)
    d = decode_user_account_data(hexs)
    assert d["total_collateral_base"] == 325041_00000000
    assert d["health_factor"] == 1029100000000000000


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
