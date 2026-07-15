"""Minimal Multicall3.aggregate3 ABI encoder/decoder (stdlib, pure — offline-testable).

Multicall3 is deployed at the canonical CREATE2 address on HyperEVM (bytecode present, 3808
bytes, checked 2026-07-14). aggregate3 batches read-only eth_calls: N calls -> 1 RPC
round-trip, which is what makes a per-pass sweep of getUserAccountData across the whole
borrower book cheap. Encoder/decoder are pure; the only network touch is via the caller's Rpc.
"""
from __future__ import annotations

from analysis.keccak import selector

MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"  # canonical (same address every chain)
SEL_AGGREGATE3 = selector("aggregate3((address,bool,bytes)[])")  # computed -> 0x82ad56cb


def _pad32(b: bytes) -> bytes:
    return b + b"\x00" * ((32 - len(b) % 32) % 32)


def encode_aggregate3(calls: list[tuple[str, str]]) -> str:
    """calls = [(target_address, calldata_hex)]; allowFailure=true for every call.
    Returns full calldata hex for Multicall3.aggregate3."""
    n = len(calls)
    bodies = []
    for target, data_hex in calls:
        data = bytes.fromhex(data_hex[2:] if data_hex.startswith("0x") else data_hex)
        body = (int(target, 16).to_bytes(32, "big")
                + (1).to_bytes(32, "big")               # allowFailure = true
                + (0x60).to_bytes(32, "big")            # offset of bytes within the tuple
                + len(data).to_bytes(32, "big")
                + _pad32(data))
        bodies.append(body)
    offsets, cursor = [], 32 * n
    for b in bodies:
        offsets.append(cursor)
        cursor += len(b)
    arr = (n.to_bytes(32, "big")
           + b"".join(o.to_bytes(32, "big") for o in offsets)
           + b"".join(bodies))
    return SEL_AGGREGATE3 + ((0x20).to_bytes(32, "big") + arr).hex()


def decode_aggregate3(result_hex: str) -> list[tuple[bool, str]]:
    """Decode aggregate3 return into [(success, returndata_hex)]."""
    raw = bytes.fromhex(result_hex[2:] if result_hex.startswith("0x") else result_hex)
    base = int.from_bytes(raw[0:32], "big")
    n = int.from_bytes(raw[base:base + 32], "big")
    items_base = base + 32
    out = []
    for i in range(n):
        off = int.from_bytes(raw[items_base + 32 * i: items_base + 32 * (i + 1)], "big")
        t = items_base + off
        success = int.from_bytes(raw[t:t + 32], "big") == 1
        data_off = int.from_bytes(raw[t + 32:t + 64], "big")
        d = t + data_off
        dlen = int.from_bytes(raw[d:d + 32], "big")
        out.append((success, "0x" + raw[d + 32:d + 32 + dlen].hex()))
    return out


def multicall(rpc, calls: list[tuple[str, str]], chunk: int = 500,
              gas: int | None = None) -> list[tuple[bool, str]]:
    """Batched aggregate3 over an Rpc (eth_call only). Chunked so a single response stays under
    public-endpoint size limits; bounded retries per chunk.

    gas defaults to None (omit the eth_call gas cap -> the node picks its default). Some HyperEVM
    endpoints (rpc.hyperliquid.xyz) reject an explicit 50M eth_call gas as "intrinsic gas too high"
    on small-block state; omitting it works on every reachable endpoint (verified 2026-07-15)."""
    import time

    out = []
    for i in range(0, len(calls), chunk):
        part = calls[i:i + chunk]
        data = encode_aggregate3(part)
        for attempt in range(4):
            try:
                res = rpc.eth_call(MULTICALL3, data, gas=gas)
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(0.4 * (attempt + 1))
        out.extend(decode_aggregate3(res))
    return out
