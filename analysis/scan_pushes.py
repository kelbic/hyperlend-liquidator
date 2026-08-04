#!/usr/bin/env python3
"""Исторический скан пушей релейера в оба RedStone-адаптера HyperLend.

Меряет то, что решает полосу tip'ов predict-потребителя:
  - tip_gwei каждого пуша + baseFee + конгестия блока (кол-во tx, медианный tip соседей)
  - задержку релейера: block_ts*1000 - max(pkg_ts) из calldata (пересечение порога -> лендинг)
  - реальную девиацию пуш-к-пушу по каждому фиду (из событий адаптера) = наблюдаемый overshoot

Выход: pushes.jsonl + сводка на stdout.
"""
import json
import sys
import time
import urllib.request
from collections import defaultdict

RPCS = ["https://hyperliquid.drpc.org", "https://rpc.hyperliquid.xyz/evm"]
ADAPTER_A = "0xe4ae88743c3834d0c492eabc47384c84bcadc6a6"
ADAPTER_B = "0x24c8964338deb5204b096039147b8e8c3aea42cc"
MARKER = bytes.fromhex("000002ed57011e0000")
VALUE_BS = 32
HOURS = float(sys.argv[1]) if len(sys.argv) > 1 else 24.0
OUT = "/home/claude-agent/.claude/jobs/d9c2c3f6/tmp/pushes.jsonl"

_rpc_i = 0


def rpc(method, params, retries=4):
    global _rpc_i
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    last = None
    for att in range(retries):
        url = RPCS[(_rpc_i + att) % len(RPCS)]
        try:
            req = urllib.request.Request(url, data=body, headers={
                "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                out = json.loads(r.read())
            if "error" in out:
                raise RuntimeError(f"{url}: {out['error']}")
            _rpc_i = (_rpc_i + att) % len(RPCS)
            return out["result"]
        except Exception as e:
            last = e
            time.sleep(0.5 * (att + 1))
    raise RuntimeError(f"rpc {method} failed: {last}")


def parse_payload_ts(data: bytes):
    """Таймстемпы пакетов из хвоста calldata (зеркало _serialize_package)."""
    if MARKER not in data:
        return []
    end = data.rfind(MARKER)
    p = end
    unsig_sz = int.from_bytes(data[p - 3:p], "big")
    p -= 3 + unsig_sz
    n_pkg = int.from_bytes(data[p - 2:p], "big")
    p -= 2
    ts_list = []
    for _ in range(n_pkg):
        p -= 65                                   # signature
        n_pts = int.from_bytes(data[p - 3:p], "big")
        p -= 3
        vbs = int.from_bytes(data[p - 4:p], "big")
        p -= 4
        ts = int.from_bytes(data[p - 6:p], "big")
        p -= 6
        p -= n_pts * (32 + vbs)                   # data points
        ts_list.append(ts)
        if p < 4:
            break
    return ts_list


def main():
    latest = int(rpc("eth_blockNumber", []), 16)
    span = int(HOURS * 3600)                      # ~1s блоки
    frm = latest - span
    print(f"скан {frm}..{latest} ({HOURS}ч, {span} блоков)", flush=True)

    logs = []
    step = 1000
    b = frm
    while b < latest:
        e = min(b + step - 1, latest)
        try:
            chunk = rpc("eth_getLogs", [{
                "fromBlock": hex(b), "toBlock": hex(e),
                "address": [ADAPTER_A, ADAPTER_B]}])
            logs.extend(chunk)
        except Exception as ex:
            print(f"  getLogs {b}..{e} FAIL: {ex}", flush=True)
        b = e + 1
        if (b - frm) % 10000 < step:
            print(f"  ...{b - frm}/{span} блоков, логов {len(logs)}", flush=True)

    print(f"логов всего: {len(logs)}", flush=True)

    # события: ValueUpdate(value, dataFeedId, updatedAt) — соберём значения по фидам
    by_tx = defaultdict(list)
    for lg in logs:
        by_tx[lg["transactionHash"]].append(lg)
    print(f"уникальных tx: {len(by_tx)}", flush=True)

    blocks = {}
    rows = []
    for i, (txh, lgs) in enumerate(sorted(by_tx.items(),
                                          key=lambda kv: int(kv[1][0]["blockNumber"], 16))):
        try:
            tx = rpc("eth_getTransactionByHash", [txh])
            rc = rpc("eth_getTransactionReceipt", [txh])
            bn = int(tx["blockNumber"], 16)
            if bn not in blocks:
                blocks[bn] = rpc("eth_getBlockByNumber", [hex(bn), False])
            blk = blocks[bn]
            base = int(blk.get("baseFeePerGas", "0x0"), 16)
            block_ts = int(blk["timestamp"], 16)
            data = bytes.fromhex(tx["input"][2:])
            ts_list = parse_payload_ts(data)
            feeds = {}
            for lg in lgs:
                # ValueUpdate: непонятна раскладка заранее — сохраняем сырьё
                feeds.setdefault(lg["address"].lower(), []).append(
                    {"topics": lg["topics"], "data": lg["data"]})
            tip = (int(rc["effectiveGasPrice"], 16) - base) / 1e9
            rows.append({
                "block": bn, "block_ts": block_ts, "tx": txh,
                "adapter": lgs[0]["address"].lower(),
                "from": tx["from"], "selector": tx["input"][:10],
                "tip_gwei": round(tip, 4), "base_gwei": round(base / 1e9, 4),
                "gas": int(rc["gasUsed"], 16), "st": int(rc["status"], 16),
                "n_pkg": len(ts_list),
                "pkg_ts_max": max(ts_list) if ts_list else None,
                "pkg_ts_min": min(ts_list) if ts_list else None,
                "delay_ms": (block_ts * 1000 - max(ts_list)) if ts_list else None,
                "n_block_txs": len(blk["transactions"]),
                "logs": feeds,
            })
        except Exception as ex:
            print(f"  tx {txh[:14]} FAIL: {ex}", flush=True)
        if i % 50 == 0:
            print(f"  ...tx {i}/{len(by_tx)}", flush=True)

    with open(OUT, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"записано {len(rows)} пушей в {OUT}", flush=True)

    ok = [r for r in rows if r["st"] == 1 and r["delay_ms"] is not None]
    if ok:
        tips = sorted(r["tip_gwei"] for r in ok)
        delays = sorted(r["delay_ms"] for r in ok)
        q = lambda a, p: a[min(len(a) - 1, int(p * len(a)))]
        print(f"\nпушей ok: {len(ok)}")
        print(f"tip gwei:  p10={q(tips,.1):.3f} p50={q(tips,.5):.3f} "
              f"p90={q(tips,.9):.3f} max={tips[-1]:.3f}")
        print(f"delay ms:  p10={q(delays,.1)} p50={q(delays,.5)} "
              f"p90={q(delays,.9)} max={delays[-1]}")
        froms = defaultdict(int)
        for r in ok:
            froms[r["from"]] += 1
        print("отправители:", dict(froms))
        sels = defaultdict(int)
        for r in ok:
            sels[r["selector"]] += 1
        print("селекторы:", dict(sels))


if __name__ == "__main__":
    main()
