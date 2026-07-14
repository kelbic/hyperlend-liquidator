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
import time
import urllib.error
import urllib.request

CHAIN_ID = 999

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


class Rpc:
    def __init__(self, urls: list[str] | None = None, timeout: float = 25.0, retries: int = 6,
                 min_interval: float = 0.03, backoff_429: float = 0.8):
        self.urls = list(urls or DEFAULT_RPCS)
        self.timeout = timeout
        self.retries = retries
        self.min_interval = min_interval   # gentle pacing — public endpoints rate-limit bursts
        self.backoff_429 = backoff_429
        self._id = 0
        self._last_call = 0.0

    def call(self, method: str, params: list):
        if method not in _READ_METHODS:
            raise ValueError(f"method {method} is not in the read-only whitelist")
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id,
                           "method": method, "params": params}).encode()
        last = None
        for attempt in range(self.retries):
            wait = self.min_interval - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.time()
            url = self.urls[attempt % len(self.urls)]
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    d = json.loads(r.read())
                if "error" in d:
                    err = d["error"]
                    raise RpcError(err.get("code"), err.get("message", ""))
                return d["result"]
            except RpcError:
                raise
            except urllib.error.HTTPError as e:
                last = e
                time.sleep(self.backoff_429 * (attempt + 1) if e.code == 429
                           else 0.4 * (attempt + 1))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError,
                    http.client.IncompleteRead, http.client.HTTPException) as e:
                last = e
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
                     chunk: int = 4_000, on_progress=None) -> list:
    """getLogs over a big range in fixed windows; halves the window on limit/truncation errors.
    HyperEVM public endpoints cap getLogs block spans (drpc silently returns [] past ~5k blocks
    without a UA-scoped error, so keep the default chunk conservative)."""
    out = []
    lo = from_block
    while lo <= to_block:
        hi = min(lo + chunk - 1, to_block)
        try:
            logs = rpc.get_logs(address, topics, lo, hi)
        except (RpcError, http.client.IncompleteRead, RuntimeError):
            if hi > lo:
                chunk = max(500, chunk // 2)
                continue
            raise
        out.extend(logs)
        if on_progress:
            on_progress(hi, to_block, len(out))
        lo = hi + 1
    return out
