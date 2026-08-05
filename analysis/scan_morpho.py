#!/usr/bin/env python3
"""Povtor zamera Morpho s ocenkoy DOMINIRUYUSHCHIH rynkov (AVLT/USDT0 i dr.).

Loan-tokeny vezde stable s 6 znakami (USDT0, USDHL) => repaidAssets/1e6 ~ USD napryamuyu.
Seized schitaem cherez ORAKUL SAMOGO RYNKA Morpho (oracle.price(), masshtab 1e36 / (10^(36+
dec_loan-dec_coll)) po specifikacii Morpho) — nash AaveOracle etih tokenov ne znaet.

Kontrol: dolya NEocenennyh dolzhna upast do ~0; pechataem yavno."""
import collections, json
from analysis.rpc import Rpc
from analysis.keccak import event_topic0, selector
from analysis import protocols as P

MORPHO = "0x68e37de8d93d3496ae143f2e900490f6280c57cd"
T_LIQ = event_topic0("Liquidate(bytes32,address,address,uint256,uint256,uint256,uint256,uint256)")
SEL_PARAMS = selector("idToMarketParams(bytes32)")
SEL_PRICE = selector("price()")
r = Rpc()
rows = [x for x in json.load(open('/home/claude-agent/.claude/jobs/d9c2c3f6/tmp/sweep30d.json'))
        if x['contract'] == MORPHO]

def call(to, data):
    return r.call("eth_call", [{"to": to, "data": data}, "latest"])

_p, _d, _s = {}, {}, {}
def params(mid):
    if mid not in _p:
        h = call(MORPHO, SEL_PARAMS + mid[2:])
        _p[mid] = ("0x" + h[2:66][-40:], "0x" + h[66:130][-40:], "0x" + h[130:194][-40:])
    return _p[mid]                      # loan, coll, oracle
def dec(t):
    if t not in _d:
        try: _d[t] = int(call(t, "0x313ce567"), 16)
        except Exception: _d[t] = None
    return _d[t]
def sym(t):
    if t not in _s:
        try:
            b = bytes.fromhex(call(t, "0x95d89b41")[2:])
            off = int.from_bytes(b[0:32], 'big'); ln = int.from_bytes(b[off:off+32], 'big')
            _s[t] = b[off+32:off+32+ln].decode('utf8', 'ignore')
        except Exception: _s[t] = t[:10]
    return _s[t]
_op = {}
def oprice(o):
    if o not in _op:
        try: _op[o] = int(call(o, SEL_PRICE), 16)
        except Exception: _op[o] = None
    return _op[o]

STABLE = {"USD₮0", "USDHL", "USDC", "USDe", "USDH", "USDT0"}
win = collections.Counter(); winusd = collections.Counter()
prizes = []; unpriced = collections.Counter(); mkt = collections.Counter()
for i, x in enumerate(rows):
    if i % 150 == 0: print(f"  ...{i}/{len(rows)}", flush=True)
    rc = r.call("eth_getTransactionReceipt", [x['tx']])
    for l in rc['logs']:
        if l['address'].lower() != MORPHO or l['topics'][0].lower() != T_LIQ.lower():
            continue
        mid = l['topics'][1]; caller = "0x" + l['topics'][2][-40:]
        d = l['data'][2:]
        repaid = int(d[0:64], 16); seized = int(d[128:192], 16)
        loan, coll, orc = params(mid)
        dl, dc, pr = dec(loan), dec(coll), oprice(orc)
        win[caller] += 1
        mkt[f"{sym(coll)}/{sym(loan)}"] += 1
        if None in (dl, dc, pr) or sym(loan) not in STABLE:
            unpriced[f"{sym(coll)}/{sym(loan)}"] += 1
            continue
        # Morpho: collateral value v loan-edinicah = seized * price / 1e36
        coll_in_loan = seized * pr / 10 ** 36
        v = (coll_in_loan - repaid) / 10 ** dl
        prizes.append(v); winusd[caller] += v

prizes.sort(reverse=True)
n_ok, n_bad = len(prizes), sum(unpriced.values())
print(f"\nMorpho Blue za 30 sut: sobytiy {sum(win.values())}; OCENENO {n_ok}, ne ocenit {n_bad}"
      f" ({n_bad/max(n_ok+n_bad,1):.0%})")
print(f"  priz vsego ${sum(prizes):,.0f}")
for th in (10, 100, 1000):
    b = [p for p in prizes if p >= th]
    print(f"  >=${th:5}: {len(b):4} sht, ${sum(b):,.0f}")
print(f"  top-8: {[f'${p:,.0f}' for p in prizes[:8]]}")
print(f"\n  rynki: {dict(mkt.most_common(6))}")
print(f"  pobediteley: {len(win)}")
for a, n in win.most_common(8):
    print(f"    {a} {n:4} sht  ${winusd[a]:>12,.0f}")
if winusd:
    print(f"  koncentratsiya top-1 po dengam: {max(winusd.values())/max(sum(winusd.values()),1):.0%}")
print(f"  ne ocenit po rynkam: {dict(unpriced.most_common(5))}")
