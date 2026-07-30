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
import urllib.request

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


# --- fleet bot tag ---------------------------------------------------------------------------

def _capture_alerts(fn, wait_n=0):
    """Run fn() with the Telegram POST intercepted; return the alert texts that went out."""
    import tempfile
    import urllib.parse
    import urllib.request
    sent = []

    def fake_urlopen(req, timeout=None):
        sent.append(urllib.parse.parse_qs(req.data.decode())["text"][0])
        return None

    # let any alert_async daemon thread left over from an EARLIER test finish before we hijack the
    # transport, so its text cannot be miscounted as one of ours
    for t in list(threading.enumerate()):
        if t is not threading.current_thread():
            t.join(1.0)
    o_open, o_env, o_chat = urllib.request.urlopen, C.TG_ENV_FILE, C.TG_CHAT_ID
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
        f.write("TELEGRAM_BOT_TOKEN=tok\n")
        env = f.name
    urllib.request.urlopen, C.TG_ENV_FILE, C.TG_CHAT_ID = fake_urlopen, env, "-100123"
    # Opt back in past _tg_muted(): the transport above is a FAKE, and these tests exist to check
    # the alert text itself. Scoped to this helper and restored in finally — every other test in
    # the suite stays muted by default, which is the point of the guard.
    o_mute = os.environ.get("HL_MUTE_TG")
    os.environ["HL_MUTE_TG"] = "0"
    try:
        fn()
        deadline = time.time() + 3.0          # alert_async posts from a daemon thread
        while time.time() < deadline and len(sent) < wait_n:
            time.sleep(0.01)
    finally:
        urllib.request.urlopen, C.TG_ENV_FILE, C.TG_CHAT_ID = o_open, o_env, o_chat
        if o_mute is None:
            os.environ.pop("HL_MUTE_TG", None)
        else:
            os.environ["HL_MUTE_TG"] = o_mute
        os.unlink(env)
    return sent


def test_alert_text_carries_bot_tag():
    """The fleet posts into ONE Telegram chat, so every outgoing alert must name its bot."""
    sent = _capture_alerts(lambda: E.alert("\U0001f52b LIQUIDATE 0xdead, sending\u2026"), wait_n=1)
    assert len(sent) == 1, sent
    assert sent[0].startswith(f"[{C.BOT_TAG}] "), sent[0]
    assert "LIQUIDATE 0xdead" in sent[0]


def test_a_test_run_can_never_reach_the_real_chat():
    """THE 19.07 INCIDENT: a test run posted fixtures into the operator's live Telegram —
    borrower `0xabababab…`, tx `0xffff…ffff`, and the literal string 'not-an-address'.

    The transport must refuse while any test module of this repo is loaded, WITHOUT the test
    having to remember a stub. This is the last line of defence: if it goes red, some test is
    one forgotten stub away from spamming a human."""
    hit = []

    def exploding_urlopen(req, timeout=None):
        hit.append(req)
        raise AssertionError("a test reached the REAL Telegram transport")

    o_open, o_env, o_chat = urllib.request.urlopen, C.TG_ENV_FILE, C.TG_CHAT_ID
    o_mute = os.environ.pop("HL_MUTE_TG", None)          # rely on auto-detect, not the env
    urllib.request.urlopen, C.TG_CHAT_ID = exploding_urlopen, "-100123"
    try:
        E.alert("\U0001f52b LIQUIDATE HF=0.9500 0xabababab…, sent: 0x" + "f" * 64)
        E.alert_async("⚠️ send error … Got: 'not-an-address'")
        deadline = time.time() + 2.0
        while time.time() < deadline and not hit:
            time.sleep(0.01)
    finally:
        urllib.request.urlopen, C.TG_ENV_FILE, C.TG_CHAT_ID = o_open, o_env, o_chat
        if o_mute is not None:
            os.environ["HL_MUTE_TG"] = o_mute
    assert not hit, "the mute guard let a test message through to Telegram"


def test_mute_guard_does_not_gag_production():
    """The guard must not become an outage: with no test module loaded and no override, alerts
    still go out. Simulated by asking _tg_muted() with the detection inputs cleared."""
    o_mute = os.environ.pop("HL_MUTE_TG", None)
    o_mods = {m: sys.modules[m] for m in list(sys.modules)
              if m.startswith(("bot.test_", "analysis.test_"))}
    for m in o_mods:
        del sys.modules[m]
    main = sys.modules.get("__main__")                 # the runner itself is a test_*.py file;
    o_file = getattr(main, "__file__", None)           # clear BOTH detection inputs
    if main is not None:
        main.__file__ = "/opt/bot/executor.py"
    try:
        assert E._tg_muted() is False, "production would be silenced"
    finally:
        sys.modules.update(o_mods)
        if main is not None:
            if o_file is None:
                del main.__file__
            else:
                main.__file__ = o_file
        if o_mute is not None:
            os.environ["HL_MUTE_TG"] = o_mute


def test_bot_tag_applied_exactly_once_through_async_wrapper():
    """alert_async() hands its text straight to alert(), the ONLY tagging point — the fire path
    must never emit a doubled "[hyperlend] [hyperlend] ..." prefix."""
    def _go():
        E.alert_async("liq ok")               # wrapper path (daemon thread)
        E.alert(f"[{C.BOT_TAG}] liq ok")      # pre-tagged text must not be tagged twice
    sent = _capture_alerts(_go, wait_n=2)
    assert sent == [f"[{C.BOT_TAG}] liq ok"] * 2, sent


# =============================================================== raw-tx path (HL_RAW_TX=1)
# The 2026-07-19 latency fix: `cast send` held the process 4.4s waiting for a receipt, so in a
# cascade the second target idled for the first target's whole confirmation. fire() now ends at
# the broadcast and _check_pending() reaps receipts on later passes.

_ENC_T = {"coll_asset": "0x" + "c" * 40, "debt_asset": "0x" + "d" * 40,
          "borrower": "0x" + "ab" * 20}
_ENC_EV = {"debt_to_cover": 1_000 * 10 ** 6, "swap_target": "0x" + "e" * 40,
           "swap_calldata": "0xcd", "min_profit_wei": 1}
_MAX_U256 = 2 ** 256 - 1

# (t, ev) pairs spanning the encoder's edges: empty bytes, a swap_calldata that is NOT a whole
# number of 32-byte words (37 bytes -> needs tail padding), max uint256 in both integer slots,
# and mixed-case addresses.
_ENC_CASES = [
    ("typical", _ENC_T, _ENC_EV),
    ("empty bytes + zero ints",
     _ENC_T, {**_ENC_EV, "swap_calldata": "0x", "debt_to_cover": 0, "min_profit_wei": 0}),
    ("max uint256 + odd-length calldata + mixed case",
     {"coll_asset": "0x" + "C" * 40, "debt_asset": "0x" + "D" * 40,
      "borrower": "0x" + "Ab" * 20},
     {**_ENC_EV, "swap_calldata": "0x" + "41" * 37, "debt_to_cover": _MAX_U256,
      "min_profit_wei": _MAX_U256}),
    ("long multi-word calldata",
     _ENC_T, {**_ENC_EV, "swap_calldata": "0x" + "ff" * 100, "debt_to_cover": 12345,
              "min_profit_wei": 999}),
]


def test_liquidate_selector_is_derived_not_pasted():
    """Selector and arg types come from LIQUIDATE_SIG at import time (project rule: never paste a
    hash). If the signature is ever edited, these move with it — and the cast test below is what
    proves the derivation itself is right."""
    from eth_utils import keccak
    assert E.LIQUIDATE_SELECTOR == "0x" + keccak(text=E.LIQUIDATE_SIG)[:4].hex()
    assert E.LIQUIDATE_TYPES == ["address", "address", "address", "uint256", "bool",
                                 "address", "bytes", "uint256"]


def test_encode_liquidate_matches_cast_calldata_byte_for_byte():
    """THE guard on the whole raw-tx path: our in-process ABI encoding must be byte-identical to
    `cast calldata`, which is what the (still-default) fallback path feeds the chain. A silent
    encoding difference means a revert at best and a wrong-argument liquidation at worst.

    SKIPS LOUDLY (unittest.SkipTest, reported by the runner) when `cast` is absent — a silent
    pass here would retire the only real oracle we have for the encoder."""
    import shutil
    import subprocess as sp
    import unittest
    cast = shutil.which("cast")
    if not cast:
        raise unittest.SkipTest("foundry `cast` not on PATH — cannot cross-check the encoding")
    o_flash = C.USE_FLASHLOAN
    try:
        for flash in (True, False):
            C.USE_FLASHLOAN = flash
            for name, t, ev in _ENC_CASES:
                mine = E._encode_liquidate(t, ev)
                ref = sp.run(
                    [cast, "calldata", E.LIQUIDATE_SIG, t["coll_asset"], t["debt_asset"],
                     t["borrower"], str(ev["debt_to_cover"]), "true" if flash else "false",
                     ev["swap_target"], ev["swap_calldata"], str(ev["min_profit_wei"])],
                    capture_output=True, text=True, timeout=60)
                assert ref.returncode == 0, f"cast failed on {name}: {ref.stderr}"
                assert mine.lower() == ref.stdout.strip().lower(), (
                    f"calldata mismatch [{name}, flash={flash}]\n  ours: {mine}\n  cast: "
                    f"{ref.stdout.strip()}")
    finally:
        C.USE_FLASHLOAN = o_flash


def test_encode_liquidate_accepts_lowercase_and_checksummed_addresses_alike():
    """Addresses reach the encoder in whatever case the book/quote produced; eth_abi rejects a
    non-checksummed mixed-case address, so _encode_liquidate must normalize."""
    from eth_utils import to_checksum_address
    lower = {k: v.lower() for k, v in _ENC_T.items()}
    upper = {k: to_checksum_address(v) for k, v in _ENC_T.items()}
    assert E._encode_liquidate(lower, _ENC_EV) == E._encode_liquidate(upper, _ENC_EV)


# ------------------------------------------------------------------- sign + broadcast
_TEST_KEY = "0x" + "11" * 32
_TEST_CONTRACT = "0x" + "1" * 40


class _FakeWrite:
    """Stand-in for the write JSON-RPC endpoint: canned nonce/baseFee, scriptable send."""

    def __init__(self, pending_nonce=7, send=lambda raw: "0x" + "f" * 64):
        self.pending_nonce = pending_nonce
        self.send = send
        self.calls = []

    def __call__(self, method, params, budget=None):
        self.calls.append((method, params))
        if method == "eth_getTransactionCount":
            return hex(self.pending_nonce)
        if method == "eth_getBlockByNumber":
            return {"baseFeePerGas": hex(10 ** 9)}
        if method == "eth_gasPrice":
            return hex(2 * 10 ** 9)
        if method == "eth_sendRawTransaction":
            return self.send(params[0])
        raise AssertionError(f"unexpected write method {method}")


def _armed_raw(fake_write, contract=_TEST_CONTRACT):
    """Context: live-armed on the raw-tx path with the write endpoint faked."""
    o = (C.DRY_RUN, C.CONTRACT, C.PRIVATE_KEY, C.RAW_TX, E._rpc_write,
         E._nonce_cache.copy(), E._owner_addr)
    C.DRY_RUN, C.CONTRACT, C.PRIVATE_KEY, C.RAW_TX = False, contract, _TEST_KEY, True
    E._rpc_write = fake_write
    E._nonce_cache.update({"addr": None, "next": None, "send_ts": 0.0})
    E._owner_addr = None
    return o


def _restore_raw(o):
    (C.DRY_RUN, C.CONTRACT, C.PRIVATE_KEY, C.RAW_TX, E._rpc_write, cache, E._owner_addr) = o
    E._nonce_cache.clear()
    E._nonce_cache.update(cache)


def _capture_signed_tx():
    """Patch Account.sign_transaction to record the tx dict while still really signing it."""
    import eth_account
    seen = []
    orig = eth_account.Account.sign_transaction

    def spy(tx, key, **kw):
        seen.append(dict(tx))
        return orig(tx, key, **kw)

    eth_account.Account.sign_transaction = spy
    return seen, orig


def test_sign_and_send_checksums_the_to_address():
    """`to` must be checksummed before signing. eth_account raises on a mixed-case address that
    is not correctly checksummed — the class of bug that killed a WC shot — so the fire path must
    never hand C.CONTRACT through raw, whatever case it was configured in."""
    from eth_utils import to_checksum_address
    import eth_account
    mixed = "0x" + "aB" * 20                       # mixed case, NOT valid EIP-55 checksum
    fw = _FakeWrite()
    o = _armed_raw(fw, contract=mixed)
    seen, orig = _capture_signed_tx()
    try:
        txh = E._sign_and_send("0xdeadbeef")
    finally:
        eth_account.Account.sign_transaction = orig
        _restore_raw(o)
    assert txh == "0x" + "f" * 64
    assert len(seen) == 1
    tx = seen[0]
    assert tx["to"] == to_checksum_address(mixed), f"`to` not checksummed: {tx['to']}"
    assert tx["chainId"] == 999 and tx["gas"] == C.GAS_LIMIT
    # EIP-1559 shape: maxFee = baseFee*2 + priority, priority straight from config
    assert tx["maxPriorityFeePerGas"] == int(C.PRIORITY_GWEI * 1e9)
    assert tx["maxFeePerGas"] == 10 ** 9 * 2 + tx["maxPriorityFeePerGas"]


def test_fee_params_falls_back_to_gas_price_without_base_fee():
    def no_base(method, params, budget=None):
        if method == "eth_getBlockByNumber":
            raise RuntimeError("no baseFeePerGas here")
        if method == "eth_gasPrice":
            return hex(3 * 10 ** 9)
        raise AssertionError(method)

    o_w, o_p = E._rpc_write, C.PRIORITY_GWEI
    E._rpc_write, C.PRIORITY_GWEI = no_base, 0.0
    try:
        max_fee, priority = E._fee_params()
    finally:
        E._rpc_write, C.PRIORITY_GWEI = o_w, o_p
    assert priority == 0 and max_fee == 3 * 10 ** 9


def test_nonce_advances_only_after_a_successful_broadcast():
    """The local counter sits on top of 'pending' so a cascade doesn't reuse a nonce the node has
    not seen yet — but it must move ONLY on success. Advancing it on a failed send would leave a
    hole that stalls every later tx behind it."""
    fw = _FakeWrite(pending_nonce=7)
    o = _armed_raw(fw)
    try:
        E._sign_and_send("0xaa")
        assert E._nonce_cache["next"] == 8, E._nonce_cache
        # node hasn't seen it yet: pending still 7, local counter wins
        E._sign_and_send("0xbb")
        assert E._nonce_cache["next"] == 9, E._nonce_cache
        assert sum(1 for m, _ in fw.calls if m == "eth_sendRawTransaction") == 2
        # chain moved past the local counter (external tx from the same key): chain wins
        fw.pending_nonce = 20
        E._sign_and_send("0xcc")
        assert E._nonce_cache["next"] == 21, E._nonce_cache
    finally:
        _restore_raw(o)


def _verdict(msg="invalid sender"):
    """A node's in-body JSON-RPC refusal — the ONLY kind of send failure that proves the tx is in
    no mempool. This is what _rpc_write raises for an `error` field in the response body."""
    def boom(raw):
        raise E.RpcVerdict(f"rpc eth_sendRawTransaction: "
                           f"{{'code': -32000, 'message': '{msg}'}}")
    return boom


def test_nonce_does_not_move_when_the_node_refuses_the_tx():
    """A REFUSED tx (node verdict) is in no mempool, so its nonce must stay free — advancing it
    would leave a hole that stalls every later tx behind it.

    Was `connection reset by peer`, which is the opposite case: that proves nothing about
    delivery, and the nonce must move (see test_ambiguous_broadcast_owns_its_nonce)."""
    fw = _FakeWrite(pending_nonce=7, send=_verdict())
    o = _armed_raw(fw)
    try:
        E._sign_and_send("0xaa")
    except E.SendUndelivered:
        pass
    except Exception as e:
        raise AssertionError(f"a node verdict must surface as SendUndelivered, got {e!r}")
    else:
        raise AssertionError("a refused broadcast must propagate")
    finally:
        cache = dict(E._nonce_cache)
        _restore_raw(o)
    assert cache["next"] is None, f"nonce advanced on a REFUSED send: {cache}"


def test_ambiguous_broadcast_owns_its_nonce():
    """A tx we cannot rule out is live must keep its nonce. Reusing it means two signed txs
    competing for one slot — one of them is guaranteed to be wasted gas, and if the loser is the
    one that mines, the liquidation we thought we sent never happened."""
    fw = _FakeWrite(pending_nonce=7,
                    send=lambda raw: (_ for _ in ()).throw(TimeoutError("hard deadline 10s")))
    o = _armed_raw(fw)
    try:
        E._sign_and_send("0xaa")
    except E.SendAmbiguous as e:
        assert e.tx_hash.startswith("0x") and len(e.tx_hash) == 66, e.tx_hash
    else:
        raise AssertionError("an ambiguous broadcast must propagate")
    finally:
        cache = dict(E._nonce_cache)
        _restore_raw(o)
    assert cache["next"] == 8, f"maybe-sent tx did not own its nonce: {cache}"


def test_already_known_is_a_delivered_send_not_a_refusal():
    """"already known" means the node HAS the tx in its pool — a duplicate ack, not a rejection.
    Booking it as a send error would abandon a tx that is on its way to being mined."""
    fw = _FakeWrite(pending_nonce=7, send=_verdict("already known"))
    o = _armed_raw(fw)
    try:
        txh = E._sign_and_send("0xaa")
        assert txh.startswith("0x") and len(txh) == 66, txh
        assert E._nonce_cache["next"] == 8, E._nonce_cache
    finally:
        _restore_raw(o)


def test_local_nonce_resyncs_down_once_the_send_window_expires():
    """A tx evicted from the mempool sends the node's 'pending' back to N while the local counter
    sits at N+1. The counter used to be monotonic, so every later shot was signed past a hole the
    sequencer will never fill and NOTHING mined again until a restart — a silently disarmed
    liquidator. Outside the send window the chain must win even though it is lower."""
    fw = _FakeWrite(pending_nonce=7)
    o = _armed_raw(fw)
    try:
        E._sign_and_send("0xaa")
        assert E._nonce_cache["next"] == 8
        fw.pending_nonce = 7                       # tx dropped: the node is back at 7 for good
        # still inside the window: the bump is a still-propagating send, keep it
        assert E._pending_nonce(E.owner_address()) == 8
        # ...and once the window closes, heal back down to the chain
        E._nonce_cache["send_ts"] = time.monotonic() - E.NONCE_SEND_WINDOW_SEC - 1
        assert E._pending_nonce(E.owner_address()) == 7, E._nonce_cache
        assert E._nonce_cache["next"] == 7, "the dead bump must be dropped, not just ignored"
        # and it must STAY healed — nothing but a real send may refresh send_ts
        for _ in range(5):
            assert E._pending_nonce(E.owner_address()) == 7
    finally:
        _restore_raw(o)


# ------------------------------------------------------------------- fire(): non-blocking
def _fire_raw_once(fake_write, st=None, gas_usd=0.01):
    st = st if st is not None else _fresh_state()
    o = _armed_raw(fake_write)
    try:
        E.fire(dict(_T), dict(_EV), st, 1_000_000.0, gas_usd)
    finally:
        _restore_raw(o)
    return st


def test_fire_raw_returns_immediately_and_leaves_the_tx_pending():
    """The whole point: fire() ends at the broadcast. No receipt poll, no `cast` subprocess —
    the next target in a cascade is evaluated now, not 4.4s from now."""
    ran = []
    o_run = E.subprocess.run
    E.subprocess.run = lambda *a, **kw: ran.append(a) or _CastOk()
    try:
        st = _fire_raw_once(_FakeWrite())
    finally:
        E.subprocess.run = o_run
    assert not ran, "raw path must never shell out to cast"
    rec = st["sent"][_T["borrower"]]
    assert rec["status"] == "pending", rec
    assert rec["tx"] == "0x" + "f" * 64
    assert st["fires"] == 1
    assert st["consec_reverts"] == 0          # nothing is decided yet
    assert st["reverts"] == 0


def test_fire_raw_never_waits_on_telegram():
    """Same ordering guarantee the cast path already had: a trickling api.telegram.org must not
    sit between the decision and the wire."""
    events = []
    release = threading.Event()

    def blocked_alert(text):
        release.wait(5)
        events.append(("alert", text))

    fw = _FakeWrite(send=lambda raw: events.append(("send", raw)) or "0x" + "f" * 64)
    o_alert = E.alert
    E.alert = blocked_alert
    try:
        st = _fire_raw_once(fw)
        assert events and events[0][0] == "send", f"broadcast must precede any alert: {events}"
        assert st["sent"][_T["borrower"]]["status"] == "pending"
    finally:
        release.set()
        E.alert = o_alert


def test_fire_raw_send_error_is_not_a_revert():
    """FLEET DISCIPLINE: a PROVEN non-delivery — here the node's own verdict — means nothing
    executed on-chain. Feeding it to consec_reverts would let an RPC brownout trip the kill-switch
    and take the bot down for the entire crash window — the only window it earns in."""
    fw = _FakeWrite(send=_verdict("invalid sender"))
    st = _fire_raw_once(fw)
    rec = st["sent"][_T["borrower"]]
    assert rec["status"] == "send_error", rec
    assert st["consec_reverts"] == 0, "send error must NOT feed the kill-switch"
    assert st["reverts"] == 0
    assert st["fires"] == 0, "nothing was broadcast — not a fire"
    assert st["gas_usd"] == 0.0, "no gas is burned by a tx that never reached the chain"


def test_fire_raw_encode_failure_is_a_send_error():
    """A failure BEFORE the wire (bad calldata, missing key) is proven non-delivery too."""
    bad_ev = dict(_EV, swap_target="not-an-address")
    o = _armed_raw(_FakeWrite())
    st = _fresh_state()
    try:
        E.fire(dict(_T), bad_ev, st, 1_000_000.0, 0.01)
    finally:
        _restore_raw(o)
    assert st["sent"][_T["borrower"]]["status"] == "send_error", st["sent"]
    assert st["fires"] == 0 and st["gas_usd"] == 0.0


def test_ambiguous_broadcast_is_tracked_as_pending_not_lost():
    """[BLOCKER] The node accepted the tx; our 10s wall fired before the answer came back. The
    old path called that a send_error: no pending record, so the real gas burned outside the $5
    daily cap, a revert could never reach consec_reverts, and DEDUP_SEC freed the borrower after
    60s — a SECOND liquidation of a position the first tx had already cured. The tx must be
    tracked on its locally computed hash so a receipt still settles it."""
    def node_took_it_but_never_answered(raw):
        raise TimeoutError("hard deadline 10.0s exceeded")

    fw = _FakeWrite(send=node_took_it_but_never_answered)
    st = _fire_raw_once(fw, gas_usd=0.62)
    rec = st["sent"][_T["borrower"]]
    assert rec["status"] == "pending", f"an ambiguous send must NOT be dropped: {rec}"
    assert rec["tx"].startswith("0x") and len(rec["tx"]) == 66, rec
    assert st["fires"] == 1
    assert st["gas_usd"] == 0.62, "gas that may really burn must sit under the daily cap"
    assert st["consec_reverts"] == 0, "an unknown outcome is not a revert"
    # and the borrower is blocked until that record settles — no second shot on a 60s dedup
    assert E.recently_fired(st, _T["borrower"], 1_000_000.0 + C.DEDUP_SEC + 1)


def test_ambiguous_broadcast_hash_matches_the_hash_the_node_would_report():
    """The tracked hash is only useful if it is the SAME hash the chain will index the tx under —
    it is derived from the signed payload, so it must equal what a healthy send returns."""
    seen = {}
    fw_ok = _FakeWrite(send=lambda raw: seen.setdefault("raw", raw) or "0x" + "f" * 64)
    o = _armed_raw(fw_ok)
    try:
        E._sign_and_send("0xdeadbeef")
    finally:
        _restore_raw(o)
    fw_amb = _FakeWrite(send=lambda raw: (_ for _ in ()).throw(TimeoutError("wall")))
    o = _armed_raw(fw_amb)
    try:
        E._sign_and_send("0xdeadbeef")
    except E.SendAmbiguous as e:
        got = e.tx_hash
    finally:
        _restore_raw(o)
    from eth_utils import keccak
    from hexbytes import HexBytes
    assert got == "0x" + keccak(HexBytes(seen["raw"])).hex(), got


def test_fire_raw_charges_provisional_gas_exactly_once():
    st = _fire_raw_once(_FakeWrite(), gas_usd=0.25)
    assert st["gas_usd"] == 0.25
    assert st["sent"][_T["borrower"]]["gas_usd"] == 0.25   # carried for an exact undo on settle


# ------------------------------------------------------------------- _check_pending
class _FakeRcptRpc:
    """Read-side stand-in: maps tx hash -> receipt (or None for 'not mined yet')."""

    def __init__(self, receipts: dict):
        self.receipts = receipts
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        assert method == "eth_getTransactionReceipt", method
        r = self.receipts.get(params[0])
        if isinstance(r, Exception):
            raise r
        return r


_TXH = "0x" + "f" * 64


def _rcpt(status="0x1", gas_used=1_000_000, price=10 ** 9):
    return {"status": status, "gasUsed": hex(gas_used), "effectiveGasPrice": hex(price),
            "blockNumber": "0x64"}


def _pending_state(gas_usd=0.5, ts=1_000_000.0):
    return {"fires": 1, "gas_usd": gas_usd, "consec_reverts": 0, "reverts": 0,
            "sent": {"0xborrower": {"ts": ts, "status": "pending", "tx": _TXH,
                                    "gas_usd": gas_usd}}}


def _run_check(rpc, st, now_ts=1_000_010.0):
    """alert_async is stubbed out: these tests are about state, and its daemon threads would
    otherwise still be in flight when a LATER test patches the Telegram transport to capture its
    own alerts — a cross-test race, not a product bug."""
    o_dry, o_async = C.DRY_RUN, E.alert_async
    C.DRY_RUN, E.alert_async = False, lambda text: None
    try:
        E._check_pending(rpc, st, now_ts)
    finally:
        C.DRY_RUN, E.alert_async = o_dry, o_async
    return st


def test_check_pending_settles_a_win():
    st = _pending_state(gas_usd=0.5)
    _run_check(_FakeRcptRpc({_TXH: _rcpt("0x1")}), st)
    rec = st["sent"]["0xborrower"]
    assert rec["status"] == "ok", rec
    assert st["consec_reverts"] == 0
    assert st["reverts"] == 0
    # provisional 0.5 undone, actual 1e6 gas * 1 gwei * HYPE_USD booked instead — ONCE
    expected = 1_000_000 * 10 ** 9 / 1e18 * C.HYPE_USD
    assert abs(st["gas_usd"] - expected) < 1e-9, st["gas_usd"]


def test_check_pending_settles_a_revert_and_feeds_the_kill_switch():
    """Only receipt.status == 0 is a revert — this is the ONLY thing that may increment
    consec_reverts on the raw path."""
    st = _pending_state()
    _run_check(_FakeRcptRpc({_TXH: _rcpt("0x0")}), st)
    assert st["sent"]["0xborrower"]["status"] == "revert"
    assert st["consec_reverts"] == 1
    assert st["reverts"] == 1


def test_check_pending_charges_gas_exactly_once_per_tx():
    """Re-running the pass must not re-charge: once settled the record is no longer pending."""
    st = _pending_state(gas_usd=0.5)
    rpc = _FakeRcptRpc({_TXH: _rcpt("0x1")})
    _run_check(rpc, st)
    after_first = st["gas_usd"]
    _run_check(rpc, st)
    _run_check(rpc, st)
    assert st["gas_usd"] == after_first, "gas re-charged on a later pass"
    assert len(rpc.calls) == 1, "a settled tx must not be re-read"


def test_check_pending_leaves_unmined_tx_pending_then_marks_it_stale():
    st = _pending_state(ts=1_000_000.0)
    rpc = _FakeRcptRpc({_TXH: None})               # not mined
    _run_check(rpc, st, now_ts=1_000_000.0 + E.PENDING_STALE_SEC - 1)
    assert st["sent"]["0xborrower"]["status"] == "pending"
    assert st["gas_usd"] == 0.5, "an unmined tx must not be reconciled"
    _run_check(rpc, st, now_ts=1_000_000.0 + E.PENDING_STALE_SEC + 1)
    assert st["sent"]["0xborrower"]["status"] == "stale"
    assert st["consec_reverts"] == 0, "a stuck nonce is not a revert"


def test_check_pending_survives_a_malformed_receipt():
    """status:null (and friends) must leave the record pending for a re-read, NOT TypeError out of
    the pass — that is the silent-zombie failure: heartbeat alive, detection dead."""
    st = _pending_state()
    _run_check(_FakeRcptRpc({_TXH: {"status": None}}), st)
    assert st["sent"]["0xborrower"]["status"] == "pending", st["sent"]
    assert st["consec_reverts"] == 0 and st["reverts"] == 0
    assert st["gas_usd"] == 0.5, "nothing is decided, so nothing is reconciled"
    assert E._rcpt_status({"status": None}) is None
    assert E._rcpt_status({}) is None
    assert E._rcpt_status({"status": "junk"}) is None


def test_check_pending_one_bad_tx_never_kills_the_pass():
    """Per-tx try/except: an endpoint blowing up on one hash must not stop the others settling."""
    good = "0x" + "a" * 64
    st = _pending_state()
    st["sent"]["0xbad"] = {"ts": 1_000_000.0, "status": "pending", "tx": _TXH, "gas_usd": 0.5}
    st["sent"]["0xgood"] = {"ts": 1_000_000.0, "status": "pending", "tx": good, "gas_usd": 0.1}
    del st["sent"]["0xborrower"]
    rpc = _FakeRcptRpc({_TXH: RuntimeError("endpoint exploded"), good: _rcpt("0x1")})
    _run_check(rpc, st)
    assert st["sent"]["0xbad"]["status"] == "pending", "failed read must stay pending for a retry"
    assert st["sent"]["0xgood"]["status"] == "ok", "the healthy tx must still settle"


def test_pending_target_is_not_refired_before_it_settles():
    """DEDUP_SEC (60s) is far shorter than the 600s stale wall, so an age-only rule would fire a
    SECOND liquidation of the same borrower while the first is still in flight."""
    st = {"sent": {"0xabc": {"ts": 1000.0, "status": "pending", "tx": _TXH}}}
    assert E.recently_fired(st, "0xabc", 1000.0 + 5)
    assert E.recently_fired(st, "0xabc", 1000.0 + C.DEDUP_SEC + 1)
    assert E.recently_fired(st, "0xabc", 1000.0 + 10_000)
    st["sent"]["0xabc"]["status"] = "ok"           # settled -> normal dedup applies again
    assert not E.recently_fired(st, "0xabc", 1000.0 + C.DEDUP_SEC + 1)


def test_gas_settle_after_the_utc_roll_cannot_go_negative():
    """[MAJOR] Shots fired just before midnight UTC settle just after it. _roll_day zeroes the
    counter and the settle then refunded each provisional charge out of the FRESH day — measured
    -$4.94 with 8 in-flight shots at $0.62, i.e. the $5 cap silently became a ~$10 cap on the day
    after a busy one, which is exactly the day after a crash cascade."""
    st = {"day": "2026-07-19", "gas_usd": 0.0, "consec_reverts": 0, "reverts": 0, "sent": {}}
    for i in range(8):                                  # 8 shots on the 19th, $0.62 provisional
        st["sent"][f"0xb{i}"] = {"ts": 1_000_000.0, "status": "pending", "tx": "0x" + f"{i:064x}",
                                 "gas_usd": 0.62, "gas_day": "2026-07-19"}
    st["gas_usd"] = 8 * 0.62
    E._roll_day(st, "2026-07-20")                       # midnight UTC
    assert st["gas_usd"] == 0.0 and st["day"] == "2026-07-20"
    rpc = _FakeRcptRpc({rec["tx"]: _rcpt("0x1", gas_used=1, price=1)
                        for rec in list(st["sent"].values())})
    _run_check(rpc, st)
    assert st["gas_usd"] >= 0.0, f"yesterday's charges refunded into today's budget: {st}"
    assert all(r["status"] == "ok" for r in st["sent"].values())


def test_gas_settle_undoes_the_provisional_charge_within_the_same_day():
    """The day guard must not break the normal case: same-day settle still swaps the estimate for
    the real cost, so one shot is never charged twice."""
    st = {"day": "2026-07-19", "gas_usd": 0.5, "consec_reverts": 0, "reverts": 0,
          "sent": {"0xb": {"ts": 1_000_000.0, "status": "pending", "tx": _TXH,
                           "gas_usd": 0.5, "gas_day": "2026-07-19"}}}
    _run_check(_FakeRcptRpc({_TXH: _rcpt("0x1")}), st)
    assert abs(st["gas_usd"] - 1_000_000 * 10 ** 9 / 1e18 * C.HYPE_USD) < 1e-9, st["gas_usd"]


def test_pending_is_released_even_when_the_receipt_read_keeps_throwing():
    """[MAJOR] The stale wall used to live inside the `not mined yet` branch, reachable only when
    an endpoint answered cleanly. With the endpoint down the read threw every pass, the record
    stayed "pending" — and recently_fired() blocks a pending target regardless of age — so one
    dead endpoint locked the borrower out of the fire path until the 24h purge."""
    st = _pending_state(ts=1_000_000.0)
    rpc = _FakeRcptRpc({_TXH: RuntimeError("endpoint down")})
    _run_check(rpc, st, now_ts=1_000_000.0 + E.PENDING_STALE_SEC - 1)
    assert st["sent"]["0xborrower"]["status"] == "pending"
    assert E.recently_fired(st, "0xborrower", 1_000_000.0 + E.PENDING_STALE_SEC - 1)
    _run_check(rpc, st, now_ts=1_000_000.0 + E.PENDING_STALE_SEC + 1)
    assert st["sent"]["0xborrower"]["status"] == "stale", \
        f"a permanently failing read must still hit the stale wall: {st['sent']}"
    freed = 1_000_000.0 + E.PENDING_STALE_SEC + 1 + C.DEDUP_SEC
    assert not E.recently_fired(st, "0xborrower", freed), "target still blocked after the wall"


def test_a_stale_tx_that_mines_later_still_books_its_revert_and_gas():
    """[MAJOR] "stale" means unmined for 10 min, NOT decided — HyperEVM can mine it afterwards.
    Dropping stale records from the poll meant a late revert never reached consec_reverts and its
    gas was never reconciled, while the borrower had already been released: the kill-switch went
    blind to the exact pattern it exists to stop (a target that keeps reverting)."""
    st = _pending_state(gas_usd=0.5, ts=1_000_000.0)
    rpc = _FakeRcptRpc({_TXH: None})
    _run_check(rpc, st, now_ts=1_000_000.0 + E.PENDING_STALE_SEC + 1)
    assert st["sent"]["0xborrower"]["status"] == "stale"
    rpc.receipts[_TXH] = _rcpt("0x0", gas_used=900_000, price=10 ** 9)   # mined late, REVERTED
    # stale txs are polled at STALE_POLL_SEC, not every pass (24h of hot-loop reads otherwise)
    n = len(rpc.calls)
    _run_check(rpc, st, now_ts=1_000_000.0 + E.PENDING_STALE_SEC + 2)
    assert len(rpc.calls) == n, "stale poll must be throttled, not run every iteration"
    _run_check(rpc, st, now_ts=1_000_000.0 + E.PENDING_STALE_SEC + E.STALE_POLL_SEC + 1)
    assert st["sent"]["0xborrower"]["status"] == "revert", st["sent"]
    assert st["consec_reverts"] == 1, "a late revert must still feed the kill-switch"
    assert st["reverts"] == 1
    expected = 900_000 * 10 ** 9 / 1e18 * C.HYPE_USD
    assert abs(st["gas_usd"] - expected) < 1e-9, f"stale tx gas never reconciled: {st['gas_usd']}"


def test_receipt_without_effective_gas_price_falls_back_to_the_estimate():
    """[MINOR] `.get("effectiveGasPrice", "0x0")` scored such a receipt at $0.00 while real gas
    burned — the daily cap goes blind — where the same missing field on gasUsed correctly kept the
    estimate. Missing means unknown, not free."""
    st = _pending_state(gas_usd=0.42)
    rcpt = {"status": "0x1", "gasUsed": hex(1_000_000), "blockNumber": "0x64"}
    _run_check(_FakeRcptRpc({_TXH: rcpt}), st)
    assert st["sent"]["0xborrower"]["status"] == "ok"
    assert st["gas_usd"] == 0.42, f"gas silently zeroed: {st['gas_usd']}"


def test_integer_receipt_fields_are_parsed_not_treated_as_garbage():
    """[MINOR] A normalizing node/proxy returns status/gasUsed as JSON ints. Read as unparsable,
    the win was never booked and the borrower stayed blocked until the stale wall — the bot going
    quiet against a node that did nothing wrong."""
    assert E._rcpt_status({"status": 1}) == 1
    assert E._rcpt_status({"status": 0}) == 0
    assert E._rcpt_status({"status": "0x1"}) == 1        # hex still works
    assert E._rcpt_status({"status": None}) is None      # and garbage is still garbage
    assert E._rcpt_status({"status": "junk"}) is None
    st = _pending_state(gas_usd=0.5)
    _run_check(_FakeRcptRpc({_TXH: {"status": 1, "gasUsed": 1_000_000,
                                    "effectiveGasPrice": 10 ** 9}}), st)
    assert st["sent"]["0xborrower"]["status"] == "ok", st["sent"]
    expected = 1_000_000 * 10 ** 9 / 1e18 * C.HYPE_USD
    assert abs(st["gas_usd"] - expected) < 1e-9, st["gas_usd"]


def test_reset_keeps_in_flight_records_so_their_receipts_still_settle():
    """[MINOR] reset is the documented kill-switch recovery, run in a hurry mid-incident with txs
    on the wire. Wiping st["sent"] wholesale lost those: their gas was never reconciled and a
    revert landing afterwards never reached consec_reverts, so the operator re-armed the bot
    straight back into the failure that had just tripped it."""
    import json
    import tempfile
    st = {"day": "2026-07-19", "gas_usd": 4.9, "consec_reverts": 3, "passes": 1, "fires": 2,
          "reverts": 1, "sent": {
              "0xlive": {"ts": 1_000_000.0, "status": "pending", "tx": _TXH, "gas_usd": 0.6,
                         "gas_day": "2026-07-19"},
              "0xstuck": {"ts": 1_000_000.0, "status": "stale", "tx": "0x" + "a" * 64,
                          "gas_usd": 0.6, "gas_day": "2026-07-19"},
              "0xdone": {"ts": 1_000_000.0, "status": "ok", "tx": "0x" + "b" * 64},
              "0xold": {"ts": 1_000_000.0, "status": "revert", "tx": "0x" + "c" * 64}}}
    with tempfile.TemporaryDirectory() as d:
        o_file, o_argv = C.STATE_FILE, sys.argv
        C.STATE_FILE = os.path.join(d, "state.json")
        sys.argv = ["executor", "reset"]
        try:
            E.save_state(st)
            E.main()
            after = json.load(open(C.STATE_FILE))
        finally:
            C.STATE_FILE, sys.argv = o_file, o_argv
    assert after["consec_reverts"] == 0 and after["gas_usd"] == 0.0, after
    assert set(after["sent"]) == {"0xlive", "0xstuck"}, \
        f"reset dropped in-flight txs (or kept settled ones): {after['sent']}"
    # and the preserved charge cannot refund into the reset budget
    E._settle_gas(after, after["sent"]["0xlive"], _rcpt("0x1", gas_used=1, price=1))
    assert after["gas_usd"] >= 0.0, after["gas_usd"]


# ------------------------------------------------------------------- cast fallback: key hygiene
def test_cast_fallback_signs_via_argv_because_foundry_has_no_key_env():
    """REGRESSION GUARD for a self-inflicted outage.

    The key was briefly moved to ETH_PRIVATE_KEY to keep it out of `ps`. foundry 1.7.1 has no
    such binding (only ETH_KEYSTORE / ETH_KEYSTORE_ACCOUNT / ETH_PASSWORD), so cast could not
    sign: every fire exited non-zero, _fire_cast reads non-zero as REVERT, and three of those in
    one cascade trip the kill-switch and exit the process — a silently disarmed liquidator.

    So the key is on argv deliberately. The `ps` leak is a local-user read and is accepted until
    either a keystore lands or HL_RAW_TX=1 makes this path dead code. This test exists so nobody
    "fixes" the leak the same broken way twice: if you move the key off argv, you MUST prove cast
    can still sign (see the live canary, not a mock)."""
    seen = {}

    def fake_run(args, **kw):
        seen["args"] = list(args)
        seen["env"] = kw.get("env")
        return _CastOk()

    o_raw = C.RAW_TX
    C.RAW_TX = False
    try:
        st = _live_fire(lambda text: None, fake_run)
    finally:
        C.RAW_TX = o_raw
    key = "0x" + "2" * 64
    assert "--private-key" in seen["args"], seen["args"]
    assert seen["args"][seen["args"].index("--private-key") + 1] == key
    # and NOT via the env binding cast does not have — that is what broke it
    assert not (seen["env"] or {}).get("ETH_PRIVATE_KEY")
    assert st["sent"][_T["borrower"]]["status"] == "ok"


# Метки времени — эпохо-масштабные: в проде now_ts = time.time(), и троттл-окно
# (BALANCE_ALERT_SEC=3600) заведомо позади. Маленькие метки вроде 1000.0 глушили бы
# первый алерт троттлом и тестировали бы артефакт стенда, а не поведение бота.
_T0 = 1_800_000_000.0


def _balance_ctx(bal_wei, base_wei=10**8):
    """Context: live, with eth_getBalance/eth_getBlockByNumber faked for the gas guard."""
    o = (C.DRY_RUN, C.PRIVATE_KEY, E._rpc_write, E._owner_addr,
         E._last_balance_check, E._last_balance_alert, E.alert)
    C.DRY_RUN, C.PRIVATE_KEY = False, _TEST_KEY
    E._owner_addr = None
    E._last_balance_check = E._last_balance_alert = 0.0

    def fake_write(method, params, budget=None):
        if method == "eth_getBalance":
            return hex(bal_wei)
        if method == "eth_getBlockByNumber":
            return {"baseFeePerGas": hex(base_wei)}
        raise AssertionError(f"unexpected {method}")
    E._rpc_write = fake_write
    return o


def _balance_restore(o):
    (C.DRY_RUN, C.PRIVATE_KEY, E._rpc_write, E._owner_addr,
     E._last_balance_check, E._last_balance_alert, E.alert) = o


def test_balance_guard_blocks_when_underfunded():
    """Осушённый EOA обязан ловиться ГАРДОМ, а не штормом «insufficient funds» в каскаде."""
    o = _balance_ctx(bal_wei=1)                      # ~ноль
    alerts = []
    E.alert = lambda text, **kw: alerts.append(text)
    try:
        st = {}
        assert E.check_balance(st, _T0, force=True) is False
        assert any("LOW GAS BALANCE" in a for a in alerts), alerts
        assert st["balance_hype"] == 0.0
    finally:
        _balance_restore(o)


def test_balance_guard_passes_when_funded():
    # конверт = GAS_LIMIT*(2*base + prio); берём заведомо больше
    need = C.GAS_LIMIT * (2 * 10**8) * 10
    o = _balance_ctx(bal_wei=need)
    alerts = []
    E.alert = lambda text, **kw: alerts.append(text)
    try:
        st = {}
        assert E.check_balance(st, _T0, force=True) is True
        assert alerts == [], alerts                  # молчит, когда всё в порядке
        assert st["balance_hype"] > 0
    finally:
        _balance_restore(o)


def test_balance_guard_fails_open_on_rpc_error():
    """Флапающий RPC НЕ должен блокировать выстрел — гард падает открытым."""
    o = _balance_ctx(bal_wei=1)
    def boom(method, params, budget=None):
        raise RuntimeError("rpc down")
    E._rpc_write = boom
    E.alert = lambda text, **kw: None
    try:
        assert E.check_balance({}, _T0, force=True) is True
    finally:
        _balance_restore(o)


def test_balance_guard_force_bypasses_throttle():
    """Периодическая проверка троттлится, но гейт ПЕРЕД выстрелом обязан считать заново."""
    o = _balance_ctx(bal_wei=1)
    E.alert = lambda text, **kw: None
    try:
        E._last_balance_check = _T0               # только что проверяли
        assert E.check_balance({}, _T0 + 0.1) is True   # без force — троттл, пропускаем
        assert E.check_balance({}, _T0 + 0.1, force=True) is False   # force — считаем реально
    finally:
        _balance_restore(o)


def test_balance_alert_is_throttled():
    """Один ⛽-алерт в час, а не на каждой итерации горячего цикла."""
    o = _balance_ctx(bal_wei=1)
    alerts = []
    E.alert = lambda text, **kw: alerts.append(text)
    try:
        for i in range(5):
            E.check_balance({}, _T0 + i, force=True)
        assert len(alerts) == 1, alerts
    finally:
        _balance_restore(o)


if __name__ == "__main__":
    import unittest
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = skipped = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except unittest.SkipTest as e:
            skipped += 1
            print(f"SKIP {fn.__name__}: {e}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    tail = f", {skipped} skipped" if skipped else ""
    print(f"\n{len(fns) - failed - skipped}/{len(fns)} passed{tail}")
    sys.exit(1 if failed else 0)


# ------------------------------------------------------------- лестница чанков (30.07)
# Две крупнейшие позиции флота ($734k и $342k WHYPE→USDC, чистый бонус $32.9k и $15.3k) были
# недостижимы: evaluate() котировал ПОЛНЫЙ размер, получал impact 45–53% и отказывался. Бот при
# этом не ошибался — он корректно молчал, поэтому дыра не оставляла следа в логе.

_P = 100_000_000            # цена $1 в шкале оракула Aave (1e8)


def _chunkable(debt_usd=100_000, coll_usd=140_000):
    """Цель с сырыми входами сайзинга — как её теперь отдаёт _size_row."""
    debt_wei = debt_usd * 10 ** 6
    coll_wei = coll_usd * 10 ** 18 // 1        # цена коллатерала тоже $1 для простоты
    from analysis.aave import size_liquidation
    sz = size_liquidation(debt_wei, 6, _P, coll_wei, 18, _P, 11000, 1000,
                          debt_usd * 10 ** 8, int(1.0 * 1e18))
    t = {"borrower": "0x" + "ab" * 20, "hf": 0.99, "coll_sym": "WHYPE", "debt_sym": "USDC",
         "coll_asset": "0x" + "c" * 40, "debt_asset": "0x" + "d" * 40,
         "coll_dec": 18, "debt_dec": 6, "coll_price": _P, "debt_price": _P,
         "bonus_bps": 11000, "fee_bps": 1000,
         "debt_wei": debt_wei, "coll_wei": coll_wei,
         "total_debt_base": debt_usd * 10 ** 8, "hf_1e18": int(1.0 * 1e18)}
    t.update(sz)
    return t


def _with_quote(fn, t, gas_usd=0.01):
    orig = liqd.quote_for_seized
    liqd.quote_for_seized = fn
    try:
        return E.evaluate(t, gas_usd)
    finally:
        liqd.quote_for_seized = orig


def test_max_cover_caps_the_pull_and_keeps_the_dust_rule():
    """Ограничение ставится на max_liq_wei, поэтому чанк обязан пройти ветку MustNotLeaveDust:
    обе остаточные ноги остаются выше порога, иначе Pool ревертнёт наш же выстрел."""
    from analysis.aave import size_liquidation, MIN_LEFTOVER_USD, ORACLE_BASE_UNIT
    debt_wei, coll_wei = 100_000 * 10 ** 6, 140_000 * 10 ** 18
    full = size_liquidation(debt_wei, 6, _P, coll_wei, 18, _P, 11000, 1000,
                            100_000 * 10 ** 8, int(1.0 * 1e18))
    cap = full["debt_pulled"] // 8
    part = size_liquidation(debt_wei, 6, _P, coll_wei, 18, _P, 11000, 1000,
                            100_000 * 10 ** 8, int(1.0 * 1e18), max_cover_wei=cap)
    assert 0 < part["debt_pulled"] <= cap
    assert part["seized"] < full["seized"]
    floor = MIN_LEFTOVER_USD * ORACLE_BASE_UNIT
    left_debt = (debt_wei - part["debt_pulled"]) * _P // 10 ** 6
    left_coll = (coll_wei - part["seized_gross"]) * _P // 10 ** 18
    assert left_debt >= floor and left_coll >= floor


def test_max_cover_zero_is_refused():
    from analysis.aave import size_liquidation
    z = size_liquidation(100_000 * 10 ** 6, 6, _P, 140_000 * 10 ** 18, 18, _P, 11000, 1000,
                         100_000 * 10 ** 8, int(1.0 * 1e18), max_cover_wei=0)
    assert z["debt_to_cover"] == 0 and z["seized"] == 0


def test_ladder_is_lazy_when_full_size_wins():
    """Обычная позиция не должна стоить ни одной лишней котировки — иначе лестница
    оплачивается латентностью на каждом проходе."""
    calls = []

    def q(coll, debt, seized_wei, cd, dd):
        calls.append(seized_wei)
        return {"amount_out": 200_000 * 10 ** 6, "price_impact": 0.001,
                "swap_target": "0xr", "swap_calldata": "0xcd", "amount_in_used": 0}

    ev = _with_quote(q, _chunkable())
    assert ev["profitable"] and ev["f"] == 1.0
    assert len(calls) == 1


def test_ladder_finds_the_chunk_when_full_size_is_too_big_for_the_pool():
    """Ровно случай 30.07: полный размер даёт неподъёмный impact, четверть — проходит."""
    t = _chunkable()

    def q(coll, debt, seized_wei, cd, dd):
        frac = seized_wei / t["seized"]
        if frac > 0.30:                      # пул не переваривает крупный вход
            return {"amount_out": seized_wei // 10 ** 12, "price_impact": 0.45,
                    "swap_target": "0xr", "swap_calldata": "0xcd", "amount_in_used": 0}
        return {"amount_out": int(seized_wei / 10 ** 12 * 1.02), "price_impact": 0.004,
                "swap_target": "0xr", "swap_calldata": "0xcd", "amount_in_used": 0}

    ev = _with_quote(q, t)
    assert ev["profitable"], ev
    assert ev["f"] < 1.0
    assert ev["debt_to_cover"] < t["debt_to_cover"]
    assert ev["net_usd"] >= C.MIN_PROFIT_USD


def test_unprofitable_everywhere_reports_the_full_size():
    """Когда не проходит ни один размер, в лог обязан вернуться ответ по ПОЛНОМУ размеру —
    иначе оператор увидит impact крошечного чанка и не поймёт, что произошло."""
    t = _chunkable()

    def q(coll, debt, seized_wei, cd, dd):
        return {"amount_out": seized_wei // 10 ** 12 // 2, "price_impact": 0.60,
                "swap_target": "0xr", "swap_calldata": "0xcd", "amount_in_used": 0}

    ev = _with_quote(q, t)
    assert not ev["profitable"] and ev["f"] == 1.0


def test_ladder_survives_old_cached_rows_without_raw_inputs():
    """Строка из старого кэша книги не имеет сырых входов — лестница обязана деградировать
    до прежнего поведения, а не падать."""
    t = _chunkable()
    for k in ("debt_wei", "coll_wei", "total_debt_base", "hf_1e18"):
        t.pop(k)

    def q(coll, debt, seized_wei, cd, dd):
        return {"amount_out": seized_wei // 10 ** 12 // 2, "price_impact": 0.60,
                "swap_target": "0xr", "swap_calldata": "0xcd", "amount_in_used": 0}

    ev = _with_quote(q, t)
    assert ev is not None and ev["f"] == 1.0 and not ev["profitable"]


def test_no_route_at_full_size_still_tries_smaller():
    """NoRouteError на полном размере не означает отсутствие маршрута вообще."""
    t = _chunkable()

    def q(coll, debt, seized_wei, cd, dd):
        if seized_wei > t["seized"] // 4:
            raise liqd.NoRouteError("no route")
        return {"amount_out": int(seized_wei / 10 ** 12 * 1.03), "price_impact": 0.003,
                "swap_target": "0xr", "swap_calldata": "0xcd", "amount_in_used": 0}

    ev = _with_quote(q, t)
    assert ev["profitable"] and ev["f"] <= 0.25


def test_descent_is_bounded_by_economics():
    """Спуск не должен уходить в размеры, которые физически не окупают порог и газ."""
    fr = list(E._chunk_fractions(full_bonus_usd=100.0, gas_usd=0.01))
    assert fr[0] == (1, 1)
    f_lo = (C.MIN_PROFIT_USD + 0.01) / 100.0
    assert all(n / d >= f_lo * 0.999 for n, d in fr[len(E.CHUNK_FRACTIONS):] or [(1, 1)])
    assert list(E._chunk_fractions(None, 0.01)) == list(E.CHUNK_FRACTIONS)


def test_eval_budget_scales_with_the_prize():
    """Фиксированный бюджет обрывал обход на третьей ступени и терял цель с бонусом $32.9k
    (замер 30.07: одна котировка LiquidSwap 1.7–3.5с)."""
    assert E._eval_budget(None) == E.EVAL_DEADLINE_SEC
    assert E._eval_budget(0) == E.EVAL_DEADLINE_SEC
    assert E._eval_budget(50) == E.EVAL_DEADLINE_SEC            # мелочь: пол
    assert E._eval_budget(32_890) > 25                          # тот самый случай
    assert E._eval_budget(10 ** 9) == E.EVAL_DEADLINE_MAX_SEC   # потолок: каскад не встанет
