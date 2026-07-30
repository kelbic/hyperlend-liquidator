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
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO, "data")

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
# --- EOA gas-balance guard (katana lesson, ported 24.07) ---------------------------------------
# Гарда НЕ БЫЛО ВООБЩЕ: осушённый кошелёк вскрылся бы только штормом «insufficient funds»
# посреди каскада — то есть ровно тогда, когда бот обязан стрелять. Нода отвергает tx, если
# balance < GAS_LIMIT*maxFeePerGas (ПОЛНЫЙ конверт комиссии, не фактический расход).
# Пол готовности = max(один конверт, BALANCE_FIRES выстрелов по текущей базе).
# Стоит один eth_getBalance + один заголовок раз в BALANCE_CHECK_SEC.
BALANCE_CHECK_SEC = float(os.environ.get("HL_BALANCE_CHECK_SEC", "600"))
BALANCE_ALERT_SEC = float(os.environ.get("HL_BALANCE_ALERT_SEC", "3600"))
BALANCE_FIRES = int(os.environ.get("HL_BALANCE_FIRES", "3"))

POLL_SEC = float(os.environ.get("HL_POLL_SEC", "3"))            # base cadence (legacy once-loop only)
# --- amortized hot-set loop (see bot/executor.py loop()) ---------------------------------------
# The loop no longer blocks on a full-book sweep. Each iteration it (a) re-reads the HF of the
# HOT SET (borrowers with HF < HOT_HF) so a cross to <1 is caught within one iteration, and (b)
# advances a rolling full-book cursor by SWEEP_CHUNK borrowers to refresh hot-set membership. A
# compact status line is logged EVERY iteration, so the log is never silent for more than one
# cadence — which is what makes the 600s deadman stop false-firing.
HOT_POLL_SEC = float(os.environ.get("HL_HOT_POLL_SEC", "2"))    # sleep between iterations (~2-3s)
# HOT_HF: hot-set membership ceiling. Measured HF distribution (debt>=$500, book of ~25.5k, 7404
# with debt) 2026-07-17: <1.15=65, <1.30=202, <1.50=377, <2.0=722. Default 1.30 keeps the hot
# set ~200 (a few hundred, polled in ~1.5-2.5s) and catches any position within a ~23% burst of
# the line at hot-poll cadence. Widen toward 1.50 (377) before an anticipated mega-crash — the
# cost is a slightly larger hot poll. Positions healthier than HOT_HF that crash in are caught
# within one full-book cycle (the rolling sweep), a few minutes — the documented residual latency.
HOT_HF = float(os.environ.get("HL_HOT_HF", "1.30"))
# SWEEP_CHUNK: borrowers scanned per iteration by the rolling full-book cursor. On the fast
# official node a 500-call getUserAccountData aggregate3 is ~1.2s; unioned with the hot set the
# per-iteration read stays ~2 round-trips / a few seconds (< the 8s budget). 25.5k / 500 = ~51
# iterations, so the full book is re-swept every ~51*(work+HOT_POLL_SEC) ≈ 3-4 min continuously.
SWEEP_CHUNK = int(os.environ.get("HL_SWEEP_CHUNK", "500"))
# SWEEP_EVERY: катить курсор не каждую итерацию, а раз в N. Замер 30.07: итерация читает
# hot(~220) ∪ chunk(500) = ~720 аккаунтов за 1.1–1.4с, а блок HyperEVM идёт ~1с — мы опрашивали
# горячих МЕДЛЕННЕЕ, чем движется чейн, и пересечение HF<1 могло прожить целый блок незамеченным.
# При N=3 две итерации из трёх читают только hot (~0.35с), и горячая позиция перечитывается
# примерно вдвое чаще. Плата — полный цикл книги растягивается (~56с → ~90с), но это дёшево:
# потолок hot-set HOT_HF=1.30 очень щедрый, чтобы выпасть из наблюдения, позиции надо рухнуть
# с 1.30 до 1.0 внутри одного цикла. N=1 возвращает прежнее поведение ровно.
SWEEP_EVERY = int(os.environ.get("HL_SWEEP_EVERY", "3"))
# Cursor + hot-set persist here (repo data dir, NOT ~/.hyperlend-bot) so a restart resumes
# mid-book with a warm hot set instead of re-scanning from zero.
HOTSET_FILE = os.environ.get("HL_HOTSET_FILE", os.path.join(DATA_DIR, "hotset.json"))

# --- transport hardening (a single black-holing endpoint must never wedge the single-threaded
# loop). The stdlib socket `timeout` is per-recv, not total: a trickling/half-open response can
# block far past it (reproduced live — a 400-call multicall to one endpoint took 33s under a 25s
# socket timeout). So each RPC attempt also runs under a HARD total wall deadline (worker thread),
# and any endpoint that times out / hangs / 429s is benched and the next tried. -----------------
RPC_TIMEOUT = float(os.environ.get("HL_RPC_TIMEOUT", "8"))        # per-attempt socket timeout (s)
RPC_HARD_TIMEOUT = float(os.environ.get("HL_RPC_HARD_TIMEOUT", "10"))  # hard total wall cap (s)
RPC_RETRIES = int(os.environ.get("HL_RPC_RETRIES", "3"))         # attempts (~one per endpoint)
RPC_BENCH_SEC = float(os.environ.get("HL_RPC_BENCH_SEC", "30"))  # bench a failed/hung endpoint
# Успешный-но-медленный ответ бенчит узел (порт midnight slow_sec, 23.07: официальный узел
# отвечал 0.8-1.3с под multicall-нагрузкой против 0.27-0.49с у соседей и никогда не бенчился).
# Дефолт 3с консервативен под наши 500-вызовные чанки (~112KB: здоровый узел ≤1.2с, нагруженный
# легитимно до ~2.5с); 0 = выключить.
RPC_SLOW_SEC = float(os.environ.get("HL_RPC_SLOW_SEC", "3"))

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
# The whole liquidator fleet posts into ONE chat, so every outgoing alert is prefixed with this
# short tag — without it an alert cannot be attributed to the bot that sent it.
BOT_TAG = os.environ.get("HL_BOT_TAG", "hyperlend")
