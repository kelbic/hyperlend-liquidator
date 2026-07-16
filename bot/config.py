"""HyperLend liquidator configuration (HL_ env prefix). Code defaults are SAFE: DRY_RUN on, no
contract set. The executor only sends when DRY_RUN=0 AND HL_CONTRACT is set. Same env-loading
idiom as the wc/katana bots — the same code runs in dry-run, fork, and live without edits.
"""
from __future__ import annotations
import os

from analysis.protocols import (
    ADDRESSES_PROVIDER, ORACLE, POOL, POOL_DATA_PROVIDER, FLASHLOAN_PREMIUM_BPS,
)

CHAIN_ID = 999  # HyperEVM

# --- RPC ---------------------------------------------------------------------------------------
# Read endpoint(s): rotated on failure (keep head/logs/reaction spread across these like katana).
# Write endpoint: where the signed tx is broadcast (defaults to the official node).
READ_RPCS = [r.strip() for r in os.environ.get(
    "HL_READ_RPCS",
    "https://hyperliquid.drpc.org,https://rpc.hyperliquid.xyz/evm,https://rpc.hyperlend.finance",
).split(",") if r.strip()]
RPC_WRITE = os.environ.get("HL_RPC", os.environ.get("HL_RPC_WRITE", "https://rpc.hyperliquid.xyz/evm"))

# --- core protocol addresses (verified on-chain — see analysis/protocols.py) -------------------
POOL_ADDR = POOL
ADDRESSES_PROVIDER_ADDR = ADDRESSES_PROVIDER
ORACLE_ADDR = ORACLE
DATA_PROVIDER_ADDR = POOL_DATA_PROVIDER
FLASH_PREMIUM_BPS = FLASHLOAN_PREMIUM_BPS

# --- executor knobs (env-overridable) ----------------------------------------------------------
DRY_RUN = os.environ.get("DRY_RUN", "1") != "0"            # default: DO NOT send
CONTRACT = os.environ.get("HL_CONTRACT", "")               # deployed HyperLendLiquidator (req to fire)
PRIVATE_KEY = os.environ.get("HL_PRIVATE_KEY", "")
KEYFILE = os.path.expanduser(os.environ.get("HL_KEYFILE", "~/.hyperlend-bot/key"))
if not PRIVATE_KEY and os.path.exists(KEYFILE):
    PRIVATE_KEY = open(KEYFILE).read().strip()

# path: flash (zero-capital, default) vs capital (contract pre-funded with the debt asset)
USE_FLASHLOAN = os.environ.get("HL_USE_FLASHLOAN", "1") != "0"

MIN_PROFIT_USD = float(os.environ.get("HL_MIN_PROFIT", "25"))   # off-chain net floor AND on-chain gate
MIN_DEBT_USD = float(os.environ.get("HL_MIN_DEBT_USD", "500"))  # ignore dust positions
MAX_IMPACT = float(os.environ.get("HL_MAX_IMPACT", "0.05"))     # skip if LiquidSwap price impact > this
WATCH_HF = float(os.environ.get("HL_WATCH_HF", "1.10"))         # refine reserves below this HF
REPORT_HF = float(os.environ.get("HL_REPORT_HF", "1.15"))

# HyperEVM: priority fee is NON-OPERATIVE (latency-FCFS, no priority auction) -> set ~0.
PRIORITY_GWEI = float(os.environ.get("HL_PRIORITY_GWEI", "0"))
# HARD gas limit — eth_estimateGas is unreliable/silently-passing on the flashloan+liq+swap
# callback path; NEVER estimate. Generous enough for flashLoanSimple -> liquidationCall -> swap
# -> repay -> sweep.
GAS_LIMIT = int(os.environ.get("HL_GAS_LIMIT", "2500000"))
GAS_UNITS_EST = int(os.environ.get("HL_GAS_UNITS", "1500000"))  # for the gas-USD kill-switch only
HYPE_USD = float(os.environ.get("HL_HYPE_USD", "45"))           # rough, gas-USD estimate only

POLL_SEC = float(os.environ.get("HL_POLL_SEC", "3"))            # base cadence (calm)
HOT_POLL_SEC = float(os.environ.get("HL_HOT_POLL_SEC", "1"))    # cadence when near-edge targets exist

# kill-switch / dedup
MAX_DAILY_GAS_USD = float(os.environ.get("HL_MAX_DAILY_GAS_USD", "5"))
MAX_CONSEC_REVERTS = int(os.environ.get("HL_MAX_CONSEC_REVERTS", "3"))
DEDUP_SEC = float(os.environ.get("HL_DEDUP_SEC", "60"))
# a REVERTED target must NOT be retried next pass (3 quick reverts on one bad target would trip
# the kill-switch) — block it for this cooldown instead
REVERT_COOLDOWN_SEC = float(os.environ.get("HL_REVERT_COOLDOWN_SEC", "300"))
HEARTBEAT_SEC = float(os.environ.get("HL_HEARTBEAT_SEC", "0"))  # OFF by default (quiet cadence)

RAW_TX = os.environ.get("HL_RAW_TX", "0") == "1"               # 0 = cast (default), 1 = eth_account

STATE_FILE = os.path.expanduser(os.environ.get("HL_STATE", "~/.hyperlend-bot/state.json"))
LOCK_FILE = os.path.expanduser(os.environ.get("HL_LOCK", "~/.hyperlend-bot/executor.lock"))

# Telegram (optional; same channel-file convention as the reference bots).
TG_ENV_FILE = os.path.expanduser("~/.claude/channels/telegram/.env")
TG_CHAT_ID = os.environ.get("HL_CHAT_ID", "265715923")
