"""HyperLend (HyperEVM, chainId 999) live-signing liquidation executor.

Autonomous loop: monitor.scan() finds HF<1 positions and sizes each (Aave-v3 close-factor +
collateral-constrained debtToCover) -> evaluate() quotes the exit on LiquidSwap (collateral ->
debt asset) and gates on net after flash-loan repayment + gas -> fire() signs and broadcasts an
atomic HyperLendLiquidator.liquidate() tx (flash-loan the debt, liquidationCall, swap, repay,
keep the spread). Framework/idioms cloned from the live wc/katana Morpho bots; the KEY DIFFERENCE
is Aave-v3 mechanics (flashLoanSimple + liquidationCall) instead of Morpho.

WRITE PATH — two implementations behind HL_RAW_TX (see the "raw-tx write path" section):
  * HL_RAW_TX=1: calldata is ABI-encoded IN PROCESS (_encode_liquidate, byte-checked against
    `cast calldata` in the tests), signed locally (EIP-1559, chainId 999) and broadcast — then
    fire() RETURNS. Receipts are reaped on later passes by _check_pending(), which is what books
    wins/reverts, reconciles gas, and feeds the kill-switch.
  * HL_RAW_TX=0 (default): the legacy `cast send` subprocess, unchanged except that the private
    key now travels in the environment rather than argv.
WHY: `cast send` blocks until the receipt lands — MEASURED 4.4s on 2026-07-19. On a latency-FCFS
chain the second target of a cascade idled for the first target's entire confirmation. Since a
send can no longer be judged at fire time, the fleet rule is explicit: ONLY receipt.status == 0 is
a revert. A failure at fire time is a send_error — it does NOT feed the kill-switch — but ONLY
when non-delivery is PROVEN (signing/encoding failed, or the node returned an explicit in-body
verdict). A transport failure proves nothing: the tx is filed as pending on its locally computed
hash so a receipt can still settle it (see _sign_and_send / SendAmbiguous).

Thesis (do not re-derive): HyperEVM is latency-FCFS with NO priority-fee auction — priority fee
is non-operative, so we cannot outbid, only out-speed, and from Vienna/US infra we structurally
lose the whale-ticket races to the Tokyo-colocated pros. This bot is a cheap option: be reliable
and present to harvest MID-TIER ($10k-$50k) spillover during crash bursts when the pros hit
capacity. So: don't burn gas racing whales; gate hard on net profit; stay up.

DETECTION LOOP — amortized hot-set (mirrors the katana bot's hot-set pattern, adapted to Aave's
full-book model; see loop() / _hot_iteration()). The old loop() re-swept getUserAccountData over
the ENTIRE ~25k-borrower book every pass (1.5-5.5 min, up to 20 min under RPC stress — during which
the log was SILENT and the 600s deadman false-fired). Instead, each iteration now:
  a. re-polls the HOT SET (borrowers with HF < HL_HOT_HF) so a cross to HF<1 is caught within ONE
     iteration (~seconds), running the UNCHANGED evaluate()/fire() economics on any HF<1;
  b. advances a rolling full-book cursor by HL_SWEEP_CHUNK borrowers to refresh hot-set membership,
     covering all 25k every ~ceil(N/CHUNK) iterations (~a few min), continuously — NO blocking
     full sweep, so the log never goes silent;
  c. emits a compact status line EVERY iteration (this is the root-cause fix for the deadman
     false-alarm — the deadman itself is unchanged);
  d. sleeps HL_HOT_POLL_SEC.
Hot poll + cursor slice are read in ONE bounded multicall over (hot ∪ chunk). Transport is hardened
(analysis/rpc.py): a short socket timeout PLUS a HARD total wall deadline per attempt (the socket
timeout is per-recv, not total — a trickling endpoint outlasts it: a 400-call multicall took 33s
under a 25s socket timeout), and any hung / timed-out / rate-limited endpoint is benched and the
next tried — so one black-holing node can never wedge the single-threaded loop.
Residual latency: a position HEALTHIER than HL_HOT_HF that crashes straight under 1 between sweeps
is caught within one full-book cycle (a few min), not at hot-poll cadence — HL_HOT_HF is sized wide
(default 1.30) to make that rare, and is env-tunable (widen before an anticipated mega-crash).

Capital protection (defaults are safe — this NEVER sends by default):
  * DRY_RUN=1 by default: logs what it WOULD do, sends nothing. Guard reads "DRY".
  * Off-chain net gate: fires only if quoted net (proceeds - flashRepay - gas) >= HL_MIN_PROFIT.
  * On-chain minProfit gate (2nd layer): the tx reverts unless realised debt-asset surplus >=
    floor — a stale/optimistic quote cannot execute a losing liquidation.
  * Swap-input haircut (bot/liqd.py): baked amountIn <= real seized collateral (no over-pull).
  * HARD gas limit (never eth_estimateGas — it passes silently on reverting liq calls).
  * KILL-SWITCH: daily gas-USD cap + consecutive-revert cap -> stop + alert.
  * DEDUP: (borrower) journal — don't re-fire a recent target.
  * flock single-instance: a second executor cannot run against the same key.

Usage:
    DRY_RUN=1 python3 -u -m bot.executor once     # single diagnostic pass (safe)
    DRY_RUN=0 HL_CONTRACT=0x.. python3 -u -m bot.executor loop
    python3 -u -m bot.executor reset              # clear kill-switch / dedup
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from eth_abi import encode as abi_encode
from eth_utils import keccak, to_checksum_address

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from bot import config as C                                    # noqa: E402
from bot import liqd                                           # noqa: E402
from bot import shadow                                         # noqa: E402
from analysis.aave import (                                    # noqa: E402
    HF_INFINITY, SEL_GET_USER_ACCOUNT_DATA, decode_user_account_data, size_liquidation,
)
from analysis.monitor import (                                 # noqa: E402
    discover_borrowers, load_book, load_reserves, refine, save_book, scan, sweep_accounts,
)
from analysis.protocols import ORACLE_BASE_UNIT, is_stable, sym  # noqa: E402
from analysis.rpc import Rpc, _run_with_deadline, http_post_json  # noqa: E402

LIQUIDATE_SIG = "liquidate(address,address,address,uint256,bool,address,bytes,uint256)"
# Argument types and selector are DERIVED from the signature string above, never hand-pasted
# (project rule, see analysis/keccak.py): a typo in either can only come from a typo in the one
# canonical signature, which the byte-equality test against `cast calldata` would catch.
LIQUIDATE_TYPES = LIQUIDATE_SIG[LIQUIDATE_SIG.index("(") + 1:-1].split(",")
LIQUIDATE_SELECTOR = "0x" + keccak(text=LIQUIDATE_SIG)[:4].hex()   # 0x3c78a656


# --------------------------------------------------------------------------- state / guards
def load_state() -> dict:
    if os.path.exists(C.STATE_FILE):
        try:
            return json.load(open(C.STATE_FILE))
        except Exception:
            pass
    return {"day": "", "gas_usd": 0.0, "consec_reverts": 0, "sent": {},
            "passes": 0, "fires": 0, "reverts": 0, "last_heartbeat": 0}


def save_state(st: dict) -> None:
    os.makedirs(os.path.dirname(C.STATE_FILE), exist_ok=True)
    tmp = C.STATE_FILE + ".tmp"
    json.dump(st, open(tmp, "w"))
    os.replace(tmp, C.STATE_FILE)


def _roll_day(st: dict, today: str) -> None:
    if st.get("day") != today:
        st["day"] = today
        st["gas_usd"] = 0.0


# --------------------------------------------------------------------------- hot-set + cursor
# Persisted in the repo data dir (NOT ~/.hyperlend-bot), so a restart resumes mid-book with a warm
# hot set instead of re-scanning 25k borrowers from zero. `cursor` indexes book["borrowers"] for
# the rolling full-book sweep; `hot` is the set of borrowers with HF < HOT_HF that we re-poll every
# iteration.
def load_hotset() -> dict:
    if os.path.exists(C.HOTSET_FILE):
        try:
            hs = json.load(open(C.HOTSET_FILE))
            return {"cursor": int(hs.get("cursor", 0)), "hot": list(hs.get("hot", []))}
        except Exception:
            pass
    return {"cursor": 0, "hot": []}


def save_hotset(hs: dict) -> None:
    os.makedirs(os.path.dirname(C.HOTSET_FILE), exist_ok=True)
    tmp = C.HOTSET_FILE + ".tmp"
    json.dump({"cursor": hs.get("cursor", 0), "hot": sorted(hs.get("hot", []))}, open(tmp, "w"))
    os.replace(tmp, C.HOTSET_FILE)


def next_chunk(borrowers: list[str], cursor: int, chunk: int) -> tuple[list[str], int, bool]:
    """Advance the rolling full-book cursor by `chunk`. Returns (slice, new_cursor, wrapped).
    Wraps to 0 at the end of the book so every borrower is re-swept every ceil(N/chunk) iterations
    — a borrower can never be permanently dropped. `wrapped` marks the end of a full cycle (when
    the loop runs discovery + persists the book)."""
    n = len(borrowers)
    if n == 0:
        return [], 0, True
    cursor %= n
    end = cursor + chunk
    sl = borrowers[cursor:end]
    if end >= n:
        return sl, 0, True
    return sl, end, False


def targets_from_accounts(accounts: dict, min_debt_usd: float) -> list[str]:
    """Borrowers just read that are liquidatable NOW: HF < 1 with non-dust debt. Feeds the exact
    same refine() -> evaluate() -> fire() path the full-book scan used (economics unchanged)."""
    floor = min_debt_usd * ORACLE_BASE_UNIT
    return [b for b, a in accounts.items()
            if a["health_factor"] < int(1e18) and a["total_debt_base"] >= floor]


def update_hotset(hot: set[str], accounts: dict, hot_hf: float, min_debt_usd: float,
                  drop: set[str] = frozenset()) -> set[str]:
    """Recompute hot-set membership from freshly-read `accounts`:
      * ADD any borrower read with HF < hot_hf, non-dust debt, and finite HF (has debt),
      * DROP any hot member read with HF >= hot_hf or infinite HF (recovered / debt repaid),
      * DROP anything in `drop` (a borrower we just fired — removed until the sweep re-seeds it),
      * borrowers NOT in `accounts` this iteration KEEP their membership (still watched; they are
        re-read every iteration because the hot set is always part of the union we sweep).
    Pure — no I/O — so the transitions are unit-tested directly."""
    ceil_wei = int(hot_hf * 1e18)
    floor = min_debt_usd * ORACLE_BASE_UNIT
    new = set(hot)
    for b, a in accounts.items():
        hf, debt = a["health_factor"], a["total_debt_base"]
        if hf < ceil_wei and hf < HF_INFINITY and debt >= floor:
            new.add(b)
        else:
            new.discard(b)      # read but no longer qualifies -> drop
    new -= set(drop)
    return new


class GuardTripped(Exception):
    pass


def guard_ok(st: dict) -> tuple[bool, str]:
    if st["consec_reverts"] >= C.MAX_CONSEC_REVERTS:
        return False, f"{st['consec_reverts']} consecutive reverts >= {C.MAX_CONSEC_REVERTS}"
    if st["gas_usd"] >= C.MAX_DAILY_GAS_USD:
        return False, f"daily gas ${st['gas_usd']:.2f} >= ${C.MAX_DAILY_GAS_USD}"
    return True, ""


_last_balance_check = 0.0
_last_balance_alert = 0.0


def check_balance(st: dict, now_ts: float, force: bool = False) -> bool:
    """EOA gas-balance guard (ported from katana, which learned it the hard way: there was NO
    guard here either, so a drained wallet would surface ONLY as an 'insufficient funds'
    send-error storm mid-cascade — exactly when the bot must be firing).

    The node REJECTS a tx unless balance >= GAS_LIMIT*maxFeePerGas, i.e. the FULL fee envelope
    (maxFee is a CAP, not the charge — but the node still requires it upfront). Readiness floor =
    max(one envelope, BALANCE_FIRES fires' burn at the current base). Cheap: one eth_getBalance +
    one header per BALANCE_CHECK_SEC (force=True bypasses the throttle for the pre-fire gate).

    Fails OPEN (returns True) if the check itself errors — a flaky RPC must never block a fire.
    Returns False + a throttled alert when underfunded."""
    global _last_balance_check, _last_balance_alert
    if not force and now_ts - _last_balance_check < C.BALANCE_CHECK_SEC:
        return True
    addr = owner_address()
    if not addr:
        return True
    _last_balance_check = now_ts
    try:
        bal = int(_rpc_write("eth_getBalance", [addr, "latest"]), 16)
        max_fee, priority = _fee_params()
        base = max(0, (max_fee - priority) // 2)
        per_fire = C.GAS_UNITS_EST * (base + priority)
        need = max(C.GAS_LIMIT * max_fee, C.BALANCE_FIRES * per_fire)
    except Exception as e:
        print(f"balance check failed (skipped, fail-open): {e}")
        return True
    st["balance_hype"] = round(bal / 1e18, 6)
    if bal >= need:
        return True
    if now_ts - _last_balance_alert > C.BALANCE_ALERT_SEC:
        _last_balance_alert = now_ts
        alert(f"⛽ LOW GAS BALANCE: {bal / 1e18:.4f} HYPE < floor {need / 1e18:.4f} HYPE "
              f"(node needs GAS_LIMIT×maxFee = {C.GAS_LIMIT * max_fee / 1e18:.4f} HYPE per fire). "
              f"Бот НЕ СМОЖЕТ выстрелить — пополнить {addr}.")
    return False


def recently_fired(st: dict, key: str, now_ts: float) -> bool:
    """Dedup: a fired target is blocked for DEDUP_SEC; a REVERTED target is blocked for the
    longer REVERT_COOLDOWN_SEC — retrying the same borrower next pass just burns the remaining
    consec-revert budget and trips the kill-switch on one bad target (katana lesson: reverts
    must not be retried immediately).

    A PENDING target is blocked until it settles, regardless of age. This status only exists on
    the non-blocking raw-tx path, where fire() returns before the receipt: DEDUP_SEC (60s) is far
    shorter than the PENDING_STALE_SEC (600s) wall, so an age-based rule would happily fire a
    SECOND liquidation of the same borrower while the first is still in flight — paying gas twice
    to have the second one revert on an already-cured position. _check_pending() moves the record
    to ok/revert/stale, and only then do the normal cooldowns apply."""
    rec = st["sent"].get(key)
    if not rec:
        return False
    age = now_ts - rec["ts"]
    status = rec.get("status")
    if status == "pending":
        return True
    if status == "revert":
        return age < C.REVERT_COOLDOWN_SEC
    return age < C.DEDUP_SEC


# --------------------------------------------------------------------------- telegram (optional)
def _tagged(text: str) -> str:
    """Prefix an alert with the bot tag. Applied ONLY here, at the single send point, so no
    caller (nor alert_async, which just hands its text to alert()) can double-tag; idempotent
    anyway, so an already-tagged text passes through unchanged."""
    pre = f"[{C.BOT_TAG}]"
    return text if text.startswith(pre) else f"{pre} {text}"


def _tg_muted() -> bool:
    """True when the Telegram transport must stay shut.

    19.07: a test run posted FIXTURES into the operator's live chat — borrower `0xabababab…`,
    tx hash `0xffff…ffff`, and the literal string 'not-an-address'. The suites exercise fire()
    and _check_pending, both of which alert; the previous defence was a stub passed by each
    test, so the first test that forgot one reached the real chat.

    A stub per test cannot hold that line — the guard belongs at the transport, where forgetting
    is impossible. Default is SAFE: if any test module of this repo is loaded, the transport is
    off unless a caller explicitly opts back in.

    HL_MUTE_TG: "1" = always mute, "0" = explicit opt-in (only _capture_alerts, which has already
    replaced urlopen with a fake — it verifies the alert TEXT and must still reach the transport),
    unset = auto-detect. The opt-in is deliberately awkward to reach by accident."""
    forced = os.environ.get("HL_MUTE_TG")
    if forced == "1":
        return True
    if forced == "0":
        return False
    # `python3 -m bot.test_executor` loads the suite as __main__, NOT under its dotted name — a
    # sys.modules scan alone MISSES the repo's own runner, which is exactly how the fixtures got
    # out. Check both: the entry point's filename, and imported test modules (pytest-style).
    main_file = os.path.basename(getattr(sys.modules.get("__main__"), "__file__", "") or "")
    if main_file.startswith("test_"):
        return True
    return any(m.startswith(("bot.test_", "analysis.test_")) for m in sys.modules)


def alert(text: str) -> None:
    text = _tagged(text)
    if _tg_muted():
        print(f"[tg muted] {text}")
        return
    if not C.TG_CHAT_ID:
        return
    try:
        token = None
        with open(C.TG_ENV_FILE) as f:
            for ln in f:
                if ln.startswith("TELEGRAM_BOT_TOKEN="):
                    token = ln.split("=", 1)[1].strip()
        if not token:
            return
        data = urllib.parse.urlencode({"chat_id": C.TG_CHAT_ID, "text": text}).encode()
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data), timeout=15)
    except Exception as e:
        print(f"alert fail: {e}")


def alert_async(text: str) -> None:
    """Fire-and-forget alert for the FIRE path. alert() is a synchronous HTTPS POST to
    api.telegram.org — ~0.1-0.5s healthy, and UNBOUNDED against a trickling endpoint (urlopen's
    timeout=15 is per-recv, not wall — the exact failure mode the RPC transport was hardened
    against). On a latency-FCFS chain that cost must never sit between the liquidation decision
    and the broadcast, and a Telegram outage must never delay or fail a shot: post from a daemon
    thread and move straight on. Any alert failure dies with the throwaway thread."""
    def _post():
        try:
            alert(text)
        except Exception:
            pass                    # a failed alert must never matter to the shot
    threading.Thread(target=_post, daemon=True).start()


# --------------------------------------------------------------------------- gas
def gas_cost_usd(rpc: Rpc) -> float:
    try:
        base = rpc.base_fee()
        _note_basefee(base)     # кормит кэш _fee_params: путь выстрела без лишнего RPC
    except Exception:
        base = int(0.1 * 1e9)
    return C.GAS_UNITS_EST * base / 1e18 * C.HYPE_USD


# --------------------------------------------------------------------------- fresh HF re-read
def fresh_hf(rpc: Rpc, borrower: str) -> float | None:
    try:
        ret = rpc.eth_call(C.POOL_ADDR, SEL_GET_USER_ACCOUNT_DATA + borrower[2:].rjust(64, "0"))
        return decode_user_account_data(ret)["health_factor"] / 1e18
    except Exception:
        return None


# --------------------------------------------------------------------------- evaluate
CHUNK_FRACTIONS = ((1, 1), (3, 4), (1, 2), (7, 20), (1, 4), (3, 20), (1, 10), (3, 50))
MIN_CHUNK_FRACTION = float(os.environ.get("HL_MIN_CHUNK_FRACTION", "0.002"))
# Бюджет обхода лестницы. Одна котировка LiquidSwap стоит 1.7–3.5с (замер 30.07), поэтому
# фиксированные 6с обрывали обход на третьей ступени и цель с бонусом $32.9k возвращалась как
# «неприбыльна» — та же дыра, только глубже. Бюджет пропорционален призу: искать выход для
# $32k стоит трёх десятков секунд, для $50 — нет.
# Сокращать обход по impact НЕЛЬЗЯ: маршрутизатор переключает маршрут скачком (замер: 29.7%
# на 7/20 и 0.23% на 1/4), линейная экстраполяция impact пропустила бы лучшую ступень.
EVAL_DEADLINE_SEC = float(os.environ.get("HL_EVAL_DEADLINE_SEC", "6"))
EVAL_DEADLINE_MAX_SEC = float(os.environ.get("HL_EVAL_DEADLINE_MAX_SEC", "40"))
EVAL_SEC_PER_BONUS_USD = float(os.environ.get("HL_EVAL_SEC_PER_BONUS_USD", "0.001"))


def _chunk_fractions(full_bonus_usd: float | None, gas_usd: float):
    """Доли полного закрытия, крупные первыми: статическая лестница, затем ограниченный
    геометрический спуск. ЛЕНИВО: evaluate() выходит на первом прибыльном чанке, поэтому для
    обычной позиции спуск не порождается вовсе и число котировок не меняется.

    Нижняя граница экономическая: чанк меньше f_lo не окупит порог прибыли плюс газ, спускаться
    туда бессмысленно (портировано из katana, где та же лестница работает с 20.07)."""
    for num, den in CHUNK_FRACTIONS:
        yield num, den
    if not full_bonus_usd or full_bonus_usd <= 0:
        return
    f_lo = max((C.MIN_PROFIT_USD + gas_usd) / full_bonus_usd, MIN_CHUNK_FRACTION)
    num, den = CHUNK_FRACTIONS[-1]
    while True:
        den *= 2
        if num / den < f_lo:
            return
        yield num, den


def _eval_budget(full_bonus_usd: float | None) -> float:
    """Секунды на обход лестницы: пол для мелочи, потолок чтобы каскад не встал на одной цели."""
    if not full_bonus_usd or full_bonus_usd <= 0:
        return EVAL_DEADLINE_SEC
    return min(EVAL_DEADLINE_MAX_SEC,
               max(EVAL_DEADLINE_SEC, full_bonus_usd * EVAL_SEC_PER_BONUS_USD))


def _resize(t: dict, num: int, den: int) -> dict | None:
    """Пересайзить цель под долю пула ИХ ЖЕ size_liquidation: ограничиваем допустимый pull,
    а close factor, MustNotLeaveDust и комиссию протокола пересчитывает боевая функция.
    None, если сырых входов в строке нет (старый кэш) или чанк вырождается в ноль."""
    if any(k not in t for k in ("debt_wei", "coll_wei", "total_debt_base", "hf_1e18")):
        return None
    cap = t["debt_pulled"] * num // den
    if cap <= 0:
        return None
    sz = size_liquidation(t["debt_wei"], t["debt_dec"], t["debt_price"],
                          t["coll_wei"], t["coll_dec"], t["coll_price"],
                          t["bonus_bps"], t["fee_bps"], t["total_debt_base"], t["hf_1e18"],
                          max_cover_wei=cap)
    if sz["debt_to_cover"] <= 0 or sz["seized"] <= 0:
        return None
    return {**t, **sz}


def evaluate(t: dict, gas_usd: float) -> dict | None:
    """Лестница чанков поверх _evaluate_one: крупнейший прибыльный размер выигрывает.

    30.07: без неё две крупнейшие позиции флота ($734k и $342k WHYPE→USDC, чистый бонус
    $32.9k и $15.3k) были недостижимы — полный размер даёт impact 45–53%, бот корректно
    отказывался и молчал. На 1/16 та же позиция проходит порог с +$1,947."""
    first = None
    deadline = time.monotonic() + _eval_budget(t.get("net_bonus_usd"))
    for num, den in _chunk_fractions(t.get("net_bonus_usd"), gas_usd):
        if first is not None and time.monotonic() > deadline:
            break                       # бюджет прохода потрачен; полный размер уже в first
        tt = t if den == 1 else _resize(t, num, den)
        if tt is None:
            continue
        res = _evaluate_one(tt, gas_usd)
        if res is None:
            continue
        res["f"] = num / den
        if first is None:
            first = res                 # ответ по полному размеру — он же и лог по умолчанию
        if "skip" in res:
            continue
        if res.get("profitable"):
            return res
    return first


def _evaluate_one(t: dict, gas_usd: float) -> dict | None:
    """Quote the exit for target `t` on LiquidSwap and gate on net profit. Returns fire params
    (swap_target, swap_calldata, min_profit_wei, net_usd, ...) or None if not profitable.

    `t['seized']` is what we actually RECEIVE (net of the liquidation protocol fee — 10% of the
    bonus goes to the treasury), so the swap amountIn haircut and all proceeds math sit on top
    of the fee-adjusted figure. `t['debt_to_cover']` is the tx param / flash amount (overshoots
    on full closes; the Pool clamps and pulls only `t['debt_pulled']` — the unpulled flash cash
    flows straight back into the repayment, so only the premium is charged on the full amount)."""
    seized = t["seized"]
    debt_to_cover = t["debt_to_cover"]
    debt_pulled = t.get("debt_pulled", debt_to_cover)
    if seized <= 0 or debt_to_cover <= 0:
        return None
    try:
        q = liqd.quote_for_seized(t["coll_asset"], t["debt_asset"], seized,
                                  t["coll_dec"], t["debt_dec"])
    except liqd.NoRouteError:
        return {"skip": "no LiquidSwap route (illiquid/exotic collateral)"}
    except Exception as e:
        return {"skip": f"quote error: {e}"}

    proceeds = q["amount_out"]                                   # debt-asset wei out of the swap
    # what the liquidation consumes: the ACTUAL pull (<= debt_to_cover) + the flash premium
    # (0.04%), which IS charged on the full flashed amount. Capital path repays only the pull.
    if C.USE_FLASHLOAN:
        premium = (debt_to_cover * C.FLASH_PREMIUM_BPS + 9999) // 10000
        owed = debt_pulled + premium
    else:
        owed = debt_pulled
    net_wei = proceeds - owed
    dprice = t["debt_price"]
    net_usd = net_wei / 10 ** t["debt_dec"] * dprice / ORACLE_BASE_UNIT - gas_usd

    # on-chain minProfit floor (debt-asset wei): HL_MIN_PROFIT USD converted at the debt price.
    min_profit_wei = int(C.MIN_PROFIT_USD * ORACLE_BASE_UNIT / dprice * 10 ** t["debt_dec"])
    ok = (q["price_impact"] <= C.MAX_IMPACT) and (net_usd >= C.MIN_PROFIT_USD)
    return {
        "swap_target": q["swap_target"], "swap_calldata": q["swap_calldata"],
        "proceeds": proceeds, "owed": owed, "net_wei": net_wei, "net_usd": net_usd,
        "impact": q["price_impact"], "min_profit_wei": min_profit_wei,
        "debt_to_cover": debt_to_cover, "seized": seized, "profitable": ok,
        # размер чанка в USD — лог и алерт должны показывать РЕАЛЬНО закрываемую часть,
        # иначе «cover=$367k» рядом с выстрелом на 1/16 читается как ошибка
        "repaid_usd": t.get("repaid_usd"), "net_bonus_usd": t.get("net_bonus_usd"),
    }


# --------------------------------------------------------------------------- raw-tx write path
# IN-PROCESS sign + broadcast behind HL_RAW_TX=1 (the flag existed in config.py but was dead).
# WHY: `cast send` blocks the whole process until the receipt lands — MEASURED 4.4s on 2026-07-19.
# On a latency-FCFS chain with no priority auction, 4.4s of dead time is not a cost, it is the
# whole edge: in a cascade the SECOND target sits idle for the first target's entire confirmation
# while the Tokyo-colocated pros take it. So the fire path now ends at the broadcast, and receipts
# are reaped on later passes by _check_pending() (WC pattern, wc-liquidator/bot/executor.py).
#
# analysis/rpc.py is READ-ONLY by construction (whitelist; no way to send a tx through it), so the
# write leg — eth_getTransactionCount / eth_sendRawTransaction / eth_getBlockByNumber for the fee —
# goes through this separate minimal client against C.RPC_WRITE (mirrors the midnight bot).
# Receipts are READS and go through the hardened, rotating Rpc.

# Local counter layered over the node's 'pending'. `send_ts` is the monotonic clock of the last
# BROADCAST (see _pending_nonce): the local bump is only trusted while a send is recent.
NONCE_SEND_WINDOW_SEC = 120.0
_nonce_cache: dict = {"addr": None, "next": None, "send_ts": 0.0,
                      # prewarm (04.08): последний ЧЕЙН-вид pending-nonce + когда прочитан.
                      # Только ускоряет _pending_nonce (кэш вместо блокирующего RPC на пути
                      # выстрела); всю семантику local-bump-vs-chain решает прежний код.
                      "chain": None, "chain_ts": 0.0}
_owner_addr: str | None = None


def prewarm_nonce(addr: str) -> None:
    """Фоновое обновление чейн-вида pending-nonce (вызывается из цикла раз в NONCE_PREWARM_SEC).
    НЕ трогает send_ts (инвариант WC-урока: его пишет ТОЛЬКО _nonce_after_send — прогрев,
    который «благословлял» мёртвый бамп, уже убивал самовосстановление у wc). Ошибка чтения
    просто оставляет кэш старым — _pending_nonce тогда сходит в RPC сам."""
    try:
        pend = int(_rpc_write("eth_getTransactionCount", [addr, "pending"]), 16)
    except Exception:
        return
    if _nonce_cache["addr"] not in (None, addr):
        return
    _nonce_cache["addr"] = addr
    _nonce_cache["chain"] = pend
    _nonce_cache["chain_ts"] = time.monotonic()


class RpcVerdict(RuntimeError):
    """An in-body JSON-RPC error: the node RECEIVED the request and answered with a refusal.

    The distinction matters ONLY on the write path, and only for eth_sendRawTransaction: a verdict
    proves the tx was rejected and is NOT in any mempool, whereas a transport failure (hard
    deadline, reset socket, half-read response) proves nothing at all — the node may well have
    accepted the tx and simply failed to tell us. Subclasses RuntimeError so every existing
    `except RuntimeError` / `except Exception` site behaves exactly as before."""


def owner_address() -> str | None:
    """Checksummed address of the signing key, derived once and memoized."""
    global _owner_addr
    if _owner_addr is None:
        try:
            from eth_account import Account
            _owner_addr = Account.from_key(C.PRIVATE_KEY).address if C.PRIVATE_KEY else ""
        except Exception:
            _owner_addr = ""
    return _owner_addr or None


def _rpc_write(method: str, params: list, budget: float | None = None):
    """Single-endpoint JSON-RPC POST to C.RPC_WRITE under a HARD wall deadline.

    The wall cap is not optional: the stdlib socket timeout is per-recv, not total — this repo
    already learned that a trickling endpoint outlasts it (analysis/rpc.py). A browser UA is
    always sent (drpc/Cloudflare 403 any request without one — the flock lesson that killed a
    SEND on 2026-07-17). A node's in-body error is a VERDICT and propagates verbatim; the caller
    (not this layer) decides whether a verdict is fatal."""
    return _rpc_write_url(C.RPC_WRITE, method, params, budget)


def _rpc_write_url(url: str, method: str, params: list, budget: float | None = None):
    """То же, но на явный url — строительный блок параллельного залпа (_broadcast_raw).
    Транспорт keep-alive (analysis/rpc.py http_post_json): TLS-рукопожатие на каждом вызове
    съедало ~260мс из 275 всей трассы (замер 04.08) — на пути выстрела это львиная доля."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": method, "params": params}).encode()
    wall = C.RPC_HARD_TIMEOUT if budget is None else budget

    def _attempt():
        return http_post_json(url, body, C.RPC_TIMEOUT)

    d = _run_with_deadline(_attempt, wall)
    if d.get("error"):
        raise RpcVerdict(f"rpc {method}: {d['error']}")
    return d["result"]


def _pending_nonce(addr: str) -> int:
    """nonce = eth_getTransactionCount(addr,'pending') with a local counter layered on top: in a
    multi-target cascade the node's 'pending' does not yet reflect the tx we broadcast seconds ago,
    so take max(pending, local next) — but ONLY while that send is recent.

    The window is the whole point (lesson bought by wc-liquidator d757345). The local counter used
    to be monotonic: once it was ahead of the chain it stayed ahead FOREVER. A tx evicted from the
    mempool (replaced, underpriced, dropped on a node restart) sends the node's 'pending' back to
    N while we keep signing N+1 — every later shot lands past a hole the sequencer will never
    fill, so nothing mines again until the process is restarted. That is a silently disarmed
    liquidator, the worst failure mode this repo has.

    So: inside NONCE_SEND_WINDOW_SEC of a real broadcast, a local bump beats a lagging endpoint's
    count (the cascade case this cache exists for). Outside it the CHAIN WINS even though it is
    lower, and the stale bump is dropped — the bot heals itself within ~2 min. `send_ts` is
    stamped by _nonce_after_send and NOWHERE else: WC's follow-up bug was a prewarm path that also
    refreshed the timestamp, which kept re-blessing the dead bump and muted the self-heal
    permanently. Nothing here may refresh send_ts without an actual send."""
    fresh = (C.NONCE_PREWARM_SEC > 0 and _nonce_cache["addr"] == addr
             and _nonce_cache.get("chain") is not None
             and time.monotonic() - _nonce_cache.get("chain_ts", 0.0) <= 2 * C.NONCE_PREWARM_SEC)
    if fresh:
        pend = _nonce_cache["chain"]      # prewarm: кэш вместо ~50-250мс RPC на пути выстрела
    else:
        pend = int(_rpc_write("eth_getTransactionCount", [addr, "pending"]), 16)
        _nonce_cache["chain"], _nonce_cache["chain_ts"] = pend, time.monotonic()
        _nonce_cache["addr"] = addr if _nonce_cache["addr"] is None else _nonce_cache["addr"]
    nxt = _nonce_cache["next"]
    if _nonce_cache["addr"] != addr or nxt is None or nxt <= pend:
        return pend
    if time.monotonic() - _nonce_cache.get("send_ts", 0.0) <= NONCE_SEND_WINDOW_SEC:
        return nxt                      # still-propagating send: don't reuse its nonce
    print(f"  nonce resync down: dropping stale local bump {nxt} -> chain {pend} "
          f"(no send for {time.monotonic() - _nonce_cache.get('send_ts', 0.0):.0f}s)")
    _nonce_cache["next"] = pend
    return pend


def _nonce_after_send(addr: str, used: int) -> None:
    """Advance the local counter past the nonce we just broadcast — on success AND on an ambiguous
    outcome (see _sign_and_send): a tx that MAY be in the mempool must not have its nonce reused,
    or the two txs race and one is guaranteed to be wasted gas. The bump is self-correcting: if
    the tx really never landed, _pending_nonce drops it back to the chain value once the send
    window expires. This is the ONLY writer of send_ts."""
    _nonce_cache["addr"] = addr
    _nonce_cache["next"] = used + 1
    _nonce_cache["send_ts"] = time.monotonic()


# Свежий baseFee из цикла (gas_cost_usd читает его каждую итерацию по read-пути): кэш снимает
# ещё один блокирующий RPC с пути выстрела. Срок годности короткий (BASEFEE_CACHE_SEC, деф. 2.5с
# ≈ 2 блока): 2x-запас maxFee покрывает рост базы EIP-1559 на таком окне с большим запасом.
_basefee_cache: dict = {"wei": None, "ts": 0.0}


def _note_basefee(base_wei: int) -> None:
    _basefee_cache["wei"], _basefee_cache["ts"] = base_wei, time.monotonic()


def _tip_wei(net_usd: float | None, st: dict | None = None) -> int:
    """Чаевые за место в блоке (04.08: малые блоки упорядочены по tip — см. config.TIP_MODE).
    Платим долю приза, зажатую в [TIP_MIN, TIP_MAX]; перевод $ -> gwei через оценочный gasUsed
    и статичный HYPE_USD (обе величины грубые, но ошибка ±30% сдвигает tip, а не ломает его).

    Потолок по балансу: узел требует balance >= GAS_LIMIT*(2*base+tip) ЦЕЛИКОМ — щедрые чаевые
    на тощем кошельке превратили бы проходной выстрел в insufficient funds. Лучше выстрел без
    чаевых, чем никакого: тогда tip зажимается в остаток конверта (вплоть до 0)."""
    if C.TIP_MODE != "auto":
        return int(C.PRIORITY_GWEI * 1e9)
    budget_usd = max(0.0, net_usd or 0.0) * C.TIP_PRIZE_FRAC
    denom = C.HYPE_USD * C.GAS_UNITS_EST * 1e-9          # $ за 1 gwei чаевых на всю tx
    per_gas_gwei = budget_usd / denom if denom > 0 else 0.0
    tip_gwei = min(C.TIP_MAX_GWEI, max(C.TIP_MIN_GWEI, per_gas_gwei))
    bal = (st or {}).get("balance_hype")
    base = _basefee_cache["wei"]
    if bal is not None and base is not None and C.GAS_LIMIT > 0:
        afford_gwei = (bal * 1e18 / C.GAS_LIMIT - 2 * base) / 1e9
        tip_gwei = max(0.0, min(tip_gwei, afford_gwei))
    return int(tip_gwei * 1e9)


def _fee_params(tip_wei: int | None = None) -> tuple[int, int]:
    """EIP-1559 fee (HyperEVM is type-0x2; our 2026-07-19 canary went out exactly this way).
    maxPriorityFeePerGas: чаевые из _tip_wei (04.08 — премисса «tip ничего не покупает»
    опровергнута замером, малые блоки упорядочены по tip). maxFeePerGas = baseFee*2 + priority:
    maxFee is a CAP, not a payment (the block charges the actual base), so the 2x headroom is
    economically free and only protects against a baseFee rise between signing and inclusion.
    Falls back to eth_gasPrice if baseFee is unavailable."""
    priority = int(C.PRIORITY_GWEI * 1e9) if tip_wei is None else tip_wei
    age = time.monotonic() - _basefee_cache["ts"]
    if _basefee_cache["wei"] is not None and 0 < C.BASEFEE_CACHE_SEC and age <= C.BASEFEE_CACHE_SEC:
        return _basefee_cache["wei"] * 2 + priority, priority
    try:
        blk = _rpc_write("eth_getBlockByNumber", ["latest", False])
        base = int(blk["baseFeePerGas"], 16)
        _note_basefee(base)
        return base * 2 + priority, priority
    except Exception:
        return int(_rpc_write("eth_gasPrice", []), 16) + priority, priority


def _encode_liquidate(t: dict, ev: dict) -> str:
    """ABI-encode HyperLendLiquidator.liquidate(...) calldata IN PROCESS — byte-identical to what
    `cast calldata "<LIQUIDATE_SIG>" ...` produces (asserted in bot/test_executor.py against the
    real `cast` binary, including empty bytes, odd-word-length swap calldata, max uint256 and
    mixed-case addresses). Argument order mirrors the cast invocation the fallback path still uses:
    (collateral, debt, borrower, debtToCover, useFlashloan, swapTarget, swapCalldata, minProfit).

    Addresses are checksummed here: eth_abi rejects a non-checksummed mixed-case address outright,
    so this both normalizes caller input and keeps the encode from silently depending on case."""
    cd = ev["swap_calldata"] or "0x"
    raw = cd[2:] if cd[:2].lower() == "0x" else cd
    values = [
        to_checksum_address(t["coll_asset"]),
        to_checksum_address(t["debt_asset"]),
        to_checksum_address(t["borrower"]),
        int(ev["debt_to_cover"]),
        bool(C.USE_FLASHLOAN),
        to_checksum_address(ev["swap_target"]),
        bytes.fromhex(raw),
        int(ev["min_profit_wei"]),
    ]
    return LIQUIDATE_SELECTOR + abi_encode(LIQUIDATE_TYPES, values).hex()


class SendAmbiguous(RuntimeError):
    """The broadcast outcome is UNKNOWN: the request went out and the answer never came back.

    Carries `tx_hash` — the hash of the signed payload, which is known BEFORE the wire and is
    exactly the hash the tx would have if the node did accept it. The caller must file this as
    pending on that hash (see _fire_raw): it is the only way an ambiguous send can still be
    settled by a receipt instead of vanishing."""

    def __init__(self, tx_hash: str, cause: str):
        super().__init__(cause)
        self.tx_hash = tx_hash
        self.cause = cause


class SendUndelivered(RuntimeError):
    """The node refused the tx with an explicit verdict — it is in NO mempool and burns no gas."""


# A node that answers "already known" / "known transaction" is telling us the tx IS in its pool:
# that is a DELIVERED send whose ack we merely duplicated, not a rejection.
_ALREADY_IN_POOL = ("already known", "known transaction", "already in the pool")


def _broadcast_raw(raw_hex: str) -> str:
    """Залп eth_sendRawTransaction во все BROADCAST_RPCS одновременно; первый ack побеждает.

    Тот же raw payload на N узлов — это ОДНА транзакция (одинаковый хэш): дубли гасятся полом
    «already known», который здесь равен доставке. Семантика исходов сохранена бит-в-бит:
      * хоть один узел принял (или уже знает) -> hash;
      * ВСЕ узлы дали явный вердикт-отказ -> RpcVerdict (первый; caller превратит в
        SendUndelivered — tx ни в одном мемпуле);
      * иначе (все обрывы / вердикты вперемешку с обрывами) -> транспортная ошибка
        (caller превратит в SendAmbiguous — доставку исключить нельзя).
    Откат HL_PARALLEL_BROADCAST=0 — прежний одиночный C.RPC_WRITE."""
    urls = C.BROADCAST_RPCS if C.PARALLEL_BROADCAST else [C.RPC_WRITE]
    if len(urls) == 1:
        # одиночный путь = откат-путь: идёт через исторический сим _rpc_write (его же
        # подменяют тесты write-семантики), поведение бит-в-бит прежнее
        if urls[0] == C.RPC_WRITE:
            return _rpc_write("eth_sendRawTransaction", [raw_hex])
        return _rpc_write_url(urls[0], "eth_sendRawTransaction", [raw_hex])
    results: dict = {}
    winner = threading.Event()

    def one(u: str) -> None:
        try:
            results[u] = ("ok", _rpc_write_url(u, "eth_sendRawTransaction", [raw_hex]))
            winner.set()
        except RpcVerdict as e:
            if any(m in str(e).lower() for m in _ALREADY_IN_POOL):
                results[u] = ("ok", None)         # узел уже держит tx = доставлено
                winner.set()
            else:
                results[u] = ("verdict", e)
        except Exception as e:                     # noqa: BLE001 — транспорт любой природы
            results[u] = ("err", e)
        if len(results) == len(urls):
            winner.set()                          # все ответили (пусть и плохо) — не ждать стену

    threads = [threading.Thread(target=one, args=(u,), daemon=True) for u in urls]
    for t in threads:
        t.start()
    winner.wait(C.RPC_HARD_TIMEOUT + 1.0)
    oks = [v for k, v in results.items() if v[0] == "ok"]
    if oks:
        return next((v[1] for v in oks if v[1]), None) or ""   # хэш от узла; "" -> локальный хэш caller'а
    if len(results) == len(urls) and all(v[0] == "verdict" for v in results.values()):
        raise next(v[1] for v in results.values())             # единогласный отказ = вердикт
    errs = [v[1] for v in results.values() if v[0] == "err"]
    raise (errs[0] if errs else TimeoutError(f"broadcast: no ack from {len(urls)} urls"))


def _signed_tx_hash(signed) -> str:
    """0x-hash of a signed payload, computed locally — available BEFORE the broadcast."""
    h = signed.hash
    return h.to_0x_hex() if hasattr(h, "to_0x_hex") else "0x" + bytes(h).hex()


def _sign_and_send(calldata: str, tip_wei: int | None = None) -> str:
    """Sign an EIP-1559 tx locally and broadcast it. Returns the tx hash.

    THREE outcomes, and conflating the last two loses real money:
      * accepted -> return the hash;
      * SendUndelivered -> the node gave an in-body JSON-RPC VERDICT (RpcVerdict: nonce too low,
        invalid sender, underpriced, ...). The tx reached the node and was refused, so it is in no
        mempool, burns no gas, and can never produce a receipt. Nothing to track;
      * SendAmbiguous -> ANY transport failure (hard wall deadline, reset socket, truncated
        response). This says NOTHING about delivery: a single-endpoint POST that trips our 10s
        wall may well have been accepted by a node that was merely slow to answer. Treating it as
        a non-send is how a live tx goes untracked — its gas escapes the daily cap, a revert never
        reaches consec_reverts, and DEDUP_SEC frees the borrower in 60s for a SECOND liquidation
        of a position the first tx already cured. So the hash is computed before the wire and the
        caller files it as pending.
    The local nonce advances for BOTH accepted and ambiguous (a maybe-sent tx owns its nonce);
    only a verdict leaves it untouched, since a verdict guarantees no hole.

    `to` is CHECKSUMMED explicitly: eth_account will happily sign a lowercase `to`, but a
    mixed-case non-checksummed one raises — and this exact class of bug killed a WC shot. Doing it
    here means C.CONTRACT can be set in any case without the fire path caring."""
    from eth_account import Account
    addr = owner_address()
    if not addr:
        raise RuntimeError("no signing key (HL_PRIVATE_KEY / HL_KEYFILE)")
    max_fee, priority = _fee_params(tip_wei)
    nonce = _pending_nonce(addr)
    tx = {"chainId": C.CHAIN_ID, "nonce": nonce, "to": to_checksum_address(C.CONTRACT),
          "value": 0, "gas": C.GAS_LIMIT, "maxFeePerGas": max_fee,
          "maxPriorityFeePerGas": priority, "data": calldata}
    signed = Account.sign_transaction(tx, C.PRIVATE_KEY)
    raw = signed.raw_transaction
    raw_hex = raw.to_0x_hex() if hasattr(raw, "to_0x_hex") else "0x" + raw.hex()
    local_hash = _signed_tx_hash(signed)      # known BEFORE the wire — survives an ambiguous send
    try:
        txh = _broadcast_raw(raw_hex) or local_hash
    except RpcVerdict as e:
        if any(m in str(e).lower() for m in _ALREADY_IN_POOL):
            _nonce_after_send(addr, nonce)    # the node HAS it: delivered, just re-acked
            return local_hash
        raise SendUndelivered(str(e)) from e   # refused: no mempool entry, no gas, no receipt
    except Exception as e:
        # transport-level: delivery unknown. Own the nonce and hand the caller the hash to track.
        _nonce_after_send(addr, nonce)
        raise SendAmbiguous(local_hash, str(e)) from e
    _nonce_after_send(addr, nonce)
    return txh


# --------------------------------------------------------------------------- fire
def _fee_args() -> list[str]:
    # HyperEVM priority fee is non-operative; keep it ~0 to avoid overpaying (no ordering effect).
    return ["--priority-gas-price", str(int(C.PRIORITY_GWEI * 1e9))]


def _sign_args() -> list[str]:
    """Key on argv — the ONLY form foundry 1.7.1 accepts short of a keystore. See _fire_cast."""
    return ["--private-key", C.PRIVATE_KEY]


def fire(t: dict, ev: dict, st: dict, now_ts: float, gas_usd: float) -> None:
    key = t["borrower"]
    nets = f"${ev['net_usd']:+,.1f}"
    hdr = (f"HF={t['hf']:.4f} cf={t['close_factor']:.0%} {t['coll_sym']}->{t['debt_sym']} "
           f"cover={t['debt_to_cover']} net={nets} impact={ev['impact']*100:.2f}% "
           f"{t['borrower'][:10]}…")
    if C.DRY_RUN or not C.CONTRACT:
        print(f"  DRY_RUN: would liquidate {hdr}; guard=DRY, NOT sent "
              f"(contract={'set' if C.CONTRACT else 'none'})")
        return
    if C.RAW_TX:
        _fire_raw(t, ev, st, now_ts, gas_usd, key, hdr)
    else:
        _fire_cast(t, ev, st, now_ts, gas_usd, key, hdr)


def _fire_raw(t: dict, ev: dict, st: dict, now_ts: float, gas_usd: float,
              key: str, hdr: str) -> None:
    """HL_RAW_TX=1: encode -> sign -> broadcast -> RETURN. No receipt wait, ever. The tx is filed
    as "pending" and settled by _check_pending() on a later pass, so the next target in a cascade
    is evaluated immediately instead of ~4.4s later."""
    maybe = ""
    try:
        calldata = _encode_liquidate(t, ev)
        txh = _sign_and_send(calldata, tip_wei=_tip_wei(ev.get("net_usd"), st))
    except SendAmbiguous as e:
        # DELIVERY UNKNOWN — book it exactly like a successful send. A tx we cannot rule out is
        # live must be tracked: untracked, its gas escapes the daily cap, a revert never reaches
        # consec_reverts, and the borrower is freed by DEDUP_SEC (60s) long before anyone could
        # notice — a second liquidation of a position the first tx may already have cured. The
        # hash was computed from the signed payload, so a receipt still settles this record; if
        # the tx truly never landed, _check_pending's stale wall releases it after 600s.
        txh, maybe = e.tx_hash, " (delivery UNCONFIRMED)"
        print(f"  ambiguous broadcast {txh[:14]}…: {e.cause}; tracking as pending")
    except Exception as e:
        # SEND ERROR — signing/encoding failed, or the node returned an explicit VERDICT
        # (SendUndelivered): nothing executed on-chain and nothing can. NOT a revert — feeding it
        # to consec_reverts would let an RPC brownout trip the kill-switch and take the bot down
        # for the whole crash window. Only receipt.status == 0 is a revert (see _check_pending).
        st["sent"][key] = {"ts": now_ts, "status": "send_error", "tx": f"senderr:{e}"[:120]}
        alert_async(f"⚠️ send error {hdr} (not counted as revert): {e}")
        return
    st["fires"] += 1
    # Provisional gas charge, exactly ONCE per tx. _check_pending reverses this figure and books
    # the real gasUsed*effectiveGasPrice when the receipt lands, so the daily cap can never be
    # double-charged for one shot. `gas_usd` is carried on the record to make the undo exact, and
    # `gas_day` records WHICH day was charged so a settle after the UTC roll cannot refund a
    # charge into the fresh day's budget (see _settle_gas).
    st["gas_usd"] += gas_usd
    st["sent"][key] = {"ts": now_ts, "status": "pending", "tx": txh, "gas_usd": gas_usd,
                       "gas_day": st.get("day")}
    # Alert AFTER the broadcast and fire-and-forget: Telegram must never sit between the
    # liquidation decision and the wire, nor delay the next shot in a cascade.
    alert_async(f"🔫 LIQUIDATE {hdr}, sent: {txh}{maybe}")


def _fire_cast(t: dict, ev: dict, st: dict, now_ts: float, gas_usd: float,
               key: str, hdr: str) -> None:
    """Fallback path (HL_RAW_TX=0, the current default). Behaviour is deliberately UNCHANGED from
    the long-running production path — including the blocking receipt wait — so flipping the flag
    is the only variable when this is verified live.

    The key goes back on argv. It was briefly moved to ETH_PRIVATE_KEY to keep it out of `ps` —
    but foundry 1.7.1 has NO such env binding (`cast send --help` offers ETH_KEYSTORE /
    ETH_KEYSTORE_ACCOUNT / ETH_PASSWORD only), so cast fell back to "Error accessing local
    wallet", every fire returned non-zero, and the caller reads non-zero as a REVERT: three
    fabricated reverts in one cascade trip the kill-switch and exit the process. The leak is real
    but it is a local-user read; a silently disarmed liquidator is worse. Closing it properly
    means a keystore (ETH_KEYSTORE + ETH_PASSWORD) — a separate, verified change, and moot once
    HL_RAW_TX=1 makes this path dead code."""
    args = ["cast", "send", C.CONTRACT, LIQUIDATE_SIG,
            t["coll_asset"], t["debt_asset"], t["borrower"], str(ev["debt_to_cover"]),
            "true" if C.USE_FLASHLOAN else "false", ev["swap_target"], ev["swap_calldata"],
            str(ev["min_profit_wei"]),
            "--gas-limit", str(C.GAS_LIMIT), "--rpc-url", C.RPC_WRITE] + _fee_args() + _sign_args()
    st["fires"] += 1
    st["gas_usd"] += gas_usd
    # ALL alerts on the fire path are fire-and-forget (alert_async): the old synchronous
    # pre-broadcast alert made EVERY shot pay a Telegram HTTPS round-trip before `cast send`
    # (unbounded on a trickling api.telegram.org); the post-result alerts likewise must not delay
    # the NEXT shot in a multi-target cascade. Broadcast never waits on Telegram.
    alert_async(f"🔫 LIQUIDATE {hdr}, sending…")
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=90)
        out = (r.stdout or "") + (r.stderr or "")
        reverted = (r.returncode != 0) or ("status" in out and "0 (failed)" in out)
        status = "revert" if reverted else "ok"
        st["sent"][key] = {"ts": now_ts, "status": status, "tx": out[-80:].strip()}
        if reverted:
            st["consec_reverts"] += 1
            st["reverts"] += 1
            alert_async(f"❌ revert {hdr}: {out[-200:]}")
        else:
            st["consec_reverts"] = 0
            alert_async(f"✅ liq ok {hdr}: {out[-160:]}")
    except Exception as e:
        st["consec_reverts"] += 1
        st["reverts"] += 1
        st["sent"][key] = {"ts": now_ts, "status": "revert", "tx": f"err:{e}"}
        alert_async(f"❌ cast error {hdr}: {e}")


# --------------------------------------------------------------------------- receipt reaping
PENDING_STALE_SEC = 600.0     # unmined this long -> flag a possible stuck nonce
STALE_POLL_SEC = 60.0         # a stale tx is still reaped, but at a slow cadence (see below)
_SENT_RETENTION_SEC = 86400.0


def _rcpt_int(v) -> int:
    """Parse a receipt integer field. The JSON-RPC spec says quantities are hex strings, but
    normalizing proxies and some load balancers hand back a plain JSON int — accept both. Raises
    on anything else (None, "junk"), which is what the callers key their fallbacks off."""
    if isinstance(v, bool):
        raise TypeError("bool is not a receipt quantity")
    if isinstance(v, int):
        return v
    return int(v, 16)


def _rcpt_status(rcpt: dict) -> int | None:
    """Receipt status, or None when unparsable (status:null and friends). Returning None instead
    of raising is deliberate: a TypeError here would escape into the hot loop and kill the pass —
    the silent-zombie failure mode (heartbeat alive, detection dead).

    An INTEGER status (status: 1) is a perfectly good answer, not garbage: read as unparsable it
    left every tx pending forever, so a win was never booked and the borrower stayed blocked until
    the stale wall — the bot going quiet against a node that did nothing wrong."""
    try:
        return _rcpt_int(rcpt.get("status"))
    except (TypeError, ValueError):
        return None


def _settle_gas(st: dict, rec: dict, rcpt: dict) -> float:
    """Swap the provisional gas charge for the ACTUAL cost of this tx. Runs exactly once per tx,
    at the moment the receipt is decided — so a tx is never charged twice, and never charged at
    an estimate when the real number is available.

    The undo is scoped to the DAY the charge was made. _roll_day zeroes gas_usd at the UTC
    boundary; an unconditional refund afterwards subtracts yesterday's estimate from today's fresh
    counter and drives it NEGATIVE — measured at -$4.94 with 8 in-flight shots at $0.62, i.e. the
    $5 cap silently became a ~$10 cap on exactly the day after a busy one. A charge belonging to a
    closed day is left alone: that day's budget already absorbed it and cannot be re-opened.
    gas_usd is also clamped at zero, so no other refund path can ever buy extra budget either.

    effectiveGasPrice has NO silent default. `.get(field, "0x0")` turned a receipt that omits it
    into a $0.00 shot — the daily cap goes blind while real gas burns — where the same missing
    field on gasUsed correctly fell back to the estimate. Missing means unknown, not free."""
    try:
        actual = (_rcpt_int(rcpt["gasUsed"]) * _rcpt_int(rcpt["effectiveGasPrice"])
                  / 1e18 * C.HYPE_USD)
    except Exception:
        actual = rec.get("gas_usd", 0.0)              # unreadable receipt: keep the estimate
    if "gas_day" in rec and rec["gas_day"] != st.get("day"):
        print(f"  gas settle ${actual:.4f} on a tx charged {rec['gas_day']} (today "
              f"{st.get('day')}): day closed, leaving today's counter untouched")
        return actual
    st["gas_usd"] = max(0.0, st["gas_usd"] - rec.get("gas_usd", 0.0)) + actual
    return actual


def _check_pending(rpc: Rpc, st: dict, now_ts: float) -> None:
    """Settle txs broadcast on earlier passes — the other half of the non-blocking fire path.
    Called once per iteration next to the hot-set update, BEFORE any new shot, so a revert that
    just landed feeds the kill-switch before we fire again this pass.

    Records are grouped by tx hash so the gas accounting and the revert counters run once per TX,
    not once per key. Each tx settles inside its own try/except: one malformed answer from one
    endpoint must never take down the pass (this loop is the bot's only liveness).

    TWO ordering rules earn their keep here:
      1. THE STALE WALL IS APPLIED BEFORE THE READ, never inside a "cleanly not mined" branch. It
         used to live under `if not rcpt:`, reachable only when an endpoint answered properly — so
         while the read kept THROWING (the exact situation in which a tx is most likely stuck) the
         record stayed "pending", and recently_fired() blocks a pending target regardless of age.
         One dead endpoint therefore locked a borrower out of the fire path until the 24h purge.
      2. STALE RECORDS ARE STILL REAPED. "stale" means unmined for 10 min, NOT decided: HyperEVM
         can mine such a tx later. Dropping it from the poll (the previous behaviour) meant its
         provisional gas was never reconciled against the real receipt and a late revert never
         reached consec_reverts, while the borrower had already been released — so the kill-switch
         could not see the very failure pattern it exists to stop. They keep being polled until
         the 24h retention purge; the alert fires once, on the transition."""
    by_tx: dict[str, list[str]] = {}
    for key, rec in st.get("sent", {}).items():
        if rec.get("status") in ("pending", "stale") and str(rec.get("tx", "")).startswith("0x"):
            by_tx.setdefault(rec["tx"], []).append(key)
    for txh, keys in by_tx.items():
        rec = st["sent"][keys[0]]
        # Age check FIRST — outside the try, so it also fires when the read below keeps throwing.
        # Past the wall it is very likely a stuck nonce (a gap left by an earlier tx the sequencer
        # never saw) — surface it, the operator must look, and unblock the borrower.
        if rec.get("status") == "pending" and now_ts - rec.get("ts", 0) > PENDING_STALE_SEC:
            for k in keys:
                st["sent"][k]["status"] = "stale"
            alert_async(f"⚠️ tx unmined 10min (stuck nonce?): {txh}")
        # A stale tx is polled at STALE_POLL_SEC, not every iteration: it stays in the journal for
        # 24h, and at hot-loop cadence that would be tens of thousands of receipt reads for one
        # stuck tx. A PENDING tx keeps polling every pass — its verdict gates the kill-switch
        # before the next shot, so that latency is the point.
        if rec.get("status") == "stale" and now_ts - rec.get("last_poll", 0) < STALE_POLL_SEC:
            continue
        for k in keys:
            st["sent"][k]["last_poll"] = now_ts
        try:
            rcpt = rpc.call("eth_getTransactionReceipt", [txh])
            if not rcpt:
                continue                     # not mined yet — re-read next pass
            status = _rcpt_status(rcpt)
            if status is None:
                # undecided, NOT reverted: leave it pending and re-read next pass. Booking this
                # as a revert would let one node answering garbage trip the kill-switch.
                print(f"  pending {txh[:14]}…: unparsable receipt status; retrying next pass")
                continue
            actual = _settle_gas(st, rec, rcpt)
            if status == 1:
                st["consec_reverts"] = 0
                for k in keys:
                    st["sent"][k] = {"ts": rec["ts"], "status": "ok", "tx": txh}
                alert_async(f"✅ liq ok {txh} (gas ${actual:.4f})")
            else:
                st["consec_reverts"] += 1
                st["reverts"] += 1
                for k in keys:
                    st["sent"][k] = {"ts": rec["ts"], "status": "revert", "tx": txh}
                alert_async(f"❌ revert {txh} (gas ${actual:.4f})")
        except Exception as e:
            print(f"  pending check fail {txh[:14]}… (retry next pass): {e}")
    for key, rec in list(st.get("sent", {}).items()):
        if now_ts - rec.get("ts", 0) > _SENT_RETENTION_SEC:
            del st["sent"][key]


# --------------------------------------------------------------------------- target processing
# --------------------------------------------------------------------------- oracle-update trigger
# 04.08 (поправка 4237b15): добыча берётся полем через ~17с ПОСЛЕ ValueUpdate — окно наше,
# если обнаружение событийное. Один getLogs по двум адаптерам за итерацию; курсор двигают
# только сами события (пустой ответ не трогает from — повторный запрос идемпотентен).
_upd_watch: dict = {"from": None}


def poll_oracle_updates(rpc: Rpc) -> int:
    """Сколько НОВЫХ ValueUpdate с прошлого опроса. Ошибка чтения = 0 (триггер — ускоритель,
    его отказ не смеет ронять итерацию: штатный опрос горячих никуда не девается)."""
    if not C.UPDATE_TRIGGER:
        return 0
    try:
        if _upd_watch["from"] is None:
            _upd_watch["from"] = rpc.block_number() + 1
            return 0
        logs = rpc.get_logs(C.ORACLE_ADAPTERS, [C.TOPIC_VALUE_UPDATE],
                            _upd_watch["from"], rpc.block_number())
        if logs:
            _upd_watch["from"] = max(int(l["blockNumber"], 16) for l in logs) + 1
        return len(logs)
    except Exception:
        return 0


# --------------------------------------------------------------------------- pre-arm
# Кромка hot-set котируется фоном, выстрел берёт готовый (t, ev) из кэша — LiquidSwap-квота
# (1.7-3.5с замер 30.07) уходит с горячего пути. Один фоновый поток единовременно (2 ядра).
_prearm: dict = {}                  # borrower -> {"t":..., "ev":..., "ts": monotonic}
_prearm_lock = threading.Lock()
_prearm_busy = threading.Event()


def _prearm_get(borrower: str, hf: float) -> tuple[dict, dict] | None:
    """Свежий армленный (t, ev), если режим close-factor тот же, что при котировке (HF>=0.95:
    ниже CF переключается 50%->100% и армленный размер перестаёт соответствовать)."""
    if not C.PREARM or hf < 0.95:
        return None
    with _prearm_lock:
        rec = _prearm.get(borrower)
        if rec and time.monotonic() - rec["ts"] <= C.PREARM_TTL:
            return rec["t"], rec["ev"]
        if rec:
            del _prearm[borrower]
    return None


def _prearm_drop(borrower: str) -> None:
    with _prearm_lock:
        _prearm.pop(borrower, None)


def _prearm_quote(rpc: Rpc, book: dict, borrowers: list[str], accounts: dict,
                  reserves_cfg: dict, gas_usd: float) -> None:
    """Фоновый поток: refine + evaluate на бритом размере, профитные — в кэш."""
    try:
        targets, _ = refine(rpc, book, accounts, borrowers, reserves_cfg,
                            min_debt_usd=C.MIN_DEBT_USD, watch_hf=C.PREARM_HF,
                            report_hf=C.PREARM_HF, retries=1)
        for t in targets:
            shaved = _resize(t, *C.PREARM_SHAVE) or t
            ev = evaluate(shaved, gas_usd)
            if ev and "skip" not in ev and ev.get("profitable"):
                with _prearm_lock:
                    _prearm[t["borrower"]] = {"t": shaved, "ev": ev, "ts": time.monotonic()}
                print(f"  ⚡ armed {t['borrower'][:10]}… HF={t['hf']:.4f} "
                      f"{shaved['coll_sym']}->{shaved['debt_sym']} net=${ev['net_usd']:+,.1f}")
    except Exception as e:                    # noqa: BLE001 — фон не смеет ронять процесс
        print(f"  prearm err: {e}")
    finally:
        _prearm_busy.clear()


def prearm_tick(rpc: Rpc, book: dict, accounts: dict, reserves_cfg: dict,
                gas_usd: float) -> None:
    """Инлайн-вход: выбрать кромку (1.0 <= HF < PREARM_HF, долг >= MIN_DEBT), которой нет
    свежего арма, и отдать одному фоновому потоку. HF<1 сюда не попадает — это живая цель,
    её берёт боевой путь этой же итерации."""
    if not (C.PREARM and C.CONTRACT) or _prearm_busy.is_set():
        return
    now = time.monotonic()
    edge = []
    for b, a in accounts.items():
        hf = a["health_factor"] / 1e18
        if not (1.0 <= hf < C.PREARM_HF and a["health_factor"] < HF_INFINITY):
            continue
        if a["total_debt_base"] / ORACLE_BASE_UNIT < C.MIN_DEBT_USD:
            continue
        with _prearm_lock:
            rec = _prearm.get(b)
        if rec and now - rec["ts"] < C.PREARM_REFRESH_SEC:
            continue
        edge.append((a["total_debt_base"], b))
    if not edge:
        return
    edge.sort(reverse=True)
    picked = [b for _, b in edge[:C.PREARM_MAX]]
    _prearm_busy.set()
    threading.Thread(target=_prearm_quote,
                     args=(rpc, book, picked, accounts, reserves_cfg, gas_usd),
                     daemon=True).start()


def process_targets(rpc: Rpc, targets: list, st: dict, now_ts: float, gas_usd: float) -> list[str]:
    """Run the (unchanged) per-target fire path over `targets`: dedup -> fresh-HF re-check ->
    evaluate() (with the alt-leg no-route fallback) -> profitability gate -> fire(). Returns the
    borrowers actually fired this call (so the loop can drop them from the hot set until the
    rolling sweep re-seeds them). Lifted verbatim out of once() so the full-book scan AND the live
    hot-set loop share ONE fire path — economics, guards, alerts, RACE/dedup all byte-identical."""
    fired: list[str] = []
    for t in sorted(targets, key=lambda x: -(x.get("net_bonus_usd") or 0)):
        key = t["borrower"]
        if recently_fired(st, key, now_ts):
            continue
        hf_now = fresh_hf(rpc, key)
        if hf_now is not None and hf_now >= 1.0:
            print(f"  skip {key[:10]}…: HF cured to {hf_now:.4f} before fire")
            continue
        armed = _prearm_get(key, hf_now if hf_now is not None else t["hf"])
        if armed is not None:
            # выстрел с готовой котировкой: LiquidSwap-квота (1.7-3.5с) уже оплачена фоном.
            # Кэш профитный по построению; экономику на живом размере страхует on-chain
            # min_profit_wei — устаревшая котировка ревертит, а не теряет деньги.
            t, ev = armed
            print(f"  ⚡ arm-hit {key[:10]}…: котировка из кэша")
        else:
            ev = evaluate(t, gas_usd)
        # no route on the primary collateral (e.g. exotic Pendle PT) -> try the runner-up leg
        if ev and "skip" in ev and "no LiquidSwap route" in ev["skip"] and t.get("alt"):
            print(f"  skip {key[:10]}… {t['coll_sym']}->{t['debt_sym']}: {ev['skip']}; "
                  f"falling back to {t['alt']['coll_sym']}")
            t = t["alt"]
            ev = evaluate(t, gas_usd)
        if ev is None:
            continue
        if "skip" in ev:
            print(f"  skip {key[:10]}… {t['coll_sym']}->{t['debt_sym']}: {ev['skip']}")
            continue
        nets = f"${ev['net_usd']:+,.1f}"
        chunk = f" chunk={ev['f']:.0%}" if ev.get("f", 1.0) < 1.0 else ""
        print(f"  target {key[:10]}… HF={t['hf']:.4f} {t['coll_sym']}->{t['debt_sym']} "
              f"cover=${ev.get('repaid_usd') or t['repaid_usd']:,.0f}{chunk} net={nets} "
              f"impact={ev['impact']*100:.2f}% profitable={ev['profitable']}")
        if ev["profitable"]:
            # газ-гард ПЕРЕД каждым выстрелом (force: без троттла — решение принимается сейчас).
            # Недофинансированный кошелёк = нода отвергнет tx; лучше один явный алерт, чем
            # шторм send-error посреди каскада (урок katana).
            if not C.DRY_RUN and not check_balance(st, now_ts, force=True):
                print(f"  skip {key[:10]}… — недостаточно газа на EOA (см. ⛽-алерт)")
                continue
            fire(t, ev, st, now_ts, gas_usd)
            _prearm_drop(key)     # использованный арм не смеет пережить свой выстрел
            fired.append(key)
    return fired


# --------------------------------------------------------------------------- pass / loop
def once(st: dict | None = None, book: dict | None = None) -> int:
    own = st is None
    if own:
        st = load_state()
    now_ts = time.time()
    _roll_day(st, datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    rpc = Rpc(C.READ_RPCS)
    if book is None:
        book = load_book()

    # scan() mutates `book` in place (borrowers/last_block/configs) and returns the same object,
    # so there is nothing to copy back — `book` is already current for save_book below.
    if not C.DRY_RUN and st.get("sent"):
        _check_pending(rpc, st, now_ts)      # settle earlier broadcasts before this pass shoots
    r = scan(rpc, book, min_debt_usd=C.MIN_DEBT_USD, watch_hf=C.WATCH_HF, report_hf=C.REPORT_HF)
    st["passes"] += 1

    ok, reason = guard_ok(st)
    targets = r["targets"]
    print(f"[{time.strftime('%H:%M:%S')}] block {r['block']} | book {r['n_book']} | "
          f"positions {r['n_positions']} | risk(HF<{C.REPORT_HF}) {len(r['risk'])} | "
          f"targets(HF<1) {len(targets)} | guard={'OK' if ok else 'STOP('+reason+')'} "
          f"(DRY_RUN={'on' if C.DRY_RUN else 'OFF'}, contract={'set' if C.CONTRACT else 'none'})")
    # visibility into the near-edge watch set (the crash-spillover candidates)
    for t in sorted(r["risk"], key=lambda x: x["hf"])[:6]:
        print(f"    watch hf={t['hf']:.4f} debt=${t['total_debt_usd']:,.0f} "
              f"{t['coll_sym']}->{t['debt_sym']} {t['borrower'][:10]}…")

    if not ok and not C.DRY_RUN:
        if own:
            save_state(st)
        raise GuardTripped(reason)

    gas_usd = gas_cost_usd(rpc)
    process_targets(rpc, targets, st, now_ts, gas_usd)
    save_book(book)          # persist the borrower book so it survives restarts
    if own:
        save_state(st)
    return len(targets)


# --------------------------------------------------------------------------- amortized hot loop
def _hot_iteration(rpc: Rpc, book: dict, st: dict, hs: dict, gas_usd: float,
                   reserves_cfg: dict) -> dict:
    """ONE amortized loop iteration — the core of the redesign. In a single bounded getUserAccount-
    Data sweep it reads (hot-set ∪ next rolling cursor chunk), then:
      a. re-polls every hot-set member (so a cross to HF<1 is caught within ONE iteration),
      b. advances the full-book cursor by SWEEP_CHUNK (refreshing hot-set membership across the
         whole 25k book every ~ceil(N/CHUNK) iterations),
      c. fires any HF<1 through the unchanged refine()->process_targets() path,
      d. drops fired borrowers from the hot set (re-seeded when the sweep next reaches them).
    Returns a status dict for the per-iteration log line. Does NOT raise on a tripped guard — it
    reports guard state so the caller logs the status line FIRST (the log must never go silent),
    then kills. RPC is bounded (hardened Rpc + multicall retries=1), so no endpoint hang can block
    this for more than ~a few * hard_timeout seconds — never the multi-minute silence that used to
    false-fire the deadman."""
    now_ts = time.time()
    # Settle anything broadcast on an earlier pass FIRST: fire() no longer waits for receipts, so
    # this is where wins/reverts, the gas reconciliation and the kill-switch counters are booked.
    # Ordering matters — a revert that just landed must feed guard_ok() BEFORE we shoot again.
    if not C.DRY_RUN and st.get("sent"):
        _check_pending(rpc, st, now_ts)
    borrowers = book["borrowers"]
    hot = set(hs["hot"])
    # Курс катим не каждую итерацию: горячие важнее полноты книги, потому что именно они
    # пересекают HF<1 в ближайшие секунды. Итерации без чанка стоят втрое дешевле и дают
    # горячим опрос чаще блока чейна (см. C.SWEEP_EVERY).
    tick = int(hs.get("tick", 0)) + 1
    hs["tick"] = tick
    do_sweep = (C.SWEEP_EVERY <= 1) or (tick % C.SWEEP_EVERY == 0)
    # Событийный триггер (04.08): свежий ValueUpdate => эта итерация hot-only (чанк курсора
    # подождёт — цена только что переставила HF всей горячей книги, дорога каждая сотня мс).
    n_upd = poll_oracle_updates(rpc)
    if n_upd:
        do_sweep = False
        print(f"  ⚡ oracle-upd x{n_upd}: hot-only проход")
    if do_sweep:
        chunk, new_cursor, wrapped = next_chunk(borrowers, hs["cursor"], C.SWEEP_CHUNK)
    else:
        chunk, new_cursor, wrapped = [], hs["cursor"], False
    to_read = list(dict.fromkeys(list(hot) + chunk))       # hot members re-read EVERY iteration
    accounts = sweep_accounts(rpc, to_read, retries=1)

    tgt_borrowers = targets_from_accounts(accounts, C.MIN_DEBT_USD)
    ok, reason = guard_ok(st)
    fired: list[str] = []
    if tgt_borrowers and (ok or C.DRY_RUN):
        # size ONLY the HF<1 borrowers (cheap) via the identical refine() sizing, then the
        # identical process_targets() fire path — economics/guards/alerts byte-identical.
        targets, _risk = refine(rpc, book, accounts, tgt_borrowers, reserves_cfg,
                                min_debt_usd=C.MIN_DEBT_USD, watch_hf=C.HOT_HF, report_hf=C.HOT_HF,
                                retries=1)
        fired = process_targets(rpc, targets, st, now_ts, gas_usd)

    hot = update_hotset(hot, accounts, C.HOT_HF, C.MIN_DEBT_USD, drop=set(fired))
    hs["cursor"] = new_cursor
    hs["hot"] = sorted(hot)

    # pre-arm кромки: фоновые котировки для 1.0<=HF<PREARM_HF (сам троттлится и не блокирует)
    if not C.DRY_RUN:
        try:
            prearm_tick(rpc, book, accounts, reserves_cfg, gas_usd)
        except Exception as e:
            print(f"  prearm tick err: {e}")

    hfs = [a["health_factor"] / 1e18 for a in accounts.values() if a["health_factor"] < HF_INFINITY]
    return {"n_read": len(accounts), "n_hot": len(hot), "n_targets": len(tgt_borrowers),
            "fired": len(fired), "cursor": new_cursor, "wrapped": wrapped, "chunk": len(chunk),
            "oracle_upd": n_upd,
            "n_book": len(borrowers), "min_hf": min(hfs) if hfs else None,
            "guard_ok": ok, "reason": reason}


def _run_discovery(rpc: Rpc, book: dict) -> bool:
    """End-of-cycle discovery: merge new Borrow-log borrowers and advance the checkpoint (bounded
    getLogs, HL_INCR_WINDOW back from head; the hardened rpc caps any endpoint hang). Run once per
    full-book cycle (cursor wrap) — a brand-new borrower is healthy at borrow time, so a few-minute
    add latency is harmless, and it keeps getLogs off the hot path.

    NEVER raises; returns True when the checkpoint advanced. On a persistent getLogs failure
    (get_logs_chunked now gives up after a bounded number of consecutive failed windows instead of
    spinning the loop forever) the cycle is SKIPPED with one clear log line: borrowers/last_block
    stay UNTOUCHED, so the un-scanned range is re-scanned from the same checkpoint on the next
    cursor wrap (no Borrow event can be silently lost) and the hot loop keeps ticking."""
    try:
        to = rpc.block_number()
        merged = sorted(discover_borrowers(rpc, book, to))
    except Exception as e:
        print(f"discovery skipped this cycle (checkpoint block {book.get('last_block')} kept, "
              f"range retried next wrap): {e}")
        return False
    book["borrowers"] = merged
    book["last_block"] = to
    return True


def _log_iter(status: dict, dt: float) -> None:
    """The compact status line emitted EVERY iteration — this is what keeps the log ticking so the
    600s deadman never false-fires (the root-cause fix). Also the operator's live health view."""
    ts = time.strftime("%H:%M:%S")
    if "err" in status:
        print(f"[{ts}] hot-set iteration ERROR ({dt:.1f}s): {status['err']}")
        return
    nb = status.get("n_book", 0)
    cur = status.get("cursor", 0)
    pct = (100.0 * cur / nb) if nb else 0.0
    mh = status.get("min_hf")
    mh_s = f"{mh:.4f}" if mh is not None else "n/a"
    g = "OK" if status.get("guard_ok") else f"STOP({status.get('reason')})"
    fired = status.get("fired", 0)
    fired_s = f" fired {fired}" if fired else ""
    print(f"[{ts}] hot-set: read {status['n_read']} | hot {status['n_hot']} | "
          f"tgt {status['n_targets']}{fired_s} | sweep {cur}/{nb} ({pct:.0f}%) | "
          f"minHF {mh_s} | {dt:.1f}s | guard={g} "
          f"(DRY_RUN={'on' if C.DRY_RUN else 'OFF'}, contract={'set' if C.CONTRACT else 'none'})")


def heartbeat(st: dict) -> None:
    if C.HEARTBEAT_SEC <= 0:
        return
    now_ts = time.time()
    if now_ts - st.get("last_heartbeat", 0) < C.HEARTBEAT_SEC:
        return
    st["last_heartbeat"] = now_ts
    alert(f"💓 hyperlend executor alive: passes {st['passes']}, fires {st['fires']}, "
          f"reverts {st['reverts']}, gas today ${st['gas_usd']:.2f}/${C.MAX_DAILY_GAS_USD}. "
          f"DRY_RUN={'on' if C.DRY_RUN else 'OFF'}.")


def _acquire_lock():
    """Single-instance flock — a second executor can't run against the same key/state."""
    os.makedirs(os.path.dirname(C.LOCK_FILE), exist_ok=True)
    fh = open(C.LOCK_FILE, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"another executor holds {C.LOCK_FILE}; exiting.")
        sys.exit(0)
    fh.write(str(os.getpid()))
    fh.flush()
    return fh  # keep the handle alive for the process lifetime


def _kill_alert(st: dict, msg: str) -> None:
    """The cron watchdog resurrects this process every minute — throttle the kill-switch alert
    (katana pattern) so a tripped guard doesn't turn Telegram into a firehose."""
    print(msg)
    if time.time() - st.get("last_kill_alert", 0) > 900:
        st["last_kill_alert"] = time.time()
        alert(msg)


def loop() -> None:
    lock = _acquire_lock()  # noqa: F841 (held for process lifetime)
    st = load_state()
    book = load_book()
    hs = load_hotset()
    ok, reason = guard_ok(st)
    if not ok and not C.DRY_RUN:
        # kill-switch already tripped: exit quietly with a throttled alert instead of spamming a
        # start + kill message on every cron respawn (~2 msg/min otherwise)
        _kill_alert(st, f"🛑 KILL-SWITCH still tripped: {reason}. Waiting for intervention "
                        f"(python3 -m bot.executor reset, then restart).")
        save_state(st)
        sys.exit(1)
    # Hardened READ rpc for the loop: short socket timeout + HARD total wall cap per attempt +
    # endpoint benching, so one black-holing endpoint can NEVER wedge the single-threaded loop
    # (a 25s socket timeout demonstrably did not stop a 33s multicall stall — see analysis/rpc.py).
    rpc = Rpc(C.READ_RPCS, timeout=C.RPC_TIMEOUT, retries=C.RPC_RETRIES, min_interval=0.05,
              backoff_429=0.2, hard_timeout=C.RPC_HARD_TIMEOUT, bench_sec=C.RPC_BENCH_SEC,
              slow_sec=C.RPC_SLOW_SEC)
    reserves_cfg = load_reserves(rpc, book)   # cached in book; no RPC if already present
    save_book(book)
    banner = (f"▶️ hyperlend executor started (DRY_RUN={'on' if C.DRY_RUN else 'OFF'}, "
              f"path={'flash' if C.USE_FLASHLOAN else 'capital'}, min_profit ${C.MIN_PROFIT_USD}, "
              f"contract={'set' if C.CONTRACT else 'NONE'}, hot-set HF<{C.HOT_HF} "
              f"poll {C.HOT_POLL_SEC}s / sweep {C.SWEEP_CHUNK}/iter of {len(book['borrowers'])}, "
              f"kill-switch gas ${C.MAX_DAILY_GAS_USD}/day, {C.MAX_CONSEC_REVERTS} reverts).")
    print(banner)
    # throttle repeat start banners too — a crash-loop under the cron watchdog must not spam
    if time.time() - st.get("last_start_alert", 0) > 600:
        st["last_start_alert"] = time.time()
        save_state(st)
        alert(banner)
    st["last_heartbeat"] = time.time()
    while True:
        t0 = time.monotonic()
        _roll_day(st, datetime.now(timezone.utc).strftime("%Y-%m-%d"))
        try:
            gas_usd = gas_cost_usd(rpc)
            status = _hot_iteration(rpc, book, st, hs, gas_usd, reserves_cfg)
        except Exception as e:
            # a transient read failure must NOT kill the loop or silence the log — it logs a
            # status line and retries next iteration (the hardened rpc already bounded the wait).
            status = {"err": str(e)[:180]}
        st["passes"] += 1
        # end of a full-book cycle: fold in new borrowers + persist the book
        if status.get("wrapped"):
            try:
                _run_discovery(rpc, book)
            except Exception as e:
                print(f"discovery err: {e}")
            save_book(book)
        # STATUS LINE — every iteration, BEFORE any kill/exit, so the log never goes silent
        _log_iter(status, time.monotonic() - t0)
        if status.get("guard_ok") is False and not C.DRY_RUN:
            _kill_alert(st, f"🛑 KILL-SWITCH: {status['reason']}. Executor stopped — needs "
                            f"intervention (python3 -m bot.executor reset, then restart).")
            save_state(st)
            save_hotset(hs)
            sys.exit(1)   # non-zero: the watchdog must see FAILURE, not a clean exit
        heartbeat(st)
        if not C.DRY_RUN:
            check_balance(st, time.time())   # периодический газ-гард (троттлится внутри)
        # --- ярус 1 (04.08): prewarm nonce + shadow-телеметрия; оба не смеют ронять цикл ---
        if C.NONCE_PREWARM_SEC > 0 and not C.DRY_RUN and C.RAW_TX:
            addr = owner_address()
            if addr and time.monotonic() - _nonce_cache.get("chain_ts", 0.0) >= C.NONCE_PREWARM_SEC:
                prewarm_nonce(addr)
        try:
            shadow.tick(rpc, book, hs, st)   # троттлится сам; тяжесть в daemon-потоке
        except Exception as e:
            print(f"  shadow tick err: {e}")
        save_state(st)
        save_hotset(hs)
        # после апдейта цены не спать полный интервал: транзиты HF доигрываются 1-3 блока
        time.sleep(0.05 if status.get("oracle_upd") else C.HOT_POLL_SEC)


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "once"
    if cmd == "once":
        try:
            once()
        except GuardTripped as g:
            print(f"KILL-SWITCH: {g}")
    elif cmd == "loop":
        loop()
    elif cmd == "reset":
        st = load_state()
        st["consec_reverts"] = 0
        st["gas_usd"] = 0.0
        # Clear the DEDUP JOURNAL but KEEP anything still in flight. reset is the documented
        # kill-switch recovery and gets run in a hurry, mid-incident, with txs on the wire —
        # wiping those records is not a reset, it is losing them: their gas is never reconciled
        # against the receipt, a revert that lands afterwards never reaches consec_reverts (so the
        # operator re-arms the bot straight back into the failure that tripped it), and the
        # borrower is instantly re-fireable while the first tx is still live. Settled records are
        # exactly what reset is for and go. Preserving in-flight ones is safe: gas_usd is clamped
        # at zero in _settle_gas, so their provisional charge cannot refund into the reset budget.
        inflight = {k: v for k, v in st.get("sent", {}).items()
                    if v.get("status") in ("pending", "stale")}
        st["sent"] = inflight
        save_state(st)
        kept = (f"; KEPT {len(inflight)} in-flight tx record(s) for receipt settlement"
                if inflight else "")
        print(f"guard/dedup reset{kept}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
