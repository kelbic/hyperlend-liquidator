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
send can no longer be judged at fire time, the fleet rule is explicit: an error BEFORE the
broadcast (transport/signing) is a send_error — it burns no gas and does NOT feed the kill-switch;
ONLY receipt.status == 0 is a revert.

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
from analysis.aave import (                                    # noqa: E402
    HF_INFINITY, SEL_GET_USER_ACCOUNT_DATA, decode_user_account_data,
)
from analysis.monitor import (                                 # noqa: E402
    discover_borrowers, load_book, load_reserves, refine, save_book, scan, sweep_accounts,
)
from analysis.protocols import ORACLE_BASE_UNIT, is_stable, sym  # noqa: E402
from analysis.rpc import Rpc, _run_with_deadline                 # noqa: E402

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


def alert(text: str) -> None:
    text = _tagged(text)
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
def evaluate(t: dict, gas_usd: float) -> dict | None:
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

_nonce_cache: dict = {"addr": None, "next": None}   # local counter layered over 'pending'
_owner_addr: str | None = None


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
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": method, "params": params}).encode()
    wall = C.RPC_HARD_TIMEOUT if budget is None else budget

    def _attempt():
        req = urllib.request.Request(C.RPC_WRITE, data=body,
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=C.RPC_TIMEOUT) as r:
            return json.loads(r.read())

    d = _run_with_deadline(_attempt, wall)
    if d.get("error"):
        raise RuntimeError(f"rpc {method}: {d['error']}")
    return d["result"]


def _pending_nonce(addr: str) -> int:
    """nonce = eth_getTransactionCount(addr,'pending') with a local counter layered on top: in a
    multi-target cascade the node's 'pending' does not yet reflect the tx we broadcast seconds ago,
    so take max(pending, local next). The local counter advances ONLY after a SUCCESSFUL broadcast
    (_nonce_after_send) — a send that failed must not leave a hole that stalls the whole queue."""
    pend = int(_rpc_write("eth_getTransactionCount", [addr, "pending"]), 16)
    nxt = _nonce_cache["next"]
    if _nonce_cache["addr"] == addr and nxt is not None and nxt > pend:
        return nxt
    return pend


def _nonce_after_send(addr: str, used: int) -> None:
    """Advance the local counter past the nonce we just successfully broadcast."""
    _nonce_cache["addr"] = addr
    _nonce_cache["next"] = used + 1


def _fee_params() -> tuple[int, int]:
    """EIP-1559 fee (HyperEVM is type-0x2; our 2026-07-19 canary went out exactly this way).
    maxPriorityFeePerGas = HL_PRIORITY_GWEI (0: the chain is latency-FCFS, a tip buys NO ordering
    — paying one is pure donation). maxFeePerGas = baseFee*2 + priority: maxFee is a CAP, not a
    payment (the block charges the actual base), so the 2x headroom is economically free and only
    protects against a baseFee rise between signing and inclusion. Falls back to eth_gasPrice if
    baseFee is unavailable."""
    priority = int(C.PRIORITY_GWEI * 1e9)
    try:
        blk = _rpc_write("eth_getBlockByNumber", ["latest", False])
        base = int(blk["baseFeePerGas"], 16)
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


def _sign_and_send(calldata: str) -> str:
    """Sign an EIP-1559 tx locally and broadcast it. Returns the tx hash; raises on any transport
    or signing failure (which the caller must classify as a SEND ERROR, never a revert).

    `to` is CHECKSUMMED explicitly: eth_account will happily sign a lowercase `to`, but a
    mixed-case non-checksummed one raises — and this exact class of bug killed a WC shot. Doing it
    here means C.CONTRACT can be set in any case without the fire path caring."""
    from eth_account import Account
    addr = owner_address()
    if not addr:
        raise RuntimeError("no signing key (HL_PRIVATE_KEY / HL_KEYFILE)")
    max_fee, priority = _fee_params()
    nonce = _pending_nonce(addr)
    tx = {"chainId": C.CHAIN_ID, "nonce": nonce, "to": to_checksum_address(C.CONTRACT),
          "value": 0, "gas": C.GAS_LIMIT, "maxFeePerGas": max_fee,
          "maxPriorityFeePerGas": priority, "data": calldata}
    signed = Account.sign_transaction(tx, C.PRIVATE_KEY)
    raw = signed.raw_transaction
    raw_hex = raw.to_0x_hex() if hasattr(raw, "to_0x_hex") else "0x" + raw.hex()
    txh = _rpc_write("eth_sendRawTransaction", [raw_hex])
    _nonce_after_send(addr, nonce)     # ONLY after the node accepted it — no hole on failure
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
    try:
        calldata = _encode_liquidate(t, ev)
        txh = _sign_and_send(calldata)
    except Exception as e:
        # SEND ERROR (transport / signing / node rejection BEFORE inclusion) is NOT a revert:
        # nothing executed on-chain, no gas was burned, and feeding it to consec_reverts would let
        # an RPC brownout trip the kill-switch and take the bot down for the whole crash window.
        # Only receipt.status == 0 is a revert (see _check_pending).
        st["sent"][key] = {"ts": now_ts, "status": "send_error", "tx": f"senderr:{e}"[:120]}
        alert_async(f"⚠️ send error {hdr} (not counted as revert): {e}")
        return
    st["fires"] += 1
    # Provisional gas charge, exactly ONCE per tx. _check_pending reverses this figure and books
    # the real gasUsed*effectiveGasPrice when the receipt lands, so the daily cap can never be
    # double-charged for one shot. `gas_usd` is carried on the record to make the undo exact.
    st["gas_usd"] += gas_usd
    st["sent"][key] = {"ts": now_ts, "status": "pending", "tx": txh, "gas_usd": gas_usd}
    # Alert AFTER the broadcast and fire-and-forget: Telegram must never sit between the
    # liquidation decision and the wire, nor delay the next shot in a cascade.
    alert_async(f"🔫 LIQUIDATE {hdr}, sent: {txh}")


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
_SENT_RETENTION_SEC = 86400.0


def _rcpt_status(rcpt: dict) -> int | None:
    """Receipt status, or None when unparsable (status:null and friends). Returning None instead
    of raising is deliberate: a TypeError here would escape into the hot loop and kill the pass —
    the silent-zombie failure mode (heartbeat alive, detection dead)."""
    try:
        return int(rcpt.get("status"), 16)
    except (TypeError, ValueError):
        return None


def _settle_gas(st: dict, rec: dict, rcpt: dict) -> float:
    """Swap the provisional gas charge for the ACTUAL cost of this tx. Runs exactly once per tx,
    at the moment the receipt is decided — so a tx is never charged twice, and never charged at
    an estimate when the real number is available."""
    st["gas_usd"] -= rec.get("gas_usd", 0.0)          # undo the provisional charge
    try:
        actual = (int(rcpt["gasUsed"], 16) * int(rcpt.get("effectiveGasPrice", "0x0"), 16)
                  / 1e18 * C.HYPE_USD)
    except Exception:
        actual = rec.get("gas_usd", 0.0)              # unreadable receipt: keep the estimate
    st["gas_usd"] += actual
    return actual


def _check_pending(rpc: Rpc, st: dict, now_ts: float) -> None:
    """Settle txs broadcast on earlier passes — the other half of the non-blocking fire path.
    Called once per iteration next to the hot-set update, BEFORE any new shot, so a revert that
    just landed feeds the kill-switch before we fire again this pass.

    Records are grouped by tx hash so the gas accounting and the revert counters run once per TX,
    not once per key. Each tx settles inside its own try/except: one malformed answer from one
    endpoint must never take down the pass (this loop is the bot's only liveness)."""
    by_tx: dict[str, list[str]] = {}
    for key, rec in st.get("sent", {}).items():
        if rec.get("status") == "pending" and str(rec.get("tx", "")).startswith("0x"):
            by_tx.setdefault(rec["tx"], []).append(key)
    for txh, keys in by_tx.items():
        try:
            rec = st["sent"][keys[0]]
            rcpt = rpc.call("eth_getTransactionReceipt", [txh])
            if not rcpt:
                # not mined yet. Past the stale wall it is very likely a stuck nonce (a gap left
                # by an earlier tx the sequencer never saw) — surface it, the operator must look.
                if now_ts - rec["ts"] > PENDING_STALE_SEC:
                    for k in keys:
                        st["sent"][k]["status"] = "stale"
                    alert_async(f"⚠️ tx unmined 10min (stuck nonce?): {txh}")
                continue
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
        print(f"  target {key[:10]}… HF={t['hf']:.4f} {t['coll_sym']}->{t['debt_sym']} "
              f"cover=${t['repaid_usd']:,.0f} net={nets} impact={ev['impact']*100:.2f}% "
              f"profitable={ev['profitable']}")
        if ev["profitable"]:
            fire(t, ev, st, now_ts, gas_usd)
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
    chunk, new_cursor, wrapped = next_chunk(borrowers, hs["cursor"], C.SWEEP_CHUNK)
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

    hfs = [a["health_factor"] / 1e18 for a in accounts.values() if a["health_factor"] < HF_INFINITY]
    return {"n_read": len(accounts), "n_hot": len(hot), "n_targets": len(tgt_borrowers),
            "fired": len(fired), "cursor": new_cursor, "wrapped": wrapped, "chunk": len(chunk),
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
              backoff_429=0.2, hard_timeout=C.RPC_HARD_TIMEOUT, bench_sec=C.RPC_BENCH_SEC)
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
        save_state(st)
        save_hotset(hs)
        time.sleep(C.HOT_POLL_SEC)


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
        st["sent"] = {}
        save_state(st)
        print("guard/dedup reset")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
