"""Offline keccak helpers — function selectors and event topic0s.

Project rule (inherited from the wc/katana bots): topic0s and selectors are NEVER hand-pasted;
they are computed from the canonical signature at import time so a typo can't silently point the
bot at the wrong event/function.
"""
from __future__ import annotations

from eth_utils import keccak


def selector(sig: str) -> str:
    """4-byte function selector, e.g. selector("getUserAccountData(address)") -> 0xbf92857c."""
    return "0x" + keccak(text=sig)[:4].hex()


def event_topic0(sig: str) -> str:
    """32-byte event topic0 (the event signature hash)."""
    return "0x" + keccak(text=sig).hex()
