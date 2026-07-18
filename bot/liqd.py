"""LiquidSwap (Liquid Labs) v2 aggregator client for HyperEVM — quotes AND ready-to-execute swap
calldata. LiquidSwap routes across ~19 HyperEVM DEXes and returns tx calldata whose `to` is the
LiquidSwap Router; that (to, calldata) pair is exactly the (swapTarget, swapCallData) the
HyperLendLiquidator callback needs to turn seized collateral into the debt asset (same pattern as
the katana bot's Sushi integration).

READ-ONLY: HTTP GET quotes only. Never signs or sends.

Two integration nuances baked in here:
  * UNIT CONVENTION (verified 2026-07-14): the API's `amountIn`/`amountOut` are HUMAN token units
    (whole tokens, decimal), NOT wei — amountIn=1 for WHYPE returns ~64.77 USDC. We convert wei ->
    human for the request and human -> wei for the returned output.
  * DRIFT SAFETY: the Router pulls the baked `amountIn` from the caller. If the collateral actually
    seized on-chain is slightly LESS than quoted (an adverse oracle tick between quote and exec),
    the swap would over-pull and revert. So we quote a slightly HAIRCUT amountIn; the baked amount
    is <= the real seized collateral, and any surplus is dust the contract sweeps. Quoting for less
    is strictly safe.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

from analysis.rpc import HardTimeout, _run_with_deadline  # noqa: F401 (HardTimeout re-exported)

API = "https://api.liqd.ag/v2/route"
ROUTER = "0x744489ee3d540777a66f2cf297479745e0852f7a"  # LiquidSwap Router (baked as tx.to)

# 0.3% haircut comfortably covers per-block oracle/interest drift on ~1s HyperEVM blocks.
SWAP_INPUT_HAIRCUT = 0.003

# Indirection so tests can inject a fake/trickling transport without a network (same pattern as
# analysis/rpc.py). Production always uses the real urllib.request.urlopen.
_urlopen = urllib.request.urlopen

# HARD wall budget (s) over ALL retries+backoffs of one quote() call. The per-recv socket timeout
# (20s) does NOT bound wall time — a trickling api.liqd.ag can hold one attempt far past it, and
# with retries=3 + sleeps the old worst case was unbounded, blocking the single-threaded loop AT
# FIRE TIME (a quote is only requested once HF<1). 8s rationale: a healthy quote round-trip is
# well under 1s, so 8s comfortably fits a full healthy retry cycle (3 attempts + 0.4/0.8s
# backoffs) — but caps the worst case under ~3 hot-loop iterations. On a latency-FCFS chain a
# quote that hasn't answered in 8s has already lost the race, and waiting longer starves every
# OTHER target mid-cascade: speed over completeness.
QUOTE_WALL_SEC = float(os.environ.get("HL_QUOTE_WALL_SEC", "8"))


class LiqdError(RuntimeError):
    pass


class NoRouteError(LiqdError):
    """No swappable route for this pair at this size (success=false / no viable pools, or a 4xx).
    The caller should skip the target rather than retry."""


def _to_human(amount_wei: int, decimals: int) -> str:
    """wei -> human decimal string, truncated to the token's precision (never rounds up, so the
    baked amountIn stays <= the real amount)."""
    q = Decimal(amount_wei) / (Decimal(10) ** decimals)
    return format(q.quantize(Decimal(1) / (Decimal(10) ** decimals)), "f")


def quote(token_in: str, token_out: str, amount_in_wei: int, in_decimals: int, out_decimals: int,
          multi_hop: bool = True, timeout: float = 20.0, retries: int = 3,
          wall_sec: float | None = None) -> dict:
    """One LiquidSwap route. Returns a normalised dict:
        {ok, amount_out (int, wei), price_impact (float 0..1), swap_target (str),
         swap_calldata (str 0x), amount_in_used (int wei), raw}
    Raises NoRouteError when no route exists (skip the target); retries transient network errors.

    The WHOLE retry loop (all attempts + backoff sleeps) runs under a HARD wall deadline of
    `wall_sec` (default QUOTE_WALL_SEC) via analysis.rpc._run_with_deadline — the socket timeout
    is per-recv, so a trickling endpoint could otherwise block the single-threaded loop right at
    fire time. On budget exhaustion HardTimeout propagates (evaluate() turns it into a target
    skip); the abandoned worker is a daemon and cannot block the loop. wall_sec=0 disables the
    cap (legacy/CLI behaviour)."""
    if wall_sec is None:
        wall_sec = QUOTE_WALL_SEC
    human_in = _to_human(amount_in_wei, in_decimals)
    if Decimal(human_in) <= 0:
        raise NoRouteError("amountIn rounds to zero at token precision")
    params = {"tokenIn": token_in, "tokenOut": token_out, "amountIn": human_in}
    if multi_hop:
        params["multiHop"] = "true"
    url = API + "?" + urllib.parse.urlencode(params)

    def _attempts() -> dict:
        last = None
        for attempt in range(retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with _urlopen(req, timeout=timeout) as r:
                    d = json.loads(r.read())
                if not d.get("success"):
                    raise NoRouteError(f"no route: {d.get('message')}")
                ex = d.get("execution") or {}
                if not ex.get("to") or not ex.get("calldata"):
                    raise NoRouteError("route had no execution calldata")
                amount_out_wei = int((Decimal(str(d["amountOut"])) * (Decimal(10) ** out_decimals))
                                     .quantize(Decimal(1)))
                impact = _parse_pct(d.get("averagePriceImpact"))
                return {
                    "ok": True,
                    "amount_out": amount_out_wei,
                    "price_impact": impact,
                    "swap_target": ex["to"],
                    "swap_calldata": ex["calldata"],
                    "amount_in_used": amount_in_wei,
                    "raw": d,
                }
            except NoRouteError:
                raise
            except urllib.error.HTTPError as e:
                if 400 <= e.code < 500:
                    raise NoRouteError(f"HTTP {e.code} (unsupported token/amount)") from e
                last = e
                time.sleep(0.4 * (attempt + 1))
            except (OSError, ValueError, json.JSONDecodeError) as e:
                last = e
                time.sleep(0.4 * (attempt + 1))
        raise LiqdError(f"quote failed after {retries}: {last}")

    if wall_sec:
        return _run_with_deadline(_attempts, wall_sec)
    return _attempts()


def quote_for_seized(coll_token: str, debt_token: str, seized_wei: int, coll_decimals: int,
                     debt_decimals: int, haircut: float = SWAP_INPUT_HAIRCUT) -> dict:
    """Quote the exit for a liquidation that will RECEIVE `seized_wei` of collateral (already net
    of the liquidation protocol fee — the caller passes the fee-adjusted figure), applying the
    drift-safety haircut on top so the baked amountIn <= the real received amount."""
    amount_in = int(seized_wei * (1.0 - haircut))
    q = quote(coll_token, debt_token, amount_in, coll_decimals, debt_decimals)
    q["haircut"] = haircut
    return q


def _parse_pct(s) -> float:
    if s is None:
        return 0.0
    try:
        return float(str(s).replace("%", "").strip()) / 100.0
    except ValueError:
        return 0.0
