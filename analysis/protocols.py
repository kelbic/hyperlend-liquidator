"""HyperLend (HyperEVM, chainId 999) protocol registry + Aave-v3 log topics.

HyperLend is an Aave-v3.6 fork. Every address below was verified on-chain 2026-07-14 by reading
the PoolAddressesProvider (getPool/getPriceOracle/getPoolDataProvider/getACLManager) and each
token's symbol()/decimals(), AND cross-checked against docs.hyperlend.finance/developer-
documentation/contract-addresses. Topic0s are computed offline via analysis.keccak (never pasted).

Liquidation bonuses were read on-chain from PoolDataProvider.getReserveConfigurationData and match
the anchoring facts exactly: WHYPE 10%, wstHYPE/UETH 15%, UBTC 20%, USDT0 8% (bonus factor in bps,
10000 = par, so liqBonus 11000 => +10%). Oracle base currency = USD, unit 1e8 (BASE_CURRENCY_UNIT).
"""
from __future__ import annotations

from analysis.keccak import event_topic0

CHAIN_ID = 999  # HyperEVM mainnet (0x3e7)

# --- core Aave-v3 contracts (verified via ADDRESSES_PROVIDER + on-chain code) ---------------
POOL = "0x00A89d7a5A02160f20150EbEA7a2b5E4879A1A8b"                    # Pool (proxy)
ADDRESSES_PROVIDER = "0x72c98246a98bFe64022a3190e7710E157497170C"     # PoolAddressesProvider
ORACLE = "0xC9Fb4fbE842d57EAc1dF3e641a281827493A630e"                 # AaveOracle (USD, 1e8)
POOL_DATA_PROVIDER = "0x4f4d4cA1e0a8A21FE0B460613bEbe917f2eb4326"     # AaveProtocolDataProvider
UI_POOL_DATA_PROVIDER = "0xfc05a3fbf47094f53a8f98fda5dd8abdd336b9d4"  # UiPoolDataProvider
ACL_MANAGER = "0x10914Ee2C2dd3F3dEF9EFFB75906CA067700a04A"
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"

POOL_DEPLOY_BLOCK = 779363       # Pool proxy first has code here (binary-searched) — backfill start
FLASHLOAN_PREMIUM_BPS = 4        # FLASHLOAN_PREMIUM_TOTAL read on-chain = 4 (0.04%)
ORACLE_BASE_UNIT = 10 ** 8       # AaveOracle.BASE_CURRENCY_UNIT (USD with 8 decimals)

# --- LiquidationLogic constants (verified against the deployed lib 0x8dc095f2…8104 + aave-v3-
# origin source, 2026-07-16). The close factor is PER-RESERVE: the 50% cap only applies when the
# targeted reserve's debt AND collateral are both >= $2000 AND HF > 0.95; otherwise the reserve is
# 100% closable. A partial liquidation (neither all debt repaid nor all collateral seized) must
# leave BOTH leftovers >= $1000 or the Pool reverts with MustNotLeaveDust.
CLOSE_FACTOR_HF_THRESHOLD = 0.95   # CLOSE_FACTOR_HF_THRESHOLD = 0.95e18
MAX_CF_THRESHOLD_USD = 2000        # MIN_BASE_MAX_CLOSE_FACTOR_THRESHOLD = 2000e8
MIN_LEFTOVER_USD = 1000            # MIN_LEFTOVER_BASE = 1000e8 (MustNotLeaveDust floor)
DEFAULT_CLOSE_FACTOR_BPS = 5000    # DEFAULT_LIQUIDATION_CLOSE_FACTOR = 50% (of TOTAL debt, in base)

# liquidationProtocolFee: share of the liquidation BONUS that goes to the treasury, NOT to us.
# Read on-chain (PoolDataProvider.getLiquidationProtocolFee) = 1000 bps (10%) for ALL reserves
# (2026-07-16), and empirically confirmed on 16 real liquidations (received/repaid = 1.090 at
# bonus 1.10, 1.135 at 1.15). load_reserves() re-reads it per reserve; this is the fallback.
LIQ_PROTOCOL_FEE_BPS = 1000

# --- token registry (symbol/decimals verified on-chain 2026-07-14) --------------------------
# `bonus_bps` is the on-chain liquidationBonus factor (10000 = par). Verified per asset.
# bonus_bps / usable-as-collateral verified on-chain 2026-07-14. USDe has liqThreshold=0 => it is
# NOT seizable as collateral (borrow/supply only), so bonus is N/A (0). The executor reads the
# live bonus per asset anyway; these are documentation + a sanity cross-check.
TOKENS = {
    "WHYPE":   {"address": "0x5555555555555555555555555555555555555555", "decimals": 18, "bonus_bps": 11000},
    "wstHYPE": {"address": "0x94e8396e0869c9F2200760aF0621aFd240E1CF38", "decimals": 18, "bonus_bps": 11500},
    "UBTC":    {"address": "0x9FDBdA0A5e284c32744D2f17Ee5c74B284993463", "decimals": 8,  "bonus_bps": 12000},
    "UETH":    {"address": "0xBe6727B535545C67d5cAa73dEa54865B92CF7907", "decimals": 18, "bonus_bps": 11500},
    "USDe":    {"address": "0x5d3a1Ff2b6BAb83b63cd9AD0787074081a52ef34", "decimals": 18, "bonus_bps": 0},
    "USDT0":   {"address": "0xB8CE59FC3717ada4C02eaDF9682A9e934F625ebb", "decimals": 6,  "bonus_bps": 10800},
    "USDC":    {"address": "0xb88339CB7199b77E23DB6E890353E22632Ba630f", "decimals": 6,  "bonus_bps": 10800},
    "USDH":    {"address": "0x111111a1a0667d36bD57c0A9f569b98057111111", "decimals": 6,  "bonus_bps": 11000},
    # additional core-pool reserves (symbols/decimals/bonus verified on-chain 2026-07-14)
    "kHYPE":   {"address": "0xfD739d4e423301CE9385c1fb8850539D657C296D", "decimals": 18, "bonus_bps": 11000},
    "sUSDe":   {"address": "0x211Cc4DD073734dA055fbF44a2b4667d5E5fE5d2", "decimals": 18, "bonus_bps": 10800},
    "PT-kHYPE-24SEP2026": {"address": "0x50fC4EDC6346F36993Bb30Fe60E932504Ed17391", "decimals": 18, "bonus_bps": 11000},
    # 04.08: добраны из полного скана истории пула (4078 ликвидаций) — реестр знал не все
    # активы, и shadow-телеметрия не могла оценить событие в долларах (первые же две живые
    # записи пришли с debt_usd=None по USDHL). Символы/decimals прочитаны on-chain.
    "USDHL":   {"address": "0xb50A96253aBDF803D85efcDce07Ad8becBc52BD5", "decimals": 6,  "bonus_bps": 10800},
    "USR":     {"address": "0x0aD339d66BF4AeD5ce31c64Bc37B3244b6394A77", "decimals": 18, "bonus_bps": 10800},
    "USOL":    {"address": "0x068f321Fa8Fb9f0D135f290Ef6a3e2813e1c8A29", "decimals": 9,  "bonus_bps": 11500},
    "beHYPE":  {"address": "0xd8FC8F0b03eBA61F64D08B0bef69d80916E5DdA9", "decimals": 18, "bonus_bps": 11000},
    "PT-kHYPE-19MAR2026": {"address": "0xea84ca9849D9e76a78B91F221F84e9Ca065FC9f5", "decimals": 18, "bonus_bps": 11000},
}
# Стейблы вне «долларовых» символов: цена ~$1, но в STABLES их класть нельзя без проверки
# ликвидности выхода — здесь только для оценки размера в телеметрии.
USD_PEGGED_EXTRA = {TOKENS[s]["address"].lower() for s in ("USDHL", "USR")}
ADDR_TO_SYMBOL = {v["address"].lower(): k for k, v in TOKENS.items()}
DECIMALS = {v["address"].lower(): v["decimals"] for v in TOKENS.values()}

# Stablecoins (USD ~ 1): these are the preferred flash-loan / debt legs (deep LiquidSwap routes,
# cheap exit) and let us size debt in USD directly.
STABLES = {TOKENS[s]["address"].lower() for s in ("USDT0", "USDC", "USDH", "USDe")}

# --- Aave-v3 event topics (computed offline) ------------------------------------------------
SIG_BORROW = "Borrow(address,address,address,uint256,uint8,uint256,uint16)"
SIG_SUPPLY = "Supply(address,address,address,uint256,uint16)"
SIG_LIQUIDATION_CALL = "LiquidationCall(address,address,address,uint256,uint256,address,bool)"
TOPIC_BORROW = event_topic0(SIG_BORROW)                 # 0xb3d0848...  (onBehalfOf = topic[2])
TOPIC_SUPPLY = event_topic0(SIG_SUPPLY)
TOPIC_LIQUIDATION_CALL = event_topic0(SIG_LIQUIDATION_CALL)  # anchor 0xe413a321...


def sym(addr: str) -> str:
    return ADDR_TO_SYMBOL.get(addr.lower(), addr[:10])


def decimals_of(addr: str) -> int:
    return DECIMALS.get(addr.lower(), 18)


def is_stable(addr: str) -> bool:
    return addr.lower() in STABLES


def topic_borrower(log: dict) -> str:
    """Borrower for an Aave Borrow log = onBehalfOf = indexed topic[2] (low 20 bytes)."""
    return "0x" + log["topics"][2][-40:]
