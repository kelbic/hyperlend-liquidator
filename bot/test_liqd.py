"""Offline unit tests for the LiquidSwap quote wall-budget (no network). Run:
PYTHONPATH=. python3 bot/test_liqd.py

Pins the R1 fix (2026-07-18): quote() used a bare urlopen(timeout=20) — per-recv, NOT wall — with
retries=3 + backoff sleeps, so a trickling api.liqd.ag could block the single-threaded hot loop
for an unbounded time RIGHT AT FIRE TIME (a quote is only requested once HF<1). Now the whole
retry loop runs under a hard wall budget (QUOTE_WALL_SEC, default 8s) via
analysis.rpc._run_with_deadline:
  * a trickling endpoint -> HardTimeout within the budget, control returns to the loop,
  * evaluate() turns that HardTimeout into a target SKIP — the loop lives,
  * a healthy endpoint / normal retry-then-fail semantics are unchanged under the wrapper.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from analysis.rpc import HardTimeout   # noqa: E402
from bot import executor as E          # noqa: E402
from bot import liqd                   # noqa: E402

_release = threading.Event()   # lets trickling workers exit at teardown (daemons anyway)


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _good_route(req, timeout=None):
    return _FakeResp(json.dumps({
        "success": True, "amountOut": "1630.5",
        "averagePriceImpact": "0.10%",
        "execution": {"to": liqd.ROUTER, "calldata": "0xdeadbeef"},
    }).encode())


def _trickle(req, timeout=None):
    _release.wait(30)                          # a trickling endpoint: per-recv timeout never fires
    return _good_route(req, timeout)


def test_quote_trickle_bounded_by_wall_budget():
    # the core R1 property: a trickling endpoint cannot hold quote() past the wall budget —
    # HardTimeout is raised and control returns to the (single-threaded) caller fast.
    _release.clear()
    liqd._urlopen = _trickle
    t0 = time.time()
    raised = False
    try:
        liqd.quote("0xc", "0xd", 10 ** 18, 18, 6, wall_sec=0.3)
    except HardTimeout:
        raised = True
    dt = time.time() - t0
    assert raised, "a trickling quote endpoint must raise HardTimeout"
    assert dt < 2.0, f"wall budget did not bound the trickle: {dt:.2f}s"
    _release.set()


def test_quote_wall_budget_covers_all_retries():
    # the budget is over ALL retries+backoffs, not per attempt: each attempt trickles "briefly",
    # each under any plausible per-attempt cap, but the SUM crosses the wall -> HardTimeout.
    def slow_attempt(req, timeout=None):
        time.sleep(0.25)
        raise OSError("reset")                 # retryable -> next attempt + backoff sleep
    liqd._urlopen = slow_attempt
    t0 = time.time()
    raised = False
    try:
        liqd.quote("0xc", "0xd", 10 ** 18, 18, 6, retries=10, wall_sec=0.6)
    except HardTimeout:
        raised = True
    assert raised, "cumulative retry time must be capped by ONE wall budget"
    assert time.time() - t0 < 2.0


def test_evaluate_survives_quote_hard_timeout():
    # the loop-liveness half: evaluate() must turn the HardTimeout into a skip dict, never an
    # exception — process_targets/_hot_iteration keep ticking through a dead aggregator.
    _release.clear()
    o_urlopen, o_wall = liqd._urlopen, liqd.QUOTE_WALL_SEC
    liqd._urlopen = _trickle
    liqd.QUOTE_WALL_SEC = 0.3
    t = {"seized": 10 ** 18, "debt_to_cover": 1_000 * 10 ** 6, "debt_pulled": 1_000 * 10 ** 6,
         "coll_asset": "0xc", "debt_asset": "0xd", "coll_dec": 18, "debt_dec": 6,
         "debt_price": 100_000_000}
    t0 = time.time()
    try:
        ev = E.evaluate(t, gas_usd=0.01)
    finally:
        liqd._urlopen, liqd.QUOTE_WALL_SEC = o_urlopen, o_wall
        _release.set()
    assert ev is not None and "skip" in ev, f"HardTimeout must become a skip, got {ev}"
    assert "quote error" in ev["skip"]
    assert time.time() - t0 < 2.0, "the loop was blocked past the wall budget"


def test_quote_success_unchanged_under_wrapper():
    # a healthy endpoint parses exactly as before through the deadline wrapper
    liqd._urlopen = _good_route
    q = liqd.quote("0xc", "0xd", 10 ** 18, 18, 6, wall_sec=5.0)
    assert q["ok"] and q["swap_target"] == liqd.ROUTER and q["swap_calldata"] == "0xdeadbeef"
    assert q["amount_out"] == int(1630.5 * 10 ** 6)
    assert abs(q["price_impact"] - 0.001) < 1e-12


def test_quote_transient_failures_keep_liqderror_semantics():
    # fast transient failures that exhaust retries INSIDE the budget still raise LiqdError (not
    # HardTimeout) — callers distinguishing route-vs-transport behaviour are unaffected.
    def flaky(req, timeout=None):
        raise OSError("connection reset")
    liqd._urlopen = flaky
    raised = False
    try:
        liqd.quote("0xc", "0xd", 10 ** 18, 18, 6, retries=2, wall_sec=5.0)
    except liqd.LiqdError as e:
        raised = not isinstance(e, liqd.NoRouteError)
    assert raised


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    _orig = liqd._urlopen
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
        finally:
            liqd._urlopen = _orig
    _release.set()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
