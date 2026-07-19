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

    o_open, o_env, o_chat = urllib.request.urlopen, C.TG_ENV_FILE, C.TG_CHAT_ID
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
        f.write("TELEGRAM_BOT_TOKEN=tok\n")
        env = f.name
    urllib.request.urlopen, C.TG_ENV_FILE, C.TG_CHAT_ID = fake_urlopen, env, "-100123"
    try:
        fn()
        deadline = time.time() + 3.0          # alert_async posts from a daemon thread
        while time.time() < deadline and len(sent) < wait_n:
            time.sleep(0.01)
    finally:
        urllib.request.urlopen, C.TG_ENV_FILE, C.TG_CHAT_ID = o_open, o_env, o_chat
        os.unlink(env)
    return sent


def test_alert_text_carries_bot_tag():
    """The fleet posts into ONE Telegram chat, so every outgoing alert must name its bot."""
    sent = _capture_alerts(lambda: E.alert("\U0001f52b LIQUIDATE 0xdead, sending\u2026"), wait_n=1)
    assert len(sent) == 1, sent
    assert sent[0].startswith(f"[{C.BOT_TAG}] "), sent[0]
    assert "LIQUIDATE 0xdead" in sent[0]


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
    E._nonce_cache.update({"addr": None, "next": None})
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


def test_nonce_does_not_move_when_the_broadcast_fails():
    def boom(raw):
        raise RuntimeError("connection reset by peer")

    fw = _FakeWrite(pending_nonce=7, send=boom)
    o = _armed_raw(fw)
    try:
        E._sign_and_send("0xaa")
    except RuntimeError:
        pass
    else:
        raise AssertionError("a failed broadcast must propagate")
    finally:
        cache = dict(E._nonce_cache)
        _restore_raw(o)
    assert cache["next"] is None, f"nonce advanced on a FAILED send: {cache}"


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
    """FLEET DISCIPLINE: a transport/signing failure before the broadcast means nothing executed
    on-chain. Feeding it to consec_reverts would let an RPC brownout trip the kill-switch and take
    the bot down for the entire crash window — the only window it earns in."""
    fw = _FakeWrite(send=lambda raw: (_ for _ in ()).throw(RuntimeError("all write RPCs down")))
    st = _fire_raw_once(fw)
    rec = st["sent"][_T["borrower"]]
    assert rec["status"] == "send_error", rec
    assert st["consec_reverts"] == 0, "send error must NOT feed the kill-switch"
    assert st["reverts"] == 0
    assert st["fires"] == 0, "nothing was broadcast — not a fire"
    assert st["gas_usd"] == 0.0, "no gas is burned by a tx that never reached the chain"


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
    o_dry = C.DRY_RUN
    C.DRY_RUN = False
    try:
        E._check_pending(rpc, st, now_ts)
    finally:
        C.DRY_RUN = o_dry
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


# ------------------------------------------------------------------- cast fallback: key hygiene
def test_cast_fallback_passes_the_key_via_env_not_argv():
    """`--private-key` in argv is readable in `ps` by every local user for the life of the
    subprocess. The key now travels in the environment instead."""
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
    assert "--private-key" not in seen["args"], seen["args"]
    key = "0x" + "2" * 64
    assert key not in seen["args"], "the key must not appear anywhere in argv"
    assert seen["env"] and seen["env"].get("ETH_PRIVATE_KEY") == key
    assert st["sent"][_T["borrower"]]["status"] == "ok"


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
