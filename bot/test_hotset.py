"""Offline unit tests for the amortized hot-set loop (no network). Run:
PYTHONPATH=. python3 bot/test_hotset.py

Covers the redesign added 2026-07-17 (mirror of the katana hot-set pattern, adapted to hyperlend's
full-book Aave model):
  * hot-set membership transitions (add on cross below HOT_HF, drop on recover / debt-repaid /
    fire-removal, keep-if-not-read),
  * rolling full-book cursor wraparound + persistence,
  * the "no gap" invariant: a hot-set member that crosses to HF<1 between sweeps IS caught (it is
    re-read every iteration), and
  * the fire path (dedup / cured-HF re-check / profitability gate) is byte-identical to the old
    once() loop — process_targets was lifted out verbatim.
"""
from __future__ import annotations

import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from bot import config as C          # noqa: E402
from bot import executor as E        # noqa: E402
from analysis.aave import HF_INFINITY  # noqa: E402
from analysis.protocols import ORACLE_BASE_UNIT  # noqa: E402

FLOOR = int(C.MIN_DEBT_USD * ORACLE_BASE_UNIT)   # non-dust debt threshold (base ccy wei)
BIG_DEBT = 1000 * ORACLE_BASE_UNIT               # $1000 -> non-dust
DUST_DEBT = 10 * ORACLE_BASE_UNIT                # $10 -> dust


def addr(i: int) -> str:
    return "0x" + f"{i:040x}"


def acct(hf: float, debt: int = BIG_DEBT) -> dict:
    return {"health_factor": HF_INFINITY if hf is None else int(hf * 1e18),
            "total_debt_base": debt}


# --------------------------------------------------------------------------- pure: next_chunk
def test_next_chunk_wraparound_and_persistence():
    bs = [addr(i) for i in range(10)]
    sl, cur, wrapped = E.next_chunk(bs, 0, 4)
    assert sl == bs[0:4] and cur == 4 and not wrapped
    sl, cur, wrapped = E.next_chunk(bs, 4, 4)
    assert sl == bs[4:8] and cur == 8 and not wrapped
    sl, cur, wrapped = E.next_chunk(bs, 8, 4)
    assert sl == bs[8:10] and cur == 0 and wrapped        # last slice wraps to 0
    # chunk >= book: one slice covers everything, wraps
    sl, cur, wrapped = E.next_chunk(bs, 0, 50)
    assert sl == bs and cur == 0 and wrapped
    # empty book: no crash
    assert E.next_chunk([], 0, 4) == ([], 0, True)
    # a stale persisted cursor past the (shrunk) book is taken modulo n, never IndexErrors
    sl, cur, wrapped = E.next_chunk(bs, 25, 4)
    assert sl == bs[5:9] and cur == 9 and not wrapped


def test_hotset_file_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        orig = C.HOTSET_FILE
        C.HOTSET_FILE = os.path.join(d, "hotset.json")
        try:
            E.save_hotset({"cursor": 4200, "hot": [addr(3), addr(1)]})
            hs = E.load_hotset()
            assert hs["cursor"] == 4200
            assert hs["hot"] == [addr(1), addr(3)]            # persisted sorted, survives restart
            # missing file -> clean default (fresh start)
            os.remove(C.HOTSET_FILE)
            assert E.load_hotset() == {"cursor": 0, "hot": []}
        finally:
            C.HOTSET_FILE = orig


# --------------------------------------------------------------------------- pure: targets
def test_targets_from_accounts():
    accounts = {
        addr(0): acct(0.95),                 # HF<1, non-dust -> target
        addr(1): acct(0.80, DUST_DEBT),      # HF<1 but dust -> NOT a target
        addr(2): acct(1.05),                 # HF>=1 -> not a target
        addr(3): acct(0.99),                 # HF<1, non-dust -> target
    }
    got = set(E.targets_from_accounts(accounts, C.MIN_DEBT_USD))
    assert got == {addr(0), addr(3)}


# --------------------------------------------------------------------------- pure: membership
def test_update_hotset_add_and_drop():
    hot = set()
    accounts = {
        addr(0): acct(1.20),                 # < HOT_HF (1.30) -> ADD
        addr(1): acct(1.60),                 # healthy -> not added
        addr(2): acct(0.90),                 # underwater but non-dust -> ADD (still hot)
        addr(3): acct(1.10, DUST_DEBT),      # near line but dust debt -> NOT added
        addr(4): acct(None),                 # no debt (infinite HF) -> NOT added
    }
    new = E.update_hotset(hot, accounts, C.HOT_HF, C.MIN_DEBT_USD)
    assert new == {addr(0), addr(2)}

    # a member read back above HOT_HF (recovered) or with debt repaid is DROPPED
    hot = {addr(0), addr(2), addr(9)}
    accounts = {addr(0): acct(1.45),         # recovered above ceiling -> drop
                addr(2): acct(None)}         # debt repaid -> drop
    new = E.update_hotset(hot, accounts, C.HOT_HF, C.MIN_DEBT_USD)
    # addr(9) was NOT read this iteration -> membership kept (still watched)
    assert new == {addr(9)}


def test_update_hotset_fire_removal():
    # a fired borrower is removed from the hot set even though its HF still qualifies — it is
    # re-seeded only when the rolling sweep next reaches it (dedup covers the interim).
    hot = {addr(0)}
    accounts = {addr(0): acct(0.90)}         # still underwater -> would normally stay/added
    new = E.update_hotset(hot, accounts, C.HOT_HF, C.MIN_DEBT_USD, drop={addr(0)})
    assert addr(0) not in new


# --------------------------------------------------------------------------- fire path unchanged
def test_process_targets_matches_once_semantics():
    """process_targets must: fire a profitable target, skip a recently-fired one, skip a target
    whose HF cured >=1 before fire, and NOT fire an unprofitable one — the exact once() behaviour."""
    T_profit = {"borrower": addr(1), "net_bonus_usd": 100.0, "hf": 0.95,
                "coll_sym": "WHYPE", "debt_sym": "USDC", "repaid_usd": 5000.0}
    T_dedup = {"borrower": addr(2), "net_bonus_usd": 90.0, "hf": 0.95,
               "coll_sym": "WHYPE", "debt_sym": "USDC", "repaid_usd": 4000.0}
    T_cured = {"borrower": addr(3), "net_bonus_usd": 80.0, "hf": 0.99,
               "coll_sym": "WHYPE", "debt_sym": "USDC", "repaid_usd": 3000.0}
    T_unprofit = {"borrower": addr(4), "net_bonus_usd": 70.0, "hf": 0.95,
                  "coll_sym": "WHYPE", "debt_sym": "USDC", "repaid_usd": 2000.0}
    now = 1_000_000.0
    st = {"sent": {addr(2): {"ts": now, "status": "ok"}},   # dedup within DEDUP_SEC
          "consec_reverts": 0, "gas_usd": 0.0}

    fired_calls = []

    def fake_fresh_hf(rpc, b):
        return 1.05 if b == addr(3) else 0.90     # T_cured cured back above 1

    def fake_evaluate(t, gas_usd):
        prof = t["borrower"] != addr(4)           # everyone profitable except T_unprofit
        return {"net_usd": 12.3, "impact": 0.002, "profitable": prof}

    def fake_fire(t, ev, st, now_ts, gas_usd):
        fired_calls.append(t["borrower"])

    o_hf, o_ev, o_fire = E.fresh_hf, E.evaluate, E.fire
    E.fresh_hf, E.evaluate, E.fire = fake_fresh_hf, fake_evaluate, fake_fire
    try:
        fired = E.process_targets(None, [T_profit, T_dedup, T_cured, T_unprofit], st, now, 0.01)
    finally:
        E.fresh_hf, E.evaluate, E.fire = o_hf, o_ev, o_fire

    assert fired_calls == [addr(1)]              # only the profitable, non-dedup, non-cured target
    assert fired == [addr(1)]


# --------------------------------------------------------------------------- iteration end-to-end
def _drive_iteration(book, hs, accounts_map, st=None):
    """Run one _hot_iteration with sweep/refine/process faked to controlled data. Returns
    (status, fired_record)."""
    fired_record = []

    def fake_sweep(rpc, to_read, retries=4):
        return {b: accounts_map[b] for b in to_read if b in accounts_map}

    def fake_refine(rpc, book, accounts, cands, reserves_cfg=None, **kw):
        rows = [{"borrower": b, "net_bonus_usd": 1.0, "hf": accounts[b]["health_factor"] / 1e18,
                 "coll_sym": "WHYPE", "debt_sym": "USDC", "repaid_usd": 5000.0} for b in cands]
        return rows, []

    def fake_process(rpc, targets, st, now_ts, gas_usd):
        keys = [t["borrower"] for t in targets]
        fired_record.extend(keys)
        return keys

    st = st or {"consec_reverts": 0, "gas_usd": 0.0, "sent": {}}
    o_sweep, o_refine, o_proc = E.sweep_accounts, E.refine, E.process_targets
    o_chunk = C.SWEEP_CHUNK
    E.sweep_accounts, E.refine, E.process_targets = fake_sweep, fake_refine, fake_process
    C.SWEEP_CHUNK = 4
    try:
        status = E._hot_iteration(None, book, st, hs, 0.0, {})
    finally:
        E.sweep_accounts, E.refine, E.process_targets = o_sweep, o_refine, o_proc
        C.SWEEP_CHUNK = o_chunk
    return status, fired_record


def test_hot_iteration_membership_cursor_and_fire():
    bs = [addr(i) for i in range(10)]
    book = {"borrowers": bs, "configs": {}}
    hs = {"cursor": 0, "hot": []}
    accounts_map = {
        bs[0]: acct(0.95),          # in cursor chunk, HF<1 -> target + fired
        bs[1]: acct(1.20),          # in cursor chunk, < HOT_HF -> becomes hot
        bs[3]: acct(1.60),          # in cursor chunk, healthy -> not hot
    }
    status, fired = _drive_iteration(book, hs, accounts_map)
    assert status["n_targets"] == 1 and status["fired"] == 1
    assert fired == [bs[0]]
    assert status["cursor"] == 4 and not status["wrapped"]   # cursor advanced by SWEEP_CHUNK=4
    assert hs["cursor"] == 4
    assert set(hs["hot"]) == {bs[1]}                          # bs0 fired->dropped, bs3 healthy
    assert abs(status["min_hf"] - 0.95) < 1e-9
    assert status["guard_ok"] is True


def test_hot_iteration_no_gap_for_hot_member_crossing():
    """The whole point: a hot-set member that crosses to HF<1 between sweeps is caught THIS
    iteration because every hot member is re-read every iteration — even though it is nowhere near
    the current rolling cursor chunk."""
    bs = [addr(i) for i in range(10)]
    book = {"borrowers": bs, "configs": {}}
    hs = {"cursor": 0, "hot": [bs[7]]}          # bs7 is hot, far from the cursor chunk [bs0..bs3]
    accounts_map = {
        bs[7]: acct(0.90),                       # crossed under 1 since the last sweep
        bs[0]: acct(1.50),                       # cursor-chunk members: healthy
    }
    status, fired = _drive_iteration(book, hs, accounts_map)
    assert fired == [bs[7]], "a hot-set member crossing to HF<1 must be fired same-iteration"
    assert status["n_targets"] == 1


def test_hot_iteration_guard_tripped_live_no_fire():
    # kill-switch tripped + live (not DRY): no target is fired, and guard_ok is reported False so
    # the loop kills AFTER logging the status line.
    bs = [addr(i) for i in range(10)]
    book = {"borrowers": bs, "configs": {}}
    hs = {"cursor": 0, "hot": []}
    accounts_map = {bs[0]: acct(0.95)}          # a real target exists
    st = {"consec_reverts": C.MAX_CONSEC_REVERTS, "gas_usd": 0.0, "sent": {}}
    o_dry = C.DRY_RUN
    C.DRY_RUN = False
    try:
        status, fired = _drive_iteration(book, hs, accounts_map, st=st)
    finally:
        C.DRY_RUN = o_dry
    assert fired == []                          # guard blocked firing
    assert status["guard_ok"] is False


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
