"""Тесты яруса 1 гонки (04.08): keep-alive, tip-политика, параллельный залп, nonce prewarm,
shadow-телеметрия. Каждый блок тестирует ровно тот инвариант, из-за которого правка появилась,
плюс откат-путь (флаг = прежнее поведение бит-в-бит)."""
from __future__ import annotations

import http.client
import json
import os
import threading
import time

import pytest

import analysis.rpc as rpc_mod
from bot import config as C
from bot import executor as ex
from bot import shadow


# --------------------------------------------------------------------------- keep-alive pool
class _FakeSock:
    def settimeout(self, t):
        pass


class _FakeConn:
    """Считает создания/запросы; умеет умереть RemoteDisconnected на N-м request."""
    created = 0

    def __init__(self, die_on: set[int] | None = None, payload: dict | None = None):
        _FakeConn.created += 1
        self.requests = 0
        self.die_on = die_on or set()
        self.payload = payload or {"jsonrpc": "2.0", "id": 1, "result": "0x1"}
        self.sock = _FakeSock()
        self.closed = False

    def request(self, method, path, body=None, headers=None):
        self.requests += 1
        if self.requests in self.die_on:
            raise http.client.RemoteDisconnected("idle keep-alive closed by server")

    def getresponse(self):
        class R:
            status = 200
            headers = {}
        r = R()
        r.read = lambda: json.dumps(self.payload).encode()
        return r

    def close(self):
        self.closed = True


@pytest.fixture
def pool_env(monkeypatch):
    """Чистый пул + фабрика фейк-соединений; keep-alive включён, _urlopen НЕ подменён."""
    monkeypatch.setattr(rpc_mod, "_KEEPALIVE", True)
    monkeypatch.setattr(rpc_mod, "_pool", {})
    monkeypatch.setattr(rpc_mod, "_urlopen", __import__("urllib.request", fromlist=["r"]).urlopen)
    _FakeConn.created = 0
    return monkeypatch


def test_keepalive_reuses_connection(pool_env):
    conns = []
    pool_env.setattr(rpc_mod, "_new_conn", lambda url, t: conns.append(_FakeConn()) or conns[-1])
    for _ in range(3):
        rpc_mod.http_post_json("https://x.example/rpc", b"{}", 5)
    assert _FakeConn.created == 1, "3 вызова должны прожить на ОДНОМ соединении"
    assert conns[0].requests == 3


def test_keepalive_retries_once_on_stale_conn(pool_env):
    """Лежалое соединение молча закрыто сервером: один немедленный повтор на свежем."""
    conns = []

    def factory(url, t):
        c = _FakeConn(die_on={2})           # второй request на этом conn умирает
        conns.append(c)
        return c

    pool_env.setattr(rpc_mod, "_new_conn", factory)
    assert rpc_mod.http_post_json("https://x.example/rpc", b"{}", 5)["result"] == "0x1"
    assert rpc_mod.http_post_json("https://x.example/rpc", b"{}", 5)["result"] == "0x1"
    assert _FakeConn.created == 2, "после смерти лежалого должен строиться ровно один новый"
    assert conns[0].closed


def test_keepalive_fresh_conn_error_propagates(pool_env):
    pool_env.setattr(rpc_mod, "_new_conn",
                     lambda url, t: _FakeConn(die_on={1}))
    with pytest.raises(http.client.RemoteDisconnected):
        rpc_mod.http_post_json("https://x.example/rpc", b"{}", 5)


def test_keepalive_off_uses_urlopen(monkeypatch):
    called = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self):
            return b'{"result": "ok"}'

    def fake_urlopen(req, timeout=None):
        called["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(rpc_mod, "_KEEPALIVE", False)
    monkeypatch.setattr(rpc_mod, "_urlopen", fake_urlopen)
    assert rpc_mod.http_post_json("https://y.example/", b"{}", 5)["result"] == "ok"
    assert called["url"] == "https://y.example/"


def test_patched_urlopen_bypasses_pool(monkeypatch):
    """Старые тесты подменяют _urlopen — пул обязан уступить, а не лезть в сеть."""
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self):
            return b'{"result": "faked"}'

    monkeypatch.setattr(rpc_mod, "_KEEPALIVE", True)
    monkeypatch.setattr(rpc_mod, "_urlopen", lambda req, timeout=None: _Resp())
    assert rpc_mod.http_post_json("https://z.example/", b"{}", 5)["result"] == "faked"


# --------------------------------------------------------------------------- tip policy
def test_tip_off_mode_is_priority_gwei(monkeypatch):
    monkeypatch.setattr(C, "TIP_MODE", "off")
    monkeypatch.setattr(C, "PRIORITY_GWEI", 0.0)
    assert ex._tip_wei(10_000.0) == 0, "откат HL_TIP_MODE=off обязан вернуть старое поведение"


def test_tip_scales_with_prize(monkeypatch):
    monkeypatch.setattr(C, "TIP_MODE", "auto")
    monkeypatch.setattr(C, "TIP_MIN_GWEI", 5.0)
    monkeypatch.setattr(C, "TIP_MAX_GWEI", 1000.0)
    monkeypatch.setattr(C, "TIP_PRIZE_FRAC", 0.05)
    monkeypatch.setattr(C, "HYPE_USD", 50.0)
    monkeypatch.setattr(C, "GAS_UNITS_EST", 1_000_000)
    ex._basefee_cache["wei"] = None          # без потолка по балансу
    # приз $500: бюджет $25 = 25/(50*1e6*1e-9) = 500 gwei
    assert ex._tip_wei(500.0) == int(500 * 1e9)
    # мелочь клампится в пол, кит — в потолок
    assert ex._tip_wei(1.0) == int(5 * 1e9)
    assert ex._tip_wei(1_000_000.0) == int(1000 * 1e9)


def test_tip_capped_by_balance_envelope(monkeypatch):
    """Тощий кошелёк: чаевые не смеют превратить проходной выстрел в insufficient funds."""
    monkeypatch.setattr(C, "TIP_MODE", "auto")
    monkeypatch.setattr(C, "TIP_MIN_GWEI", 5.0)
    monkeypatch.setattr(C, "TIP_MAX_GWEI", 1000.0)
    monkeypatch.setattr(C, "TIP_PRIZE_FRAC", 0.05)
    monkeypatch.setattr(C, "HYPE_USD", 50.0)
    monkeypatch.setattr(C, "GAS_UNITS_EST", 1_000_000)
    monkeypatch.setattr(C, "GAS_LIMIT", 2_500_000)
    ex._note_basefee(int(0.1e9))
    # 0.04 HYPE: конверт 0.04e18/2.5e6 = 16 gwei на газ; минус 2*base=0.2 -> ~15.8 gwei потолок
    tip = ex._tip_wei(10_000.0, {"balance_hype": 0.04})
    assert tip < int(16 * 1e9), f"tip {tip} обязан ужаться в конверт баланса"
    assert tip > 0
    # совсем нищий кошелёк -> 0, но не отрицательный
    assert ex._tip_wei(10_000.0, {"balance_hype": 0.0}) == 0


def test_fee_params_uses_fresh_cache_and_tip(monkeypatch):
    monkeypatch.setattr(C, "BASEFEE_CACHE_SEC", 2.5)
    calls = []
    monkeypatch.setattr(ex, "_rpc_write", lambda *a: calls.append(a) or (_ for _ in ()).throw(
        AssertionError("кэш свежий — RPC запрещён")))
    ex._note_basefee(int(1e9))
    max_fee, prio = ex._fee_params(tip_wei=int(7e9))
    assert prio == int(7e9)
    assert max_fee == 2 * int(1e9) + int(7e9)
    assert not calls


def test_fee_params_expired_cache_hits_rpc(monkeypatch):
    monkeypatch.setattr(C, "BASEFEE_CACHE_SEC", 2.5)
    ex._basefee_cache["wei"], ex._basefee_cache["ts"] = int(1e9), time.monotonic() - 10
    monkeypatch.setattr(ex, "_rpc_write",
                        lambda m, p, budget=None: {"baseFeePerGas": hex(int(3e9))})
    max_fee, prio = ex._fee_params(tip_wei=0)
    assert max_fee == 2 * int(3e9)


# --------------------------------------------------------------------------- parallel broadcast
def _bc(monkeypatch, urls, behav):
    """behav[url] -> 'ok:hash' | RpcVerdict | Exception"""
    monkeypatch.setattr(C, "PARALLEL_BROADCAST", True)
    monkeypatch.setattr(C, "BROADCAST_RPCS", urls)
    monkeypatch.setattr(C, "RPC_HARD_TIMEOUT", 2.0)

    def fake(url, method, params, budget=None):
        b = behav[url]
        if isinstance(b, Exception):
            raise b
        return b

    monkeypatch.setattr(ex, "_rpc_write_url", fake)
    return ex._broadcast_raw("0xdead")


def test_broadcast_first_ok_wins(monkeypatch):
    h = _bc(monkeypatch, ["u1", "u2"], {"u1": "0xhash", "u2": TimeoutError("slow")})
    assert h == "0xhash"


def test_broadcast_already_known_is_delivery(monkeypatch):
    h = _bc(monkeypatch, ["u1", "u2"],
            {"u1": ex.RpcVerdict("rpc: already known"), "u2": ex.RpcVerdict("rpc: already known")})
    assert h == ""            # доставлено, хэш возьмётся локальный


def test_broadcast_unanimous_verdict_raises_verdict(monkeypatch):
    with pytest.raises(ex.RpcVerdict):
        _bc(monkeypatch, ["u1", "u2"],
            {"u1": ex.RpcVerdict("rpc: nonce too low"), "u2": ex.RpcVerdict("rpc: nonce too low")})


def test_broadcast_verdict_plus_transport_is_ambiguous(monkeypatch):
    """Обрыв на одном узле не даёт объявить not-delivered: tx может жить в его мемпуле."""
    with pytest.raises(TimeoutError):
        _bc(monkeypatch, ["u1", "u2"],
            {"u1": ex.RpcVerdict("rpc: nonce too low"), "u2": TimeoutError("wall")})


def test_broadcast_off_single_url(monkeypatch):
    """Откат = прежний одиночный путь через исторический сим _rpc_write (его фейкают
    старые тесты write-семантики — совместимость обязана сохраниться)."""
    monkeypatch.setattr(C, "PARALLEL_BROADCAST", False)
    monkeypatch.setattr(C, "RPC_WRITE", "only")
    seen = []
    monkeypatch.setattr(ex, "_rpc_write",
                        lambda m, p, budget=None: seen.append(m) or "0xh")
    monkeypatch.setattr(ex, "_rpc_write_url",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("одиночный путь обязан идти через _rpc_write")))
    assert ex._broadcast_raw("0xdead") == "0xh"
    assert seen == ["eth_sendRawTransaction"]


# --------------------------------------------------------------------------- nonce prewarm
@pytest.fixture
def nonce_env(monkeypatch):
    monkeypatch.setattr(ex, "_nonce_cache",
                        {"addr": None, "next": None, "send_ts": 0.0, "chain": None, "chain_ts": 0.0})
    monkeypatch.setattr(C, "NONCE_PREWARM_SEC", 15.0)
    return monkeypatch


def test_prewarm_feeds_pending_without_rpc(nonce_env):
    ex._nonce_cache.update({"addr": "0xA", "chain": 7, "chain_ts": time.monotonic()})
    nonce_env.setattr(ex, "_rpc_write", lambda *a: (_ for _ in ()).throw(
        AssertionError("свежий prewarm-кэш — RPC на пути выстрела запрещён")))
    assert ex._pending_nonce("0xA") == 7


def test_prewarm_stale_cache_falls_back_to_rpc(nonce_env):
    ex._nonce_cache.update({"addr": "0xA", "chain": 7, "chain_ts": time.monotonic() - 100})
    nonce_env.setattr(ex, "_rpc_write", lambda m, p, budget=None: hex(9))
    assert ex._pending_nonce("0xA") == 9


def test_prewarm_never_touches_send_ts(nonce_env):
    """WC-урок: send_ts пишет ТОЛЬКО _nonce_after_send — прогрев не благословляет бампы."""
    ex._nonce_cache["send_ts"] = 123.456
    nonce_env.setattr(ex, "_rpc_write", lambda m, p, budget=None: hex(5))
    ex.prewarm_nonce("0xA")
    assert ex._nonce_cache["send_ts"] == 123.456
    assert ex._nonce_cache["chain"] == 5


def test_prewarm_disabled_always_rpc(nonce_env):
    nonce_env.setattr(C, "NONCE_PREWARM_SEC", 0.0)
    ex._nonce_cache.update({"addr": "0xA", "chain": 7, "chain_ts": time.monotonic()})
    nonce_env.setattr(ex, "_rpc_write", lambda m, p, budget=None: hex(11))
    assert ex._pending_nonce("0xA") == 11, "откат =0 обязан вернуть старый путь (живой RPC)"


def test_local_bump_still_wins_inside_send_window(nonce_env):
    """Семантика local-bump-vs-chain не тронута prewarm'ом."""
    now = time.monotonic()
    ex._nonce_cache.update({"addr": "0xA", "next": 12, "send_ts": now,
                            "chain": 10, "chain_ts": now})
    assert ex._pending_nonce("0xA") == 12


# --------------------------------------------------------------------------- shadow telemetry
@pytest.fixture
def shadow_env(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "SHADOW", True)
    monkeypatch.setattr(C, "SHADOW_EVERY_SEC", 0.0)
    monkeypatch.setattr(C, "SHADOW_FILE", str(tmp_path / "races.jsonl"))
    monkeypatch.setattr(C, "SHADOW_CKPT", str(tmp_path / "ckpt.json"))
    monkeypatch.setattr(C, "CONTRACT", "0xCBAB63AA7F8fA7F15445e85e64b2ADe4fEeC2bd6")
    monkeypatch.setattr(shadow, "_state", {"last_tick": 0.0})
    return monkeypatch


def _mk_log(caller: str, block=100, cover=5_000_000_000):
    coll = "0x9FDBdA0A5e284c32744D2f17Ee5c74B284993463"    # UBTC
    debt = "0xb88339CB7199b77E23DB6E890353E22632Ba630f"    # USDC
    victim = "0x" + "77" * 20
    data = (hex(cover)[2:].rjust(64, "0") + "0" * 64
            + caller[2:].lower().rjust(64, "0") + "0" * 64)
    return {"blockNumber": hex(block), "transactionHash": "0x" + "aa" * 32,
            "topics": ["0xliq", "0x" + coll[2:].rjust(64, "0"),
                       "0x" + debt[2:].rjust(64, "0"), "0x" + victim[2:].rjust(64, "0")],
            "data": "0x" + data}


class _ShadowRpc:
    def __init__(self, logs):
        self.logs = logs

    def block_number(self):
        return 100

    def get_logs(self, address, topics, frm, to):
        return self.logs


def test_shadow_excludes_our_own_liquidations(shadow_env):
    """Урок 03.08 (midnight): монитор прислал НАШУ сделку как конкурента. caller = КОНТРАКТ."""
    spawned = []
    shadow_env.setattr(shadow.threading, "Thread",
                       lambda target, args, daemon: spawned.append(args) or
                       type("T", (), {"start": lambda self: None})())
    ours = _mk_log(C.CONTRACT)
    shadow.tick(_ShadowRpc([ours]), {"borrowers": []}, {"hot": []}, {})
    assert not spawned, "своя ликвидация не должна порождать shadow-запись"


def test_shadow_foreign_event_spawns_enrich_and_moves_ckpt(shadow_env):
    spawned = []
    shadow_env.setattr(shadow.threading, "Thread",
                       lambda target, args, daemon: spawned.append(args) or
                       type("T", (), {"start": lambda self: None})())
    foreign = _mk_log("0x" + "42" * 20)
    shadow.tick(_ShadowRpc([foreign]), {"borrowers": ["0x" + "77" * 20]},
                {"hot": []}, {"balance_hype": 5.0})
    assert len(spawned) == 1
    events, our_view, snap = spawned[0][1], spawned[0][2], spawned[0][3]
    assert events[0]["liquidator"] == "0x" + "42" * 20
    assert our_view["in_book"]["0x" + "77" * 20] is True
    assert json.load(open(C.SHADOW_CKPT))["last_block"] == 100


def test_shadow_decode_fields():
    e = shadow._decode(_mk_log("0x" + "42" * 20, block=77, cover=123456))
    assert e["block"] == 77 and e["cover"] == 123456
    assert e["victim"] == "0x" + "77" * 20


def test_shadow_disabled_is_noop(shadow_env):
    shadow_env.setattr(C, "SHADOW", False)
    shadow.tick(None, {}, {}, {})            # None-rpc: упал бы, если бы не откат-гард


# --------------------------------------------------------------------------- oracle-update trigger
class _UpdRpc:
    def __init__(self, head, logs):
        self.head, self.logs, self.calls = head, logs, []

    def block_number(self):
        self.calls.append("head")
        return self.head

    def get_logs(self, address, topics, frm, to):
        self.calls.append(("logs", frm, to))
        return [l for l in self.logs if frm <= l["_blk"] <= to]


def _upd_log(blk):
    return {"_blk": blk, "blockNumber": hex(blk)}


@pytest.fixture
def upd_env(monkeypatch):
    monkeypatch.setattr(C, "UPDATE_TRIGGER", True)
    monkeypatch.setattr(ex, "_upd_watch", {"from": None})
    return monkeypatch


def test_upd_first_call_inits_cursor_returns_zero(upd_env):
    r = _UpdRpc(100, [_upd_log(99)])
    assert ex.poll_oracle_updates(r) == 0
    assert ex._upd_watch["from"] == 101, "старые события до старта не считаются"


def test_upd_new_events_advance_cursor(upd_env):
    r = _UpdRpc(100, [])
    ex.poll_oracle_updates(r)                       # init: from=101
    r.head, r.logs = 110, [_upd_log(105), _upd_log(108)]
    assert ex.poll_oracle_updates(r) == 2
    assert ex._upd_watch["from"] == 109
    r.head = 115
    assert ex.poll_oracle_updates(r) == 0, "те же события не считаются дважды"


def test_upd_read_error_returns_zero(upd_env):
    class Boom:
        def block_number(self):
            raise TimeoutError("storm")
    ex._upd_watch["from"] = 50
    class Boom2:
        def block_number(self):
            return 60
        def get_logs(self, *a):
            raise TimeoutError("storm")
    assert ex.poll_oracle_updates(Boom()) == 0
    assert ex.poll_oracle_updates(Boom2()) == 0, "отказ триггера не смеет ронять итерацию"


def test_upd_disabled_is_noop(upd_env):
    upd_env.setattr(C, "UPDATE_TRIGGER", False)
    assert ex.poll_oracle_updates(None) == 0        # None-rpc: упал бы без откат-гарда


# --------------------------------------------------------------------------- pre-arm
@pytest.fixture
def prearm_env(monkeypatch):
    monkeypatch.setattr(C, "PREARM", True)
    monkeypatch.setattr(C, "PREARM_HF", 1.02)
    monkeypatch.setattr(C, "PREARM_TTL", 45.0)
    monkeypatch.setattr(C, "PREARM_REFRESH_SEC", 20.0)
    monkeypatch.setattr(C, "PREARM_MAX", 2)
    monkeypatch.setattr(ex, "_prearm", {})
    ex._prearm_busy.clear()
    return monkeypatch


def test_prearm_get_respects_ttl_and_cf_regime(prearm_env):
    ex._prearm["0xb"] = {"t": {"x": 1}, "ev": {"y": 2}, "ts": time.monotonic()}
    assert ex._prearm_get("0xb", 0.99) == ({"x": 1}, {"y": 2})
    assert ex._prearm_get("0xb", 0.90) is None, "HF<0.95 = другой close-factor, кэш не годится"
    ex._prearm["0xold"] = {"t": {}, "ev": {}, "ts": time.monotonic() - 100}
    assert ex._prearm_get("0xold", 0.99) is None, "протухший арм обязан отвергнуться"
    assert "0xold" not in ex._prearm, "и удалиться"


def test_prearm_tick_picks_edge_only(prearm_env):
    from analysis.aave import HF_INFINITY as INF
    from analysis.protocols import ORACLE_BASE_UNIT as OBU
    prearm_env.setattr(C, "CONTRACT", "0xc")
    prearm_env.setattr(C, "MIN_DEBT_USD", 500.0)
    accounts = {
        "0xedge":  {"health_factor": int(1.01e18), "total_debt_base": int(9_000 * OBU)},
        "0xlive":  {"health_factor": int(0.99e18), "total_debt_base": int(9_000 * OBU)},  # HF<1: боевой путь
        "0xfar":   {"health_factor": int(1.20e18), "total_debt_base": int(9_000 * OBU)},
        "0xdust":  {"health_factor": int(1.01e18), "total_debt_base": int(10 * OBU)},
        "0xinf":   {"health_factor": INF,          "total_debt_base": int(9_000 * OBU)},
    }
    spawned = []
    prearm_env.setattr(ex.threading, "Thread",
                       lambda target, args, daemon: spawned.append(args) or
                       type("T", (), {"start": lambda self: None})())
    ex.prearm_tick(None, {}, accounts, {}, 1.0)
    assert len(spawned) == 1
    assert spawned[0][2] == ["0xedge"], f"в кромку попал лишний: {spawned[0][2]}"


def test_prearm_tick_single_flight(prearm_env):
    prearm_env.setattr(C, "CONTRACT", "0xc")
    ex._prearm_busy.set()                            # фоновый поток уже котирует
    prearm_env.setattr(ex.threading, "Thread",
                       lambda *a, **k: (_ for _ in ()).throw(AssertionError("второй поток")))
    ex.prearm_tick(None, {}, {}, {}, 1.0)            # не должен спавнить


def test_process_targets_uses_armed_quote(prearm_env):
    """Арм-хит: evaluate (1.7-3.5с квота) НЕ вызывается, выстрел идёт с кэшем."""
    prearm_env.setattr(C, "DRY_RUN", True)           # fire печатает и выходит — сеть не нужна
    prearm_env.setattr(ex, "fresh_hf", lambda rpc, b: 0.98)
    prearm_env.setattr(ex, "evaluate",
                       lambda t, g: (_ for _ in ()).throw(AssertionError("квота на арм-хите")))
    t_cached = {"borrower": "0xb", "hf": 0.99, "close_factor": 0.5, "coll_sym": "UBTC",
                "debt_sym": "USDC", "debt_to_cover": 1, "repaid_usd": 100.0}
    ev_cached = {"net_usd": 42.0, "impact": 0.001, "profitable": True,
                 "debt_to_cover": 1, "min_profit_wei": 1}
    ex._prearm["0xb"] = {"t": t_cached, "ev": ev_cached, "ts": time.monotonic()}
    st = {"sent": {}, "fires": 0, "gas_usd": 0.0, "day": "2026-08-04"}
    fired = ex.process_targets(None, [dict(t_cached, net_bonus_usd=50.0)], st, 1000.0, 1.0)
    assert fired == ["0xb"]
