"""Aave-v3 read helpers + liquidation sizing for HyperLend. Pure where possible (offline-tested
in test_aave.py); the on-chain reads take an Rpc.

Health factor (Aave convention, 1e18): HF = sum(collateral_i * price_i * liqThreshold_i)
/ sum(debt_j * price_j). Liquidatable when HF < 1e18. We read HF directly from the Pool via
getUserAccountData rather than recomputing it — the Pool is the source of truth the liquidation
will be judged against.

Sizing mirrors Aave's LiquidationLogic.executeLiquidationCall (v3.3+ rules, verified against the
deployed lib + aave-v3-origin source 2026-07-16):
  * close factor is PER-RESERVE: the reserve is 100% closable when its debt < $2k OR its
    collateral < $2k OR HF <= 0.95; otherwise the Pool caps the cover at 50% of the user's
    TOTAL debt (in base currency).
  * MustNotLeaveDust: if we neither repay ALL of the reserve's debt nor seize ALL of its
    collateral, BOTH leftovers must each stay >= $1000 (MIN_LEFTOVER_BASE) or the call reverts.
    So we size to one of exactly three revert-free shapes: full debt close, full collateral
    seize, or a partial that leaves >= $1000 (+margin) on both legs.
  * liquidationProtocolFee: the treasury takes fee_bps (10%) of the BONUS portion of the seized
    collateral — we RECEIVE collateralAmount - fee, not the gross amount. All proceeds math uses
    the fee-adjusted figure (never overestimate).
  * on full closes the sent debtToCover carries a small overshoot buffer: the Pool clamps
    debtToCover to its own max (min(maxLiquidatableDebt, collateral-covered debt)) and pulls only
    the actual amount, so accrued interest between scan and fire cannot degrade a planned full
    close into a dust-reverting partial. Leftover flash cash simply goes back to the repayment.
Everything in base currency uses AaveOracle prices (USD, 1e8).
"""
from __future__ import annotations

from analysis.keccak import selector
from analysis.protocols import (
    CLOSE_FACTOR_HF_THRESHOLD, DEFAULT_CLOSE_FACTOR_BPS, LIQ_PROTOCOL_FEE_BPS,
    MAX_CF_THRESHOLD_USD, MIN_LEFTOVER_USD, ORACLE_BASE_UNIT,
)

# --- selectors (computed) ------------------------------------------------------
SEL_GET_USER_ACCOUNT_DATA = selector("getUserAccountData(address)")
SEL_GET_USER_RESERVE_DATA = selector("getUserReserveData(address,address)")
SEL_GET_RESERVE_CONFIG = selector("getReserveConfigurationData(address)")
SEL_GET_ASSET_PRICE = selector("getAssetPrice(address)")
SEL_GET_RESERVES_LIST = selector("getReservesList()")
SEL_GET_ASSET_PRICES = selector("getAssetsPrices(address[])")
SEL_GET_LIQ_PROTOCOL_FEE = selector("getLiquidationProtocolFee(address)")

HF_INFINITY = 2 ** 128  # getUserAccountData returns type(uint256).max when there is no debt


def _words(h: str) -> list[int]:
    h = h[2:] if h.startswith("0x") else h
    return [int(h[i:i + 64], 16) for i in range(0, len(h), 64)]


def _addr_from_word(w: int) -> str:
    return "0x" + hex(w)[2:].rjust(40, "0")[-40:]


# --- decoders (pure) -----------------------------------------------------------
def decode_user_account_data(ret_hex: str) -> dict:
    """getUserAccountData -> (totalCollateralBase, totalDebtBase, availableBorrowsBase,
    currentLiquidationThreshold, ltv, healthFactor). Base amounts are USD*1e8; HF is 1e18."""
    w = _words(ret_hex)
    return {
        "total_collateral_base": w[0], "total_debt_base": w[1],
        "available_borrows_base": w[2], "liquidation_threshold": w[3],
        "ltv": w[4], "health_factor": w[5],
    }


def decode_user_reserve_data(ret_hex: str) -> dict:
    """getUserReserveData(asset,user) -> per-asset position. currentATokenBalance and
    currentVariableDebt are index-applied (human wei), so usable directly."""
    w = _words(ret_hex)
    return {
        "aToken_balance": w[0], "stable_debt": w[1], "variable_debt": w[2],
        "usage_as_collateral": bool(w[8]),
    }


def decode_reserve_config(ret_hex: str) -> dict:
    """getReserveConfigurationData -> (decimals, ltv, liquidationThreshold, liquidationBonus,
    reserveFactor, usageAsCollateralEnabled, borrowingEnabled, stableBorrowRateEnabled, isActive,
    isFrozen)."""
    w = _words(ret_hex)
    return {
        "decimals": w[0], "ltv": w[1], "liquidation_threshold": w[2],
        "liquidation_bonus": w[3], "reserve_factor": w[4],
        "usage_as_collateral": bool(w[5]), "borrowing_enabled": bool(w[6]),
        "is_active": bool(w[8]), "is_frozen": bool(w[9]),
    }


def decode_address_array(ret_hex: str) -> list[str]:
    w = _words(ret_hex)
    if not w:
        return []
    n = w[1]  # w[0] = offset (0x20), w[1] = length
    return [_addr_from_word(w[2 + i]) for i in range(n)]


# --- sizing (pure) -------------------------------------------------------------
# Overshoot on full closes: the Pool clamps debtToCover to its own max and pulls only the actual
# amount, so the buffer costs just the 4bps flash premium on 1% while making the planned full
# close immune to interest accrual / small oracle drift between scan and fire.
FULL_CLOSE_BUFFER = 0.01
# Partial liquidations target leftovers of MIN_LEFTOVER_BASE*(1+margin) on both legs so an oracle
# tick between scan and fire cannot push a leftover under $1000 (MustNotLeaveDust revert).
DUST_MARGIN = 0.05


def percent_mul(x: int, bps: int) -> int:
    """Aave PercentageMath.percentMul (half-up rounding)."""
    return (x * bps + 5000) // 10000


def percent_div(x: int, bps: int) -> int:
    """Aave PercentageMath.percentDiv (half-up rounding)."""
    return (x * 10000 + bps // 2) // bps


def max_liquidatable_debt(debt_wei: int, debt_val: int, coll_val: int,
                          total_debt_base: int, hf_1e18: int,
                          debt_dec: int, debt_price: int) -> tuple[int, float]:
    """Per-reserve close factor, mirroring LiquidationLogic: the reserve is 100% closable unless
    its debt AND collateral are both >= $2000 (base) AND HF > 0.95 — then the cover is capped at
    50% of the user's TOTAL debt (base). Returns (max debt wei, close-factor label for logs)."""
    thresh = MAX_CF_THRESHOLD_USD * ORACLE_BASE_UNIT
    hf_gate = int(CLOSE_FACTOR_HF_THRESHOLD * 1e18)
    if coll_val >= thresh and debt_val >= thresh and hf_1e18 > hf_gate:
        half_total_val = percent_mul(total_debt_base, DEFAULT_CLOSE_FACTOR_BPS)
        if debt_val > half_total_val:
            # floor conversion => never above the on-chain max (conservative)
            return half_total_val * (10 ** debt_dec) // debt_price, 0.5
    return debt_wei, 1.0


def liquidation_amounts(debt_pulled: int, debt_dec: int, debt_price: int,
                        coll_wei: int, coll_dec: int, coll_price: int,
                        bonus_bps: int, fee_bps: int) -> dict:
    """Mirror _calculateAvailableCollateralToLiquidate for a given actual debt pull: gross seized
    collateral (what leaves the borrower), the treasury's protocol fee (fee_bps of the BONUS
    portion), and what WE receive (gross - fee)."""
    base_coll = debt_price * debt_pulled * (10 ** coll_dec) // (coll_price * (10 ** debt_dec))
    gross = min(percent_mul(base_coll, bonus_bps), coll_wei)
    bonus_part = gross - percent_div(gross, bonus_bps)
    fee = percent_mul(bonus_part, fee_bps) if fee_bps else 0
    return {"gross": gross, "fee": fee, "received": gross - fee}


def size_liquidation(debt_wei: int, debt_dec: int, debt_price: int,
                     coll_wei: int, coll_dec: int, coll_price: int,
                     bonus_bps: int, fee_bps: int, total_debt_base: int, hf_1e18: int,
                     safety: float = 0.995) -> dict:
    """Compute a revert-free (debtToCover, expected received collateral) for one (debt,
    collateral) leg, honouring the per-reserve close factor, the MustNotLeaveDust rule and the
    liquidation protocol fee.

    All *_price are AaveOracle USD prices (1e8). Amounts are token wei. `bonus_bps` is the
    on-chain liquidationBonus factor (10000 = par); `fee_bps` the liquidationProtocolFee.
    Returns:
      debt_to_cover — the tx param / flash amount (overshoots on full closes; Pool clamps),
      debt_pulled   — expected ACTUAL debt the Pool pulls (<= debt_to_cover),
      seized        — expected collateral WE receive (net of the protocol fee) — use this for
                      the exit quote and all proceeds math,
      + gross/fee wei, USD figures and a close-factor label for logs. Zeroed dict when the leg
      cannot be liquidated without tripping the dust rule.
    """
    zero = {"debt_to_cover": 0, "debt_pulled": 0, "seized": 0, "seized_gross": 0, "fee": 0,
            "close_factor": 0.0, "full_debt_close": False, "full_coll_seize": False,
            "repaid_usd": 0.0, "seized_usd": 0.0, "net_bonus_usd": 0.0}
    if debt_wei <= 0 or coll_wei <= 0 or debt_price <= 0 or coll_price <= 0 or bonus_bps <= 10000:
        return zero

    unit_d, unit_c = 10 ** debt_dec, 10 ** coll_dec
    debt_val = debt_wei * debt_price // unit_d          # USD*1e8, this reserve's debt leg
    coll_val = coll_wei * coll_price // unit_c          # USD*1e8, this reserve's collateral leg

    max_liq_wei, cf = max_liquidatable_debt(debt_wei, debt_val, coll_val,
                                            total_debt_base, hf_1e18, debt_dec, debt_price)
    # debt pull that seizes ALL collateral (Pool's own inverse formula, mirrored)
    debt_all_coll = percent_div(coll_price * coll_wei * unit_d // (debt_price * unit_c), bonus_bps)

    full_debt = full_coll = False
    if max_liq_wei >= debt_wei and debt_all_coll >= debt_wei:
        # (a) full debt close — dust rule can't apply (all reserve debt repaid)
        pulled = debt_wei
        cover = int(debt_wei * (1 + FULL_CLOSE_BUFFER))
        full_debt = True
    elif debt_all_coll <= max_liq_wei:
        # (b) collateral binds — seize ALL collateral (dust-exempt); the Pool clamps the pull to
        # what the collateral covers, leftover flash cash flows back into the repayment
        pulled = min(debt_all_coll, debt_wei)
        cover = int(debt_all_coll * (1 + FULL_CLOSE_BUFFER))
        full_coll = True
    else:
        # (c) partial-partial: MustNotLeaveDust — cap the cover so BOTH leftovers keep
        # >= $1000*(1+margin); shrinking only ever increases the leftovers (safe direction)
        dust_floor = int(MIN_LEFTOVER_USD * ORACLE_BASE_UNIT * (1 + DUST_MARGIN))
        max_liq_val = max_liq_wei * debt_price // unit_d
        cover_val = min(max_liq_val,
                        debt_val - dust_floor,
                        percent_div(coll_val - dust_floor, bonus_bps))
        cover_val = int(cover_val * safety)
        if cover_val <= 0:
            return zero
        pulled = cover = min(cover_val * unit_d // debt_price, max_liq_wei)
        if pulled <= 0:
            return zero

    amt = liquidation_amounts(pulled, debt_dec, debt_price, coll_wei, coll_dec, coll_price,
                              bonus_bps, fee_bps)
    if full_coll:
        # seize-all branch: gross is exactly the balance (the Pool's clamp), not the pull-derived
        # figure whose rounding could land 1 wei off
        bonus_part = coll_wei - percent_div(coll_wei, bonus_bps)
        fee = percent_mul(bonus_part, fee_bps) if fee_bps else 0
        amt = {"gross": coll_wei, "fee": fee, "received": coll_wei - fee}

    repaid_usd = pulled * debt_price / unit_d / ORACLE_BASE_UNIT
    seized_usd = amt["received"] * coll_price / unit_c / ORACLE_BASE_UNIT
    return {
        "debt_to_cover": cover,
        "debt_pulled": pulled,
        "seized": amt["received"],
        "seized_gross": amt["gross"],
        "fee": amt["fee"],
        "close_factor": cf,
        "full_debt_close": full_debt,
        "full_coll_seize": full_coll,
        "repaid_usd": repaid_usd,
        "seized_usd": seized_usd,
        "net_bonus_usd": seized_usd - repaid_usd,
    }
