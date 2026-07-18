"""Offline unit tests for the hardened RPC transport (no network). Run:
PYTHONPATH=. python3 analysis/test_rpc.py

Pins the anti-wedge behaviour added 2026-07-17 after the live loop HUNG >10min blocked in a single
do_poll despite a 25s socket timeout (the socket timeout is per-recv, not total — a trickling /
half-open endpoint outlasts it; reproduced: a 400-call multicall took 33s under timeout=25s). A
single black-holing endpoint must never freeze the single-threaded loop:
  * an endpoint that raises socket.timeout -> rotate to the next and succeed,
  * an endpoint that NEVER returns -> the HARD wall deadline abandons it and we rotate + succeed,
  * a failed/hung endpoint is BENCHED so we don't re-pick it,
  * every endpoint dead -> the call still returns control (raises) within a bounded time.
"""
from __future__ import annotations

import json
import socket
import threading
import time

import analysis.rpc as rpc_mod
from analysis.rpc import HardTimeout, Rpc

GOOD = "https://good.example"
BAD = "https://bad.example"
HANG = "https://hang.example"

_release = threading.Event()   # lets hung workers exit at teardown (they are daemons anyway)


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _result_body(result: str) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode()


def _install(behaviors: dict, counts: dict) -> None:
    """Patch rpc._urlopen with a fake dispatching on the request URL. `behaviors[url]` is a
    callable(timeout) -> _FakeResp (or raises / blocks). `counts` tallies hits per URL."""
    def fake(req, timeout=None):
        url = req.full_url
        counts[url] = counts.get(url, 0) + 1
        for key, fn in behaviors.items():
            if url.startswith(key):
                return fn(timeout)
        raise AssertionError(f"no fake behaviour for {url}")
    rpc_mod._urlopen = fake


def _good(_timeout):
    return _FakeResp(_result_body("0x3e7"))   # eth_chainId -> 999


def _raise_timeout(_timeout):
    raise socket.timeout("simulated recv timeout")


def _never_returns(_timeout):
    _release.wait(30)                          # block the worker thread ~forever (daemon)
    return _FakeResp(_result_body("0x3e7"))


def test_rotate_on_socket_timeout():
    counts = {}
    _install({BAD: _raise_timeout, GOOD: _good}, counts)
    r = Rpc([BAD, GOOD], retries=3, min_interval=0.0, timeout=1.0)
    t0 = time.time()
    assert r.chain_id() == 999               # rotated past the timing-out endpoint
    assert time.time() - t0 < 1.0            # did not block
    assert counts[BAD] == 1 and counts[GOOD] == 1
    assert r._benched.get(BAD, 0) > time.time()   # BAD was benched


def test_hard_deadline_on_hang():
    counts = {}
    _release.clear()
    _install({HANG: _never_returns, GOOD: _good}, counts)
    # HANG never returns; the per-attempt socket timeout is irrelevant (the worker ignores it).
    # Only the HARD deadline can free us. It must, and fast.
    r = Rpc([HANG, GOOD], retries=3, min_interval=0.0, timeout=30.0, hard_timeout=0.3)
    t0 = time.time()
    assert r.chain_id() == 999
    dt = time.time() - t0
    assert dt < 2.0, f"hard deadline did not bound the hang: {dt:.2f}s"
    assert r._benched.get(HANG, 0) > time.time()
    _release.set()                            # let the leaked daemon worker exit cleanly


def test_benched_endpoint_is_skipped():
    counts = {}
    _install({BAD: _raise_timeout, GOOD: _good}, counts)
    r = Rpc([BAD, GOOD], retries=3, min_interval=0.0, timeout=1.0)
    assert r.chain_id() == 999                # call 1: hits BAD once, benches it, uses GOOD
    assert r.chain_id() == 999                # call 2: BAD is benched -> skipped entirely
    assert counts[BAD] == 1, f"benched endpoint was re-picked: {counts}"
    assert counts[GOOD] == 2


def test_all_endpoints_dead_returns_control_bounded():
    counts = {}
    _release.clear()
    _install({HANG: _never_returns, BAD: _raise_timeout}, counts)
    r = Rpc([HANG, BAD], retries=3, min_interval=0.0, timeout=30.0, hard_timeout=0.3)
    t0 = time.time()
    raised = False
    try:
        r.chain_id()
    except RuntimeError:
        raised = True                         # exhausted retries -> control returned, not a hang
    dt = time.time() - t0
    assert raised
    assert dt < 3.0, f"a fully-dead network still blocked {dt:.2f}s (must be bounded)"
    _release.set()


def _rate_limited(_timeout):
    return _FakeResp(json.dumps(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "rate limited"}}).encode())


def _bad_params(_timeout):
    return _FakeResp(json.dumps(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "invalid params"}}).encode())


def test_rate_limit_rotates_and_benches():
    # a rate-limit JSON-RPC error must be treated as transport: bench that endpoint, rotate to a
    # healthy one, succeed — never crash the caller (the cascade-time failure mode).
    counts = {}
    _install({BAD: _rate_limited, GOOD: _good}, counts)
    r = Rpc([BAD, GOOD], retries=3, min_interval=0.0, backoff_429=0.0, timeout=1.0)
    assert r.chain_id() == 999
    assert r._benched.get(BAD, 0) > time.time()
    assert counts[GOOD] == 1


def test_genuine_rpc_error_still_raises():
    # a real verdict (bad params) is NOT a transport failure — it propagates so real bugs surface.
    counts = {}
    _install({GOOD: _bad_params}, counts)
    r = Rpc([GOOD], retries=3, min_interval=0.0, timeout=1.0)
    raised = False
    try:
        r.chain_id()
    except rpc_mod.RpcError:
        raised = True
    assert raised


def test_hard_timeout_off_by_default_is_backward_compatible():
    # Without hard_timeout the transport behaves as before (no worker thread): a good endpoint
    # just works. (The CLI/backfill path relies on this.)
    counts = {}
    _install({GOOD: _good}, counts)
    r = Rpc([GOOD], retries=2, min_interval=0.0, timeout=5.0)
    assert r.hard_timeout is None
    assert r.chain_id() == 999


# --------------------------------------------------------------------- get_logs_chunked bounded
class _LogsRpc:
    """Duck-typed rpc for get_logs_chunked: `fail_pattern(call_no)` -> True raises RpcError."""
    def __init__(self, fail_pattern):
        self.calls = 0
        self.windows = []
        self._fail = fail_pattern

    def get_logs(self, address, topics, lo, hi):
        self.calls += 1
        if self._fail(self.calls):
            raise rpc_mod.RpcError(-32005, "rate limited")
        self.windows.append((lo, hi))
        return [{"lo": lo, "hi": hi}]


def test_get_logs_chunked_persistent_failure_raises_bounded():
    # THE former infinite spin: chunk floor is 500, so on a window >=2 blocks a persistent error
    # used to `continue` forever (raise only reachable at hi==lo). Now: a bounded number of
    # consecutive failures -> RpcError up to the caller, quickly.
    r = _LogsRpc(lambda n: True)                 # every endpoint call fails, forever
    t0 = time.time()
    raised = False
    try:
        rpc_mod.get_logs_chunked(r, "0xpool", [], 0, 9_999, chunk=4_000)
    except rpc_mod.RpcError:
        raised = True
    assert raised, "persistent getLogs failure must raise, not spin"
    assert r.calls <= 6, f"gave up only after {r.calls} attempts (must be bounded)"
    assert time.time() - t0 < 2.0


def test_get_logs_chunked_single_block_window_still_raises_fast():
    # the pre-existing hi==lo escape hatch must survive: a 1-block window raises on first failure
    r = _LogsRpc(lambda n: True)
    raised = False
    try:
        rpc_mod.get_logs_chunked(r, "0xpool", [], 42, 42, chunk=4_000)
    except rpc_mod.RpcError:
        raised = True
    assert raised and r.calls == 1


def test_get_logs_chunked_sporadic_failures_still_complete():
    # the counter is CONSECUTIVE and resets on success: a long run with a transient error before
    # every window (6+ total failures, never 2 in a row) must still complete the whole range —
    # the backfill path must not be aborted by an accumulated count.
    r = _LogsRpc(lambda n: n % 2 == 1)           # odd calls fail, even succeed
    logs = rpc_mod.get_logs_chunked(r, "0xpool", [], 0, 3_999, chunk=500)
    assert len(logs) == 8                        # 8 windows of 500 covering 0..3999
    assert r.windows[0] == (0, 499) and r.windows[-1] == (3_500, 3_999)
    covered = sum(hi - lo + 1 for lo, hi in r.windows)
    assert covered == 4_000, "every block of the range must be swept exactly once"


if __name__ == "__main__":
    import sys
    _orig = rpc_mod._urlopen
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
        finally:
            rpc_mod._urlopen = _orig
    _release.set()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
