"""Minimal stdlib JSON-RPC client for HyperEVM public endpoints. READ-ONLY by construction:
only whitelisted eth_* read methods pass — there is no way to send a transaction through this
module. The live write-path (sign + broadcast) lives in bot/executor.py behind an explicit
DRY_RUN gate and a separate minimal client; this module never signs or sends.

HyperEVM mainnet: chainId 999 (0x3e7), native HYPE, HyperBFT, ~1s small blocks. All endpoints
below verified 2026-07-14 (eth_chainId -> 0x3e7). drpc/Cloudflare 403s any request without a
User-Agent, so one is always sent (learned the hard way — see STATE.md §RPC).

Endpoint roles (mirrors the katana bot's "keep head/logs/reaction on different endpoints"):
  * hyperliquid.drpc.org  — full archive; use for historical getLogs backfill.
  * rpc.hyperliquid.xyz/evm — official node; head + reaction.
  * rpc.hyperlend.finance — protocol's own endpoint; redundancy.
The Rpc client rotates on failure so a single-endpoint hiccup rotates rather than dropping a pass.
"""
from __future__ import annotations

import http.client
import json
import threading
import time
import urllib.error
import urllib.request

CHAIN_ID = 999

# Indirection so tests can inject a fake transport (a hanging / timing-out endpoint) without a
# network. Production code always uses the real urllib.request.urlopen.
_urlopen = urllib.request.urlopen


class HardTimeout(TimeoutError):
    """A single RPC attempt exceeded its HARD total wall deadline. The stdlib socket `timeout` is
    per-recv, not total — a trickling or half-open response can block far past it (observed live:
    a 400-call multicall to one endpoint took 33s under a 25s socket timeout, silencing the loop).
    This wraps every attempt in a wall-clock cap enforced by a worker thread, independent of any
    socket-timeout weirdness, so the loop can always rotate to another endpoint and keep ticking."""


def _run_with_deadline(fn, deadline: float):
    """Run fn() in a daemon worker; raise HardTimeout if it doesn't finish within `deadline`.
    On a real never-returning socket the worker keeps living (daemon) until its own socket timeout
    fires — bounded, and its endpoint is benched so we don't re-pick it — while the caller has
    already moved on. Returns fn()'s result, or re-raises fn()'s exception."""
    box: dict = {}
    done = threading.Event()

    def worker():
        try:
            box["r"] = fn()
        except BaseException as e:      # propagate to caller thread verbatim
            box["e"] = e
        finally:
            done.set()

    threading.Thread(target=worker, daemon=True).start()
    if not done.wait(deadline):
        raise HardTimeout(f"hard deadline {deadline}s exceeded")
    if "e" in box:
        raise box["e"]
    return box.get("r")

# Verified reachable 2026-07-14 (eth_chainId -> 0x3e7). Order = default priority.
DEFAULT_RPCS = [
    "https://hyperliquid.drpc.org",       # full archive (historical getLogs)
    "https://rpc.hyperliquid.xyz/evm",    # official node (head/reaction)
    "https://rpc.hyperlend.finance",      # protocol endpoint (redundancy)
]

_READ_METHODS = {
    "eth_chainId", "eth_blockNumber", "eth_getBlockByNumber", "eth_getLogs",
    "eth_call", "eth_getCode", "eth_getTransactionReceipt", "eth_getTransactionByHash",
    "eth_getBalance", "eth_getStorageAt", "eth_gasPrice", "eth_maxPriorityFeePerGas",
    "eth_feeHistory",
}


class RpcError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(f"rpc error {code}: {message}")
        self.code = code
        self.message = message


# JSON-RPC errors that say NOTHING about the request — the endpoint is rate-limited / at capacity /
# transiently broken (katana lesson: a rate limit {"code":-32005} during a cascade must NOT wedge
# or crash the caller — bench it and try the next endpoint). Everything else (bad params, method
# not found, a genuine execution error) is a real verdict and propagates as RpcError.
_RETRYABLE_RPC_CODES = {-32005, -32016, -32603, -32000}
_RETRYABLE_RPC_MSGS = ("rate limit", "limit exceeded", "too many request", "capacity", "timeout",
                       "try again", "429", "temporarily", "overloaded", "busy", "exceeded")


def _is_retryable_rpc_error(code, message: str) -> bool:
    if code in _RETRYABLE_RPC_CODES:
        return True
    m = (message or "").lower()
    return any(s in m for s in _RETRYABLE_RPC_MSGS)


class Rpc:
    def __init__(self, urls: list[str] | None = None, timeout: float = 25.0, retries: int = 6,
                 min_interval: float = 0.03, backoff_429: float = 0.8,
                 hard_timeout: float | None = None, bench_sec: float = 30.0):
        self.urls = list(urls or DEFAULT_RPCS)
        self.timeout = timeout             # per-attempt socket timeout (per-recv, NOT total)
        self.retries = retries
        self.min_interval = min_interval   # gentle pacing — public endpoints rate-limit bursts
        self.backoff_429 = backoff_429
        # hard_timeout: total wall cap per attempt, enforced by a worker thread independent of the
        # socket timeout (which is per-recv and a trickling endpoint can outlast). None -> off
        # (legacy behaviour for the CLI/backfill). The live loop sets it so no single endpoint can
        # ever wedge the single-threaded loop for more than ~hard_timeout.
        self.hard_timeout = hard_timeout
        self.bench_sec = bench_sec         # how long a hung/failed endpoint is benched
        self._id = 0
        self._last_call = 0.0
        self._idx = 0                      # rotating endpoint cursor (advances every attempt)
        self._benched: dict[str, float] = {}   # url -> unix ts until which it is benched

    def _pick_url(self, now: float) -> str:
        """Next endpoint that is not currently benched (a hung/failed one is skipped). If every
        endpoint is benched, pick the one whose bench expires soonest so we still make progress."""
        n = len(self.urls)
        for _ in range(n):
            u = self.urls[self._idx % n]
            self._idx += 1
            if self._benched.get(u, 0.0) <= now:
                return u
        return min(self.urls, key=lambda u: self._benched.get(u, 0.0))

    def _bench(self, url: str, now: float) -> None:
        self._benched[url] = now + self.bench_sec

    def _request(self, url: str, body: bytes):
        """One HTTP attempt to `url`, optionally under the hard wall deadline. Returns the parsed
        JSON-RPC result or raises (RpcError for a genuine node error; transport errors otherwise)."""
        def do():
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
            with _urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        d = _run_with_deadline(do, self.hard_timeout) if self.hard_timeout else do()
        if "error" in d:
            err = d["error"]
            raise RpcError(err.get("code"), err.get("message", ""))
        return d["result"]

    def call(self, method: str, params: list):
        if method not in _READ_METHODS:
            raise ValueError(f"method {method} is not in the read-only whitelist")
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id,
                           "method": method, "params": params}).encode()
        last = None
        for attempt in range(self.retries):
            now = time.time()
            wait = self.min_interval - (now - self._last_call)
            if wait > 0:
                time.sleep(wait)
            url = self._pick_url(time.time())
            self._last_call = time.time()
            try:
                return self._request(url, body)
            except RpcError as e:
                # rate-limit / capacity / transient node error -> bench + rotate (never wedge or
                # crash on a throttled endpoint mid-cascade); a genuine verdict propagates.
                if not _is_retryable_rpc_error(e.code, e.message):
                    raise
                last = e
                self._bench(url, time.time())
                time.sleep(self.backoff_429 * (attempt + 1))
            except HardTimeout as e:
                # the endpoint hung past the hard deadline: bench it and rotate immediately (no
                # extra sleep — we already spent hard_timeout on it). THIS is the anti-wedge path.
                last = e
                self._bench(url, time.time())
            except urllib.error.HTTPError as e:
                last = e
                if e.code == 429:
                    self._bench(url, time.time())   # rate-limited: bench + back off
                time.sleep(self.backoff_429 * (attempt + 1) if e.code == 429
                           else 0.4 * (attempt + 1))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError,
                    http.client.IncompleteRead, http.client.HTTPException) as e:
                # timeout / refused / reset / garbage: bench this endpoint and try the next one
                last = e
                self._bench(url, time.time())
                time.sleep(0.4 * (attempt + 1))
        raise RuntimeError(f"rpc exhausted retries: {last}")

    # -- convenience wrappers --------------------------------------------
    def chain_id(self) -> int:
        return int(self.call("eth_chainId", []), 16)

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def get_block(self, number: int | str, full: bool = False) -> dict:
        tag = number if isinstance(number, str) else hex(number)
        return self.call("eth_getBlockByNumber", [tag, full])

    def base_fee(self) -> int:
        """Latest block base fee (wei). HyperEVM is EIP-1559; priority fee is non-operative."""
        blk = self.get_block("latest", False)
        return int(blk.get("baseFeePerGas", "0x0"), 16)

    def get_logs(self, address, topics, from_block: int, to_block: int) -> list:
        return self.call("eth_getLogs", [{
            "address": address, "topics": topics,
            "fromBlock": hex(from_block), "toBlock": hex(to_block)}])

    def get_code(self, address: str, tag: str = "latest") -> str:
        return self.call("eth_getCode", [address, tag])

    def eth_call(self, to: str, data: str, tag: str = "latest", gas: int | None = None) -> str:
        req = {"to": to, "data": data}
        if gas is not None:
            req["gas"] = hex(gas)
        return self.call("eth_call", [req, tag])

    def gas_price(self) -> int:
        try:
            return int(self.call("eth_gasPrice", []), 16)
        except Exception:
            return int(0.1 * 1e9)


def get_logs_chunked(rpc: Rpc, address, topics, from_block: int, to_block: int,
                     chunk: int = 4_000, on_progress=None, max_consec_failures: int = 6) -> list:
    """getLogs over a big range in fixed windows; halves the window on limit/truncation errors
    (floor 500). HyperEVM public endpoints cap getLogs block spans (drpc silently returns [] past
    ~5k blocks without a UA-scoped error, so keep the default chunk conservative).

    BOUNDED (fix 2026-07-18): once the halving hit the 500 floor, a persistent error just
    `continue`d forever — the only raise was reachable at hi==lo, so any window >=2 blocks with
    every endpoint failing spun the caller infinitely. This became a LIVE path when discovery
    moved onto every hot-loop cursor wrap (~every few minutes). Now at most `max_consec_failures`
    CONSECUTIVE failed windows are tolerated (each failure already burned the Rpc client's own
    bounded retries, so total wall is ~max_consec_failures * retries * hard_timeout worst case);
    then an RpcError propagates so the caller can skip the pass WITHOUT advancing its checkpoint
    and retry the same range later. The counter resets on every successful window, so a long
    (backfill) run with sporadic transient errors still completes."""
    out = []
    lo = from_block
    failures = 0
    while lo <= to_block:
        hi = min(lo + chunk - 1, to_block)
        try:
            logs = rpc.get_logs(address, topics, lo, hi)
        except (RpcError, http.client.IncompleteRead, RuntimeError) as e:
            failures += 1
            if failures >= max_consec_failures or hi == lo:
                if isinstance(e, RpcError):
                    raise
                raise RpcError("get_logs_chunked",
                               f"gave up after {failures} consecutive failed window(s), "
                               f"last [{lo},{hi}] chunk={chunk}: {e}") from e
            chunk = max(500, chunk // 2)
            continue
        failures = 0
        out.extend(logs)
        if on_progress:
            on_progress(hi, to_block, len(out))
        lo = hi + 1
    return out
