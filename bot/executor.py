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
from analysis.aave import SEL_GET_USER_ACCOUNT_DATA, decode_user_account_data  # noqa: E402
from analysis.monitor import load_book, save_book, scan  # noqa: E402
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


class GuardTripped(Exception):
    pass


def guard_ok(st: dict) -> tuple[bool, str]:
    if st["consec_reverts"] >= C.MAX_CONSEC_REVERTS:
        return False, f"{st['consec_reverts']} consecutive reverts >= {C.MAX_CONSEC_REVERTS}"
    if st["gas_usd"] >= C.MAX_DAILY_GAS_USD:
        return False, f"daily gas ${st['gas_usd']:.2f} >= ${C.MAX_DAILY_GAS_USD}"
    return True, ""


def recently_fired(st: dict, key: str, now_ts: float) -> bool:
    rec = st["sent"].get(key)
    return bool(rec and (now_ts - rec["ts"]) < C.DEDUP_SEC and rec.get("status") != "revert")


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
    (swap_target, swap_calldata, min_profit_wei, net_usd, ...) or None if not profitable."""
    seized = t["seized"]
    debt_to_cover = t["debt_to_cover"]
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
    # flash-loan repayment: principal + premium (0.04%). Capital path repays only the principal
    # implicitly (it spent its own balance), so owed == debt_to_cover.
    if C.USE_FLASHLOAN:
        premium = (debt_to_cover * C.FLASH_PREMIUM_BPS + 9999) // 10000
        owed = debt_to_cover + premium
    else:
        owed = debt_to_cover
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
    for t in sorted(targets, key=lambda x: -(x.get("gross_bonus_usd") or 0)):
        key = t["borrower"]
        if recently_fired(st, key, now_ts):
            continue
        hf_now = fresh_hf(rpc, key)
        if hf_now is not None and hf_now >= 1.0:
            print(f"  skip {key[:10]}…: HF cured to {hf_now:.4f} before fire")
            continue
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
    save_book(book)          # persist the borrower book so it survives restarts
    if own:
        save_state(st)
    return len(targets)


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


def loop() -> None:
    lock = _acquire_lock()  # noqa: F841 (held for process lifetime)
    st = load_state()
    book = load_book()
    alert(f"▶️ hyperlend executor started (DRY_RUN={'on' if C.DRY_RUN else 'OFF'}, "
          f"path={'flash' if C.USE_FLASHLOAN else 'capital'}, min_profit ${C.MIN_PROFIT_USD}, "
          f"contract={'set' if C.CONTRACT else 'NONE'}, kill-switch gas ${C.MAX_DAILY_GAS_USD}/day, "
          f"{C.MAX_CONSEC_REVERTS} reverts).")
    st["last_heartbeat"] = time.time()
    while True:
        try:
            n = once(st, book)
            wait = C.HOT_POLL_SEC if n else C.POLL_SEC
        except GuardTripped as g:
            alert(f"🛑 KILL-SWITCH: {g}. Executor stopped — needs intervention "
                  f"(python3 -m bot.executor reset, then restart).")
            save_state(st)
            return
        except Exception as e:
            print(f"loop err: {e}")
            wait = C.POLL_SEC
        heartbeat(st)
        save_state(st)
        time.sleep(wait)


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
