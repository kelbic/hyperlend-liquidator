"""Offline unit tests for the pure Aave-v3 sizing math (no network). Run:
PYTHONPATH=. python3 analysis/test_aave.py

Pins the LiquidationLogic behaviour to the constants verified on-chain 2026-07-16 (deployed lib
0x8dc095f2…8104 + PoolDataProvider reads + 16 historical liquidations):
  * liquidationProtocolFee = 1000 bps of the BONUS -> received/repaid = 1.090 @ bonus 1.10,
    1.135 @ bonus 1.15 (the empirical anchors),
  * MIN_LEFTOVER_BASE = 1000e8 (MustNotLeaveDust), MIN_BASE_MAX_CLOSE_FACTOR_THRESHOLD = 2000e8,
  * CLOSE_FACTOR_HF_THRESHOLD = 0.95e18, DEFAULT_LIQUIDATION_CLOSE_FACTOR = 50% of TOTAL debt.
"""
from __future__ import annotations

from analysis.aave import (
    DUST_MARGIN, FULL_CLOSE_BUFFER, decode_user_account_data, liquidation_amounts,
    max_liquidatable_debt, percent_div, percent_mul, size_liquidation,
)
from analysis.protocols import (
    CLOSE_FACTOR_HF_THRESHOLD, LIQ_PROTOCOL_FEE_BPS, MAX_CF_THRESHOLD_USD, MIN_LEFTOVER_USD,
    ORACLE_BASE_UNIT,
)

USD = ORACLE_BASE_UNIT          # 1e8
P_USDT0 = 100_000_000           # $1.00, 6 dec
P_WHYPE = 6_460_044_434         # $64.60, 18 dec


def test_constants_pinned_to_chain():
    # ground truth read on-chain 2026-07-16 — if someone edits these, every sizing bet is off
    assert LIQ_PROTOCOL_FEE_BPS == 1000
    assert MIN_LEFTOVER_USD == 1000
    assert MAX_CF_THRESHOLD_USD == 2000
    assert CLOSE_FACTOR_HF_THRESHOLD == 0.95


def test_percent_math_matches_aave_rounding():
    # PercentageMath rounds half-up
    assert percent_mul(10_000, 5000) == 5_000
    assert percent_mul(1, 5000) == 1          # 0.5 -> 1 (half-up)
    assert percent_div(100 * 10 ** 18, 11000) * 11 // 10 == 100 * 10 ** 18


def test_protocol_fee_semantics_exact():
    # repay 100 USDT0 vs WHYPE at par prices, bonus 10%: gross = 110, bonus part = 10,
    # fee = 10% of 10 = 1, we receive 109 (NOT 110)
    amt = liquidation_amounts(100 * 10 ** 6, 6, 100_000_000,
                              10 ** 30, 18, 100_000_000, 11000, 1000)
    assert amt["gross"] == 110 * 10 ** 18
    assert amt["fee"] == 1 * 10 ** 18
    assert amt["received"] == 109 * 10 ** 18


def test_fee_adjusted_bonus_matches_empirical_110():
    # empirical anchor: received/repaid = 1.090 at bonus 1.10 (16 real liquidations)
    s = size_liquidation(1500 * 10 ** 6, 6, P_USDT0, 100 * 10 ** 18, 18, P_WHYPE,
                         11000, 1000, 1500 * USD, int(0.99e18))
    assert s["full_debt_close"]
    assert abs(s["seized_usd"] / s["repaid_usd"] - 1.090) < 0.0005
    assert abs(s["net_bonus_usd"] - 0.090 * s["repaid_usd"]) < 1.0


def test_fee_adjusted_bonus_matches_empirical_115():
    # empirical anchor: received/repaid = 1.135 at bonus 1.15
    s = size_liquidation(1500 * 10 ** 6, 6, P_USDT0, 100 * 10 ** 18, 18, P_WHYPE,
                         11500, 1000, 1500 * USD, int(0.99e18))
    assert abs(s["seized_usd"] / s["repaid_usd"] - 1.135) < 0.0005


def test_small_reserve_debt_full_close():
    # reserve debt < $2000 -> 100% closable regardless of HF; the tx param overshoots so the
    # Pool (which clamps) still closes the full leg after interest accrual
    debt_wei = 1500 * 10 ** 6
    s = size_liquidation(debt_wei, 6, P_USDT0, 100 * 10 ** 18, 18, P_WHYPE,
                         11000, 1000, 50_000 * USD, int(0.99e18))
    assert s["full_debt_close"] and s["close_factor"] == 1.0
    assert s["debt_pulled"] == debt_wei
    assert s["debt_to_cover"] == int(debt_wei * (1 + FULL_CLOSE_BUFFER))


def test_small_reserve_collateral_full_close():
    # reserve collateral < $2000 -> 100% closable even with big debt & healthy-ish HF; here the
    # collateral binds -> full collateral seize (dust-exempt), never a partial
    coll_wei = 20 * 10 ** 18                      # ~$1292 WHYPE
    s = size_liquidation(50_000 * 10 ** 6, 6, P_USDT0, coll_wei, 18, P_WHYPE,
                         11000, 1000, 50_000 * USD, int(0.99e18))
    assert s["full_coll_seize"] and s["close_factor"] == 1.0
    assert s["seized_gross"] == coll_wei


def test_deep_hf_full_close():
    # HF < 0.95 -> 100% closable even when both legs are large
    debt_wei = 50_000 * 10 ** 6
    s = size_liquidation(debt_wei, 6, P_USDT0, 2000 * 10 ** 18, 18, P_WHYPE,
                         11000, 1000, 50_000 * USD, int(0.90e18))
    assert s["full_debt_close"] and s["debt_pulled"] == debt_wei


def test_50_percent_close_factor_partial():
    # both legs >= $2000, HF in (0.95, 1) -> cover capped at 50% of TOTAL debt; leftovers are
    # far above $1000 so the dust rule doesn't bind
    debt_wei = 50_000 * 10 ** 6
    s = size_liquidation(debt_wei, 6, P_USDT0, 2000 * 10 ** 18, 18, P_WHYPE,
                         11000, 1000, 50_000 * USD, int(0.98e18))
    assert not s["full_debt_close"] and not s["full_coll_seize"]
    assert s["close_factor"] == 0.5
    assert 24_500 < s["repaid_usd"] <= 25_000               # ~50% (0.995 safety shave)
    assert s["debt_to_cover"] == s["debt_pulled"]            # partials never overshoot
    # on-chain dust check would pass: both leftovers >= $1000
    assert (debt_wei - s["debt_pulled"]) / 10 ** 6 >= 1000
    coll_left_usd = (2000 * 10 ** 18 - s["seized_gross"]) * P_WHYPE / 10 ** 18 / USD
    assert coll_left_usd >= 1000


def test_dust_guard_debt_leftover():
    # reserve debt $2050 (total $2050), HF 0.98: 50% cap = $1025, but that would leave $1025
    # ... < $1050 dust floor -> cover shrinks so the leftover stays >= 1000*(1+margin)
    debt_wei = 2050 * 10 ** 6
    s = size_liquidation(debt_wei, 6, P_USDT0, 100 * 10 ** 18, 18, P_WHYPE,
                         11000, 1000, 2050 * USD, int(0.98e18))
    assert s["debt_to_cover"] > 0 and not s["full_debt_close"]
    leftover_usd = (debt_wei - s["debt_pulled"]) / 10 ** 6
    assert leftover_usd >= MIN_LEFTOVER_USD * (1 + DUST_MARGIN) - 1
    assert s["repaid_usd"] < 1025                            # tighter than the plain 50% cap


def test_dust_guard_collateral_leftover():
    # collateral $5000 @ bonus 1.10, 50% cap = $4000 -> gross seize $4400 would leave only $600
    # of collateral (< $1000) -> cover shrinks so the collateral leftover stays >= the floor
    coll_wei = int(5000 / 64.60044434 * 10 ** 18)            # ~$5000 WHYPE
    debt_wei = 8000 * 10 ** 6
    s = size_liquidation(debt_wei, 6, P_USDT0, coll_wei, 18, P_WHYPE,
                         11000, 1000, 8000 * USD, int(0.98e18))
    assert s["debt_to_cover"] > 0
    assert not s["full_coll_seize"] and not s["full_debt_close"]
    coll_left_usd = (coll_wei - s["seized_gross"]) * P_WHYPE / 10 ** 18 / USD
    assert coll_left_usd >= MIN_LEFTOVER_USD                 # on-chain floor honoured
    assert s["repaid_usd"] < 4000                            # tighter than the plain 50% cap


def test_full_collateral_seize_fee_adjusted():
    # tiny collateral vs debt -> seize ALL collateral; we receive coll - fee, and the tx param
    # overshoots the exact inverse so Pool rounding/drift can't degrade it into a dusty partial
    coll_wei = 1 * 10 ** 18                                  # 1 WHYPE ~ $64.60
    debt_wei = 100_000 * 10 ** 6
    s = size_liquidation(debt_wei, 6, P_USDT0, coll_wei, 18, P_WHYPE,
                         11000, 1000, 100_000 * USD, int(0.98e18))
    assert s["full_coll_seize"]
    assert s["seized_gross"] == coll_wei
    assert s["seized"] < coll_wei                            # protocol fee came off the top
    assert abs(s["seized_usd"] / s["repaid_usd"] - 1.090) < 0.001
    assert s["debt_to_cover"] > s["debt_pulled"]             # overshoot; Pool clamps
    assert s["repaid_usd"] < 60                              # ~64.60/1.10


def test_never_overestimates():
    for hf in (int(0.90e18), int(0.98e18)):
        for coll_usd in (500, 3000, 50_000):
            coll_wei = int(coll_usd / 64.60044434 * 10 ** 18)
            s = size_liquidation(10_000 * 10 ** 6, 6, P_USDT0, coll_wei, 18, P_WHYPE,
                                 11000, 1000, 10_000 * USD, hf)
            if s["debt_to_cover"] == 0:
                continue
            assert s["seized"] <= s["seized_gross"] <= coll_wei
            assert s["debt_pulled"] <= s["debt_to_cover"]
            assert s["seized_usd"] <= s["repaid_usd"] * 1.10  # never above the gross bonus


def test_max_liquidatable_matches_onchain_rule():
    # 50% branch requires BOTH legs >= $2000 AND hf > 0.95e18 (strict, as deployed)
    d_wei, d_val, c_val, total = 10_000 * 10 ** 6, 10_000 * USD, 10_000 * USD, 10_000 * USD
    full, cf = max_liquidatable_debt(d_wei, d_val, c_val, total, int(0.95e18), 6, P_USDT0)
    assert full == d_wei and cf == 1.0                       # hf == threshold -> NOT > -> 100%
    half, cf = max_liquidatable_debt(d_wei, d_val, c_val, total, int(0.95e18) + 1, 6, P_USDT0)
    assert half == 5000 * 10 ** 6 and cf == 0.5
    # reserve debt <= 50% of TOTAL debt -> the whole reserve leg is liquidatable
    full2, cf = max_liquidatable_debt(d_wei, d_val, c_val, 20_000 * USD, int(0.98e18), 6, P_USDT0)
    assert full2 == d_wei and cf == 1.0


def test_size_liquidation_zero_on_bad_inputs():
    s = size_liquidation(0, 6, 1, 1, 18, 1, 11000, 1000, 0, 0)
    assert s["debt_to_cover"] == 0 and s["seized"] == 0
    s = size_liquidation(10 ** 6, 6, 1, 10 ** 18, 18, 1, 10000, 1000, USD, int(0.9e18))
    assert s["debt_to_cover"] == 0                           # par bonus: nothing to earn


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
