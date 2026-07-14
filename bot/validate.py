"""DRY-RUN pipeline validator (READ-ONLY, never sends).

There is usually no HF<1 position in calm markets (the whole thesis: dust in calm, real in
crashes). To prove the fire path works end-to-end without waiting for a crash, this harness takes
the current near-edge watch candidates (lowest HF), PROMOTES each to a target, and runs it through
the exact evaluate() + fire() code the live loop uses — with DRY_RUN forced on. It exercises:
Aave sizing -> live LiquidSwap route quote -> flash-repay + gas net -> profitability gate ->
the "guard=DRY, NOT sent" decline. Output is the honest per-candidate decision.

Usage: python3 -u -m bot.validate            # uses live HyperEVM RPC + live LiquidSwap
"""
from __future__ import annotations

import os
import sys

os.environ["DRY_RUN"] = "1"  # force safe before importing config

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from bot import config as C            # noqa: E402
from bot import executor as E          # noqa: E402
from analysis.monitor import load_book, scan  # noqa: E402
from analysis.rpc import Rpc           # noqa: E402


def main() -> None:
    assert C.DRY_RUN, "validator must run with DRY_RUN on"
    rpc = Rpc(C.READ_RPCS)
    book = load_book()
    # widen the watch/report ceiling so we always have candidates to exercise the path against
    r = scan(rpc, book, min_debt_usd=C.MIN_DEBT_USD, watch_hf=1.20, report_hf=1.20)
    print(f"block {r['block']} | book {r['n_book']} | positions {r['n_positions']} | "
          f"real targets(HF<1) {len(r['targets'])} | near-edge risk {len(r['risk'])}")

    # promote the lowest-HF near-edge rows to synthetic targets (real on-chain sizing + prices)
    candidates = sorted(r["targets"] + r["risk"], key=lambda x: x["hf"])[:5]
    if not candidates:
        print("no candidates in book right now")
        return
    gas_usd = E.gas_cost_usd(rpc)
    print(f"gas est ${gas_usd:.4f}/liq | min_profit ${C.MIN_PROFIT_USD} | "
          f"path={'flash' if C.USE_FLASHLOAN else 'capital'} | premium {C.FLASH_PREMIUM_BPS}bps\n")
    for t in candidates:
        tag = "TARGET(HF<1)" if t["hf"] < 1.0 else f"promoted watch HF={t['hf']:.4f}"
        print(f"--- {t['coll_sym']}->{t['debt_sym']} {t['borrower'][:12]}… [{tag}] "
              f"cover=${t['repaid_usd']:,.0f} seized={t['seized']} bonus≈${t['gross_bonus_usd']:,.0f}")
        ev = E.evaluate(t, gas_usd)
        if ev is None:
            print("    evaluate -> None (unsized)"); continue
        if "skip" in ev:
            print(f"    SKIP: {ev['skip']}"); continue
        print(f"    LiquidSwap: proceeds={ev['proceeds']} debt-wei, impact={ev['impact']*100:.2f}%, "
              f"flashRepay={ev['owed']}, net=${ev['net_usd']:+,.2f}, "
              f"profitable={ev['profitable']}, minProfitWei={ev['min_profit_wei']}")
        # run the real fire() — DRY_RUN on => prints the decline, sends nothing
        E.fire(t, ev, {"fires": 0, "gas_usd": 0.0, "consec_reverts": 0, "reverts": 0, "sent": {}},
               0.0, gas_usd)
    print("\nDRY-RUN validation complete — no transaction was sent (guard=DRY throughout).")


if __name__ == "__main__":
    main()
