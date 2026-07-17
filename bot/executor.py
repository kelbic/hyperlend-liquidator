"""HyperLend (HyperEVM, chainId 999) live-signing liquidation executor.

Autonomous loop: monitor.scan() finds HF<1 positions and sizes each (Aave-v3 close-factor +
collateral-constrained debtToCover) -> evaluate() quotes the exit on LiquidSwap (collateral ->
debt asset) and gates on net after flash-loan repayment + gas -> fire() signs and broadcasts an
atomic HyperLendLiquidator.liquidate() tx (flash-loan the debt, liquidationCall, swap, repay,
keep the spread). Framework/idioms cloned from the live wc/katana Morpho bots; the KEY DIFFERENCE
is Aave-v3 mechanics (flashLoanSimple + liquidationCall) instead of Morpho.

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
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

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
from analysis.rpc import Rpc                                    # noqa: E402

LIQUIDATE_SIG = "liquidate(address,address,address,uint256,bool,address,bytes,uint256)"


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
    must not be retried immediately)."""
    rec = st["sent"].get(key)
    if not rec:
        return False
    age = now_ts - rec["ts"]
    if rec.get("status") == "revert":
        return age < C.REVERT_COOLDOWN_SEC
    return age < C.DEDUP_SEC


# --------------------------------------------------------------------------- telegram (optional)
def alert(text: str) -> None:
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


# --------------------------------------------------------------------------- fire
def _sign_args() -> list[str]:
    return ["--private-key", C.PRIVATE_KEY]


def _fee_args() -> list[str]:
    # HyperEVM priority fee is non-operative; keep it ~0 to avoid overpaying (no ordering effect).
    return ["--priority-gas-price", str(int(C.PRIORITY_GWEI * 1e9))]


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

    args = ["cast", "send", C.CONTRACT, LIQUIDATE_SIG,
            t["coll_asset"], t["debt_asset"], t["borrower"], str(ev["debt_to_cover"]),
            "true" if C.USE_FLASHLOAN else "false", ev["swap_target"], ev["swap_calldata"],
            str(ev["min_profit_wei"]),
            "--gas-limit", str(C.GAS_LIMIT), "--rpc-url", C.RPC_WRITE] + _fee_args() + _sign_args()
    alert(f"🔫 LIQUIDATE {hdr}, sending…")
    st["fires"] += 1
    st["gas_usd"] += gas_usd
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=90)
        out = (r.stdout or "") + (r.stderr or "")
        reverted = (r.returncode != 0) or ("status" in out and "0 (failed)" in out)
        status = "revert" if reverted else "ok"
        st["sent"][key] = {"ts": now_ts, "status": status, "tx": out[-80:].strip()}
        if reverted:
            st["consec_reverts"] += 1
            st["reverts"] += 1
            alert(f"❌ revert {hdr}: {out[-200:]}")
        else:
            st["consec_reverts"] = 0
            alert(f"✅ liq ok {hdr}: {out[-160:]}")
    except Exception as e:
        st["consec_reverts"] += 1
        st["reverts"] += 1
        st["sent"][key] = {"ts": now_ts, "status": "revert", "tx": f"err:{e}"}
        alert(f"❌ cast error {hdr}: {e}")


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


def _run_discovery(rpc: Rpc, book: dict) -> None:
    """End-of-cycle discovery: merge new Borrow-log borrowers and advance the checkpoint (bounded
    getLogs, HL_INCR_WINDOW back from head; the hardened rpc caps any endpoint hang). Run once per
    full-book cycle (cursor wrap) — a brand-new borrower is healthy at borrow time, so a few-minute
    add latency is harmless, and it keeps getLogs off the hot path."""
    to = rpc.block_number()
    book["borrowers"] = sorted(discover_borrowers(rpc, book, to))
    book["last_block"] = to


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
