"""Aave-v3 read helpers + liquidation sizing for HyperLend. Pure where possible (offline-tested
in test_aave.py); the on-chain reads take an Rpc.

Health factor (Aave convention, 1e18): HF = sum(collateral_i * price_i * liqThreshold_i)
/ sum(debt_j * price_j). Liquidatable when HF < 1e18. We read HF directly from the Pool via
getUserAccountData rather than recomputing it — the Pool is the source of truth the liquidation
will be judged against.

Sizing mirrors Aave's LiquidationLogic.executeLiquidationCall:
  * close factor: 100% if (userTotalDebtBase < $2k) OR (HF < 0.95); else 50%  (HyperLend rule).
  * pick the debt leg D (largest debt, prefer a stable for a deep/cheap flash+exit) and the
    collateral leg C (largest seizable collateral, prefer the highest bonus).
  * actualDebtToCover = min(closeFactor * userDebt_D,  collateralConstrainedMax_D)
        collateralConstrainedMax_D = userColl_C_value_D * 1e4 / bonus_bps
    i.e. we can never repay more debt than the borrower's collateral (incl. the bonus) can cover.
  * seizedCollateral_C = actualDebtToCover_value * bonus_bps / 1e4, expressed in C units.
Everything in base currency uses AaveOracle prices (USD, 1e8).
"""
from __future__ import annotations

from analysis.keccak import selector
from analysis.protocols import ORACLE_BASE_UNIT

# --- selectors (computed) ------------------------------------------------------
SEL_GET_USER_ACCOUNT_DATA = selector("getUserAccountData(address)")
SEL_GET_USER_RESERVE_DATA = selector("getUserReserveData(address,address)")
SEL_GET_RESERVE_CONFIG = selector("getReserveConfigurationData(address)")
SEL_GET_ASSET_PRICE = selector("getAssetPrice(address)")
SEL_GET_RESERVES_LIST = selector("getReservesList()")
SEL_GET_ASSET_PRICES = selector("getAssetsPrices(address[])")

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
def close_factor(total_debt_base: int, hf_1e18: int) -> float:
    """HyperLend close factor: 100% if debt < $2k OR HF < 0.95, else 50%."""
    from analysis.protocols import CLOSE_FACTOR_HF_THRESHOLD, SMALL_DEBT_FULL_CLOSE_USD
    debt_usd = total_debt_base / ORACLE_BASE_UNIT
    if debt_usd < SMALL_DEBT_FULL_CLOSE_USD or hf_1e18 < int(CLOSE_FACTOR_HF_THRESHOLD * 1e18):
        return 1.0
    return 0.5


def size_liquidation(debt_wei: int, debt_dec: int, debt_price: int,
                     coll_wei: int, coll_dec: int, coll_price: int,
                     bonus_bps: int, cf: float, safety: float = 0.995) -> dict:
    """Compute the exact (debtToCover, expected seized collateral) for one (debt, collateral) leg.

    All *_price are AaveOracle USD prices (1e8). Amounts are token wei. `bonus_bps` is the
    on-chain liquidationBonus factor (10000 = par). `safety` shaves a hair off the debt so
    rounding never makes the on-chain liquidationCall try to seize more collateral than exists
    (which would revert). Returns wei amounts + USD figures for the profit gate.
    """
    if debt_wei <= 0 or coll_wei <= 0 or debt_price <= 0 or coll_price <= 0 or bonus_bps <= 0:
        return {"debt_to_cover": 0, "seized": 0, "repaid_usd": 0.0, "seized_usd": 0.0,
                "gross_bonus_usd": 0.0}

    # base-currency (USD*1e8) values of the borrower's debt and collateral legs
    debt_val = debt_wei * debt_price // (10 ** debt_dec)          # USD*1e8
    coll_val = coll_wei * coll_price // (10 ** coll_dec)          # USD*1e8

    # cap 1: close factor on the debt leg
    max_by_cf_val = int(debt_val * cf)
    # cap 2: collateral (incl. bonus) can't be exceeded -> max debt value the collateral covers
    max_by_coll_val = coll_val * 10000 // bonus_bps
    cover_val = min(max_by_cf_val, max_by_coll_val)
    cover_val = int(cover_val * safety)
    if cover_val <= 0:
        return {"debt_to_cover": 0, "seized": 0, "repaid_usd": 0.0, "seized_usd": 0.0,
                "gross_bonus_usd": 0.0}

    # debtToCover in debt wei
    debt_to_cover = cover_val * (10 ** debt_dec) // debt_price
    debt_to_cover = min(debt_to_cover, debt_wei)
    # seized collateral value = cover_val * bonus; convert to collateral wei
    seized_val = cover_val * bonus_bps // 10000                    # USD*1e8
    seized = seized_val * (10 ** coll_dec) // coll_price
    seized = min(seized, coll_wei)

    return {
        "debt_to_cover": debt_to_cover,
        "seized": seized,
        "repaid_usd": debt_to_cover * debt_price / (10 ** debt_dec) / ORACLE_BASE_UNIT,
        "seized_usd": seized * coll_price / (10 ** coll_dec) / ORACLE_BASE_UNIT,
        "gross_bonus_usd": (seized_val - cover_val) / ORACLE_BASE_UNIT,
    }
