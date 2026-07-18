"""Offline unit tests for the executor's dedup / evaluate / fire plumbing (no network). Run:
PYTHONPATH=. python3 bot/test_executor.py

Covers the two live-loop regressions fixed 2026-07-16:
  * a REVERTED target must be blocked for REVERT_COOLDOWN_SEC (immediate retries burned the
    3-revert kill-switch budget on a single bad target),
  * evaluate() must quote the FEE-ADJUSTED seized amount and charge the flash premium on the
    full tx param while only the actual pull consumes proceeds,
and the 2026-07-18 fire-path latency fix:
  * the broadcast (`cast send`) must NEVER wait on a Telegram alert — the old synchronous
    pre-broadcast alert cost every shot an HTTPS round-trip (unbounded on a trickling
    api.telegram.org); alerts are now fire-and-forget and an alert failure cannot affect a shot,
  * a failed discovery cycle must keep the book checkpoint (last_block) so no Borrow-log range
    is ever silently skipped, and must not raise into the hot loop.
"""
from __future__ import annotations

import os
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from bot import config as C          # noqa: E402
from bot import executor as E        # noqa: E402
from bot import liqd                 # noqa: E402
from analysis.rpc import RpcError    # noqa: E402


def test_recently_fired_blocks_after_revert():
    st = {"sent": {"0xabc": {"ts": 1000.0, "status": "revert"}}}
    # immediately after the revert: blocked (must NOT retry next pass)
    assert E.recently_fired(st, "0xabc", 1000.0 + 5)
    assert E.recently_fired(st, "0xabc", 1000.0 + C.REVERT_COOLDOWN_SEC - 1)
    # after the cooldown: free again
    assert not E.recently_fired(st, "0xabc", 1000.0 + C.REVERT_COOLDOWN_SEC + 1)


def test_recently_fired_ok_uses_dedup_sec():
    st = {"sent": {"0xabc": {"ts": 1000.0, "status": "ok"}}}
    assert E.recently_fired(st, "0xabc", 1000.0 + C.DEDUP_SEC - 1)
    assert not E.recently_fired(st, "0xabc", 1000.0 + C.DEDUP_SEC + 1)
    assert not E.recently_fired(st, "0xnew", 1000.0)


def test_evaluate_uses_fee_adjusted_seized_and_pulled():
    # USDT0 debt (6 dec, $1), WHYPE collateral: full close of a $1500 leg.
    # seized = fee-adjusted (received) amount; debt_to_cover overshoots; pulled = actual.
    t = {
        "seized": 109 * 10 ** 18 // 100,          # 1.09 WHYPE received (fee-adjusted)
        "debt_to_cover": 1515 * 10 ** 6,          # overshot tx param (+1%)
        "debt_pulled": 1500 * 10 ** 6,            # actual pull the Pool will make
        "coll_asset": "0xc", "debt_asset": "0xd", "coll_dec": 18, "debt_dec": 6,
        "debt_price": 100_000_000,
    }
    captured = {}

    def fake_quote(coll, debt, seized_wei, coll_dec, debt_dec):
        captured["seized_wei"] = seized_wei
        return {"amount_out": 1630 * 10 ** 6, "price_impact": 0.001,
                "swap_target": "0xrouter", "swap_calldata": "0xcd", "amount_in_used": 0}

    orig = liqd.quote_for_seized
    liqd.quote_for_seized = fake_quote
    try:
        ev = E.evaluate(t, gas_usd=0.01)
    finally:
        liqd.quote_for_seized = orig

    # the quote input is the fee-adjusted seized figure, untouched
    assert captured["seized_wei"] == t["seized"]
    # premium charged on the FULL flashed amount, proceeds consumed by the actual pull only
    premium = (t["debt_to_cover"] * C.FLASH_PREMIUM_BPS + 9999) // 10000
    assert ev["owed"] == t["debt_pulled"] + premium
    assert ev["net_wei"] == 1630 * 10 ** 6 - ev["owed"]


# ------------------------------------------------------------------- fire path: broadcast first
_T = {"borrower": "0x" + "ab" * 20, "hf": 0.95, "close_factor": 1.0, "coll_sym": "WHYPE",
      "debt_sym": "USDC", "debt_to_cover": 1_000 * 10 ** 6, "coll_asset": "0x" + "c" * 40,
      "debt_asset": "0x" + "d" * 40}
_EV = {"net_usd": 42.0, "impact": 0.002, "debt_to_cover": 1_000 * 10 ** 6,
       "swap_target": "0x" + "e" * 40, "swap_calldata": "0xcd", "min_profit_wei": 1}


def _fresh_state() -> dict:
    return {"fires": 0, "gas_usd": 0.0, "consec_reverts": 0, "reverts": 0, "sent": {}}


def _live_fire(fake_alert, fake_run):
    """Run fire() live-armed (DRY off, contract set) with alert + subprocess.run patched."""
    o_alert, o_run = E.alert, E.subprocess.run
    o_dry, o_contract, o_key = C.DRY_RUN, C.CONTRACT, C.PRIVATE_KEY
    E.alert, E.subprocess.run = fake_alert, fake_run
    C.DRY_RUN, C.CONTRACT, C.PRIVATE_KEY = False, "0x" + "1" * 40, "0x" + "2" * 64
    st = _fresh_state()
    try:
        E.fire(dict(_T), dict(_EV), st, 1_000_000.0, 0.01)
    finally:
        E.alert, E.subprocess.run = o_alert, o_run
        C.DRY_RUN, C.CONTRACT, C.PRIVATE_KEY = o_dry, o_contract, o_key
    return st


class _CastOk:
    returncode = 0
    stdout = "blockHash 0x.. status 1 (success)"
    stderr = ""


def test_fire_broadcasts_without_waiting_on_alert():
    """THE ordering fix: `cast send` must happen immediately, even while the Telegram POST is
    still in flight (here: blocked indefinitely, simulating a trickling api.telegram.org — the
    old code sat in alert() BEFORE the broadcast, unbounded)."""
    events = []
    release = threading.Event()
    alert_started = threading.Event()

    def blocked_alert(text):
        alert_started.set()
        release.wait(5)                      # trickling Telegram: does not return
        events.append(("alert", text))

    def fake_run(args, **kw):
        events.append(("cast", list(args)))
        return _CastOk()

    st = _live_fire(blocked_alert, fake_run)
    # the broadcast happened, FIRST, while the alert was still blocked
    assert events and events[0][0] == "cast", f"broadcast must precede any alert: {events}"
    assert events[0][1][:2] == ["cast", "send"]
    assert st["sent"][_T["borrower"]]["status"] == "ok"
    release.set()
    # the alerts still go out (fire-and-forget): 🔫 pre-shot + ✅ result
    deadline = time.time() + 2.0
    while time.time() < deadline and sum(1 for k, _ in events if k == "alert") < 2:
        time.sleep(0.01)
    texts = [x for k, x in events if k == "alert"]
    assert any("LIQUIDATE" in x for x in texts), f"pre-shot alert lost: {texts}"
    assert any("liq ok" in x for x in texts), f"result alert lost: {texts}"
    assert alert_started.is_set()


def test_fire_alert_failure_never_affects_the_shot():
    # a raising alert (Telegram hard-down) must not delay, fail, or alter the shot/state
    def bomb_alert(text):
        raise RuntimeError("telegram down")

    ran = []

    def fake_run(args, **kw):
        ran.append(list(args))
        return _CastOk()

    st = _live_fire(bomb_alert, fake_run)
    time.sleep(0.05)                          # let the daemon alert threads die quietly
    assert len(ran) == 1, "the broadcast must happen exactly once despite alert failure"
    assert st["fires"] == 1 and st["consec_reverts"] == 0
    assert st["sent"][_T["borrower"]]["status"] == "ok"


# ------------------------------------------------------------------- discovery failure resilience
class _FakeRpcHead:
    def __init__(self, head: int):
        self._head = head

    def block_number(self) -> int:
        return self._head


def test_run_discovery_failure_keeps_checkpoint_and_never_raises():
    """R3 caller half: when get_logs_chunked gives up (bounded RpcError instead of the old
    infinite spin), _run_discovery must swallow it, keep borrowers/last_block UNTOUCHED (the
    range is re-scanned from the same checkpoint next wrap — no Borrow event lost), and return
    False so the hot loop just keeps ticking."""
    book = {"borrowers": ["0x" + "a" * 40], "last_block": 123}

    def boom(rpc, bk, to):
        raise RpcError("get_logs_chunked", "gave up after 6 consecutive failed window(s)")

    o = E.discover_borrowers
    E.discover_borrowers = boom
    try:
        ok = E._run_discovery(_FakeRpcHead(500), book)
    finally:
        E.discover_borrowers = o
    assert ok is False                        # skipped, not raised — loop() survives regardless
    assert book["last_block"] == 123, "checkpoint must NOT advance past an unscanned range"
    assert book["borrowers"] == ["0x" + "a" * 40]


def test_run_discovery_success_advances_checkpoint():
    book = {"borrowers": ["0x" + "a" * 40], "last_block": 123}
    o = E.discover_borrowers
    E.discover_borrowers = lambda rpc, bk, to: {"0x" + "a" * 40, "0x" + "b" * 40}
    try:
        ok = E._run_discovery(_FakeRpcHead(500), book)
    finally:
        E.discover_borrowers = o
    assert ok is True
    assert book["last_block"] == 500
    assert book["borrowers"] == sorted(["0x" + "a" * 40, "0x" + "b" * 40])


if __name__ == "__main__":
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
