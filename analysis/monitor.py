"""HyperLend position monitor — on-chain, READ-ONLY. Discovers borrowers, reads their health
factor straight from the Pool, and for near-edge users picks the best (debt, collateral) leg and
sizes a liquidation. No transactions on this path, ever.

Used two ways:
  * CLI paper mode: `python3 -m analysis.monitor` — one pass, prints the risk table + WOULD-
    LIQUIDATE lines. Writes data/monitor.log.
  * As the executor's target finder: scan() returns structured target rows the live executor
    re-validates (fresh HF) and fires on.

Discovery (Aave v3 has no Morpho-style indexer here, so it is on-chain):
  1. Borrower set = onBehalfOf (indexed topic[2]) of every Pool `Borrow` log. Built incrementally
     from a persisted checkpoint over bounded getLogs windows; a one-time full backfill from the
     Pool deploy block is available via HL_DEPLOY_BLOCK. The set is persisted (data/book.json) and
     grows monotonically; cured/closed positions simply read back healthy and are skipped.
  2. Each pass: one Multicall3 sweep of getUserAccountData(user) over the whole book -> HF.
  3. For users under the watch ceiling: Multicall3 getUserReserveData(asset,user) across reserves
     + AaveOracle prices + cached reserve configs -> pick legs -> size.
"""
from __future__ import annotations

import json
import os
import time

from analysis.aave import (
    HF_INFINITY, SEL_GET_ASSET_PRICE, SEL_GET_RESERVE_CONFIG, SEL_GET_RESERVES_LIST,
    SEL_GET_USER_ACCOUNT_DATA, SEL_GET_USER_RESERVE_DATA, close_factor, decode_address_array,
    decode_reserve_config, decode_user_account_data, decode_user_reserve_data, size_liquidation,
)
from analysis.multicall import multicall
from analysis.protocols import (
    ADDR_TO_SYMBOL, ORACLE, ORACLE_BASE_UNIT, POOL, POOL_DATA_PROVIDER, TOPIC_BORROW,
    is_stable, sym,
)
from analysis.rpc import Rpc, get_logs_chunked

# --- config (env) --------------------------------------------------------------
DEPLOY_BLOCK = int(os.environ.get("HL_DEPLOY_BLOCK", "0"))     # >0 to backfill the whole history
LOG_CHUNK = int(os.environ.get("HL_LOG_CHUNK", "4000"))       # getLogs window (drpc caps ~5k)
INCR_WINDOW = int(os.environ.get("HL_INCR_WINDOW", "20000"))  # per-pass fresh-Borrow window back from head
WATCH_HF = float(os.environ.get("HL_WATCH_HF", "1.10"))       # refine (read reserves) below this HF
REPORT_HF = float(os.environ.get("HL_REPORT_HF", "1.15"))     # risk-table cutoff
MIN_DEBT_USD = float(os.environ.get("HL_MIN_DEBT_USD", "500"))

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
BOOK_PATH = os.path.join(DATA_DIR, "book.json")
LOG_PATH = os.path.join(DATA_DIR, "monitor.log")


# --- IO ------------------------------------------------------------------------
def load_book(path: str = BOOK_PATH) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"last_block": DEPLOY_BLOCK - 1, "borrowers": [], "configs": {}}


def save_book(book: dict, path: str = BOOK_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(book, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


# --- discovery -----------------------------------------------------------------
def discover_borrowers(rpc: Rpc, book: dict, to: int) -> set[str]:
    """Merge Borrow-log borrowers from (max(checkpoint, head-window)..head) into the book set.
    On the first run with HL_DEPLOY_BLOCK set, this backfills the full history (chunked)."""
    borrowers = set(a.lower() for a in book.get("borrowers", []))
    frm = max(book.get("last_block", -1) + 1, to - INCR_WINDOW if INCR_WINDOW else to)
    if DEPLOY_BLOCK and not borrowers:
        frm = DEPLOY_BLOCK           # one-time full backfill
    if frm > to:
        return borrowers
    logs = get_logs_chunked(rpc, POOL, [TOPIC_BORROW], frm, to, chunk=LOG_CHUNK)
    for lg in logs:
        borrowers.add("0x" + lg["topics"][2][-40:])
    return borrowers


# --- reserve config cache ------------------------------------------------------
def load_reserves(rpc: Rpc, book: dict) -> dict:
    """{reserve_addr: {decimals, liquidation_bonus, liquidation_threshold, is_active, is_frozen}}
    Cached in the book; refreshed if empty. Bonuses can move on governance, so the executor also
    re-reads them, but for scan sizing the cache is fine (refreshed each cold start)."""
    cfg = book.get("configs") or {}
    if cfg:
        return {k.lower(): v for k, v in cfg.items()}
    reserves = decode_address_array(rpc.eth_call(POOL, SEL_GET_RESERVES_LIST))
    res = multicall(rpc, [(POOL_DATA_PROVIDER, SEL_GET_RESERVE_CONFIG + a[2:].rjust(64, "0"))
                          for a in reserves])
    out = {}
    for a, (ok, ret) in zip(reserves, res):
        if ok and len(ret) >= 2 + 10 * 64:
            c = decode_reserve_config(ret)
            out[a.lower()] = {"decimals": c["decimals"], "liquidation_bonus": c["liquidation_bonus"],
                              "liquidation_threshold": c["liquidation_threshold"],
                              "is_active": c["is_active"], "is_frozen": c["is_frozen"]}
    book["configs"] = out
    return out


# --- core scan (shared by CLI + executor) --------------------------------------
def scan(rpc: Rpc | None = None, book: dict | None = None,
         min_debt_usd: float = MIN_DEBT_USD, watch_hf: float = WATCH_HF,
         report_hf: float = REPORT_HF) -> dict:
    """One pass. Returns {block, n_positions, targets:[HF<1 & sized], risk:[HF<report_hf], book}.
    Each target row carries everything the executor needs to fire: debt/coll assets + decimals,
    debtToCover (wei), expected seized (wei), prices, HF, close factor."""
    rpc = rpc or Rpc()
    book = book if book is not None else load_book()
    to = rpc.block_number()

    reserves_cfg = load_reserves(rpc, book)
    reserves = list(reserves_cfg.keys())

    # 1. discovery
    borrowers = discover_borrowers(rpc, book, to)
    borrower_list = sorted(borrowers)

    # 2. sweep getUserAccountData for the whole book
    res = multicall(rpc, [(POOL, SEL_GET_USER_ACCOUNT_DATA + b[2:].rjust(64, "0"))
                          for b in borrower_list])
    accounts = {}
    for b, (ok, ret) in zip(borrower_list, res):
        if ok and len(ret) >= 2 + 6 * 64:
            accounts[b] = decode_user_account_data(ret)

    # near-edge set: finite HF under the watch/report ceiling and non-dust debt
    ceiling = max(watch_hf, report_hf)
    near = [b for b, a in accounts.items()
            if a["health_factor"] < int(ceiling * 1e18)
            and a["total_debt_base"] / ORACLE_BASE_UNIT >= min_debt_usd
            and a["health_factor"] < HF_INFINITY]

    # 3. per-asset reserve data + prices for the near-edge set only (bounded)
    prices, user_reserves = {}, {}
    if near:
        pres = multicall(rpc, [(ORACLE, SEL_GET_ASSET_PRICE + a[2:].rjust(64, "0"))
                               for a in reserves])
        for a, (ok, ret) in zip(reserves, pres):
            if ok and len(ret) >= 66:
                prices[a] = int(ret[2:66], 16)
        pairs = [(b, a) for b in near for a in reserves]
        rres = multicall(rpc, [(POOL_DATA_PROVIDER,
                                SEL_GET_USER_RESERVE_DATA + a[2:].rjust(64, "0") + b[2:].rjust(64, "0"))
                               for b, a in pairs])
        for (b, a), (ok, ret) in zip(pairs, rres):
            if ok and len(ret) >= 2 + 9 * 64:
                d = decode_user_reserve_data(ret)
                if d["variable_debt"] + d["stable_debt"] > 0 or d["aToken_balance"] > 0:
                    user_reserves.setdefault(b, {})[a] = d

    # 4. build target/risk rows
    targets, risk = [], []
    for b in near:
        acct = accounts[b]
        row = _build_row(b, acct, user_reserves.get(b, {}), reserves_cfg, prices)
        if row is None:
            continue
        if acct["health_factor"] < int(1e18) and (row["repaid_usd"] >= min_debt_usd or row["debt_to_cover"] > 0):
            targets.append(row)
        else:
            risk.append(row)

    # persist book (grows monotonically; configs cached)
    book["last_block"] = to
    book["borrowers"] = borrower_list
    return {"block": to, "n_positions": len(accounts), "n_book": len(borrower_list),
            "targets": targets, "risk": risk, "book": book}


def _build_row(borrower: str, acct: dict, ureserves: dict, cfg: dict, prices: dict) -> dict | None:
    """Pick the best (debt, collateral) leg for `borrower` and size the liquidation."""
    hf = acct["health_factor"]
    cf = close_factor(acct["total_debt_base"], hf)

    # debt legs: reserves with positive borrow; value in USD*1e8. Prefer a stable (cheap exit).
    debts, colls = [], []
    for a, d in ureserves.items():
        c = cfg.get(a)
        if not c:
            continue
        debt_wei = d["variable_debt"] + d["stable_debt"]
        if debt_wei > 0 and prices.get(a):
            val = debt_wei * prices[a] // (10 ** c["decimals"])
            debts.append((a, debt_wei, val))
        if (d["aToken_balance"] > 0 and d["usage_as_collateral"] and c["liquidation_bonus"] > 10000
                and prices.get(a)):
            val = d["aToken_balance"] * prices[a] // (10 ** c["decimals"])
            colls.append((a, d["aToken_balance"], val, c["liquidation_bonus"]))
    if not debts or not colls:
        return None

    # debt leg: largest value, tie-break to a stable. collateral leg: largest bonus-weighted value.
    debts.sort(key=lambda x: (is_stable(x[0]), x[2]), reverse=True)
    colls.sort(key=lambda x: x[2] * x[3], reverse=True)
    d_addr, d_wei, _ = debts[0]
    c_addr, c_wei, _, bonus = colls[0]
    dc, cc = cfg[d_addr], cfg[c_addr]

    sz = size_liquidation(d_wei, dc["decimals"], prices[d_addr],
                          c_wei, cc["decimals"], prices[c_addr], bonus, cf)
    if sz["debt_to_cover"] <= 0 or sz["seized"] <= 0:
        return None
    return {
        "borrower": borrower, "hf": hf / 1e18, "close_factor": cf,
        "debt_asset": d_addr, "debt_dec": dc["decimals"], "debt_price": prices[d_addr],
        "coll_asset": c_addr, "coll_dec": cc["decimals"], "coll_price": prices[c_addr],
        "bonus_bps": bonus, "debt_to_cover": sz["debt_to_cover"], "seized": sz["seized"],
        "repaid_usd": sz["repaid_usd"], "seized_usd": sz["seized_usd"],
        "gross_bonus_usd": sz["gross_bonus_usd"],
        "total_debt_usd": acct["total_debt_base"] / ORACLE_BASE_UNIT,
        "debt_sym": sym(d_addr), "coll_sym": sym(c_addr),
    }


def run_once(rpc: Rpc | None = None, book_path: str = BOOK_PATH, log_path: str = LOG_PATH) -> str:
    book = load_book(book_path)
    r = scan(rpc, book)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    lines = [f"=== {now} block {r['block']} | book {r['n_book']} | positions {r['n_positions']} "
             f"| targets(HF<1) {len(r['targets'])} | risk(HF<{REPORT_HF}) {len(r['risk'])}"]
    for t in sorted(r["targets"], key=lambda x: x["hf"]):
        lines.append(f"  >>> WOULD LIQUIDATE hf={t['hf']:.4f} cf={t['close_factor']:.0%} "
                     f"{t['coll_sym']}->{t['debt_sym']} cover=${t['repaid_usd']:,.0f} "
                     f"bonus≈${t['gross_bonus_usd']:,.0f} {t['borrower']}")
    for t in sorted(r["risk"], key=lambda x: x["hf"])[:20]:
        lines.append(f"  hf={t['hf']:.4f} debt=${t['total_debt_usd']:,.0f} "
                     f"{t['coll_sym']}->{t['debt_sym']} {t['borrower']}")
    report = "\n".join(lines) + "\n"
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a") as f:
        f.write(report)
    save_book(r["book"], book_path)
    return report


if __name__ == "__main__":
    print(run_once(), end="")
