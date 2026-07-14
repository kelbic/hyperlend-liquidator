# STATE — hyperlend-liquidator

**As of 2026-07-14. Status: DRY-RUN, reviewed-ready. Nothing deployed; zero transactions sent.**

This is a cheap-option addition to the 3 live Morpho liquidators. It targets **mid-tier
($10k–$50k) liquidation spillover** on HyperLend during crash bursts — NOT whale tickets (those are
won on latency by Tokyo-colocated pros on a chain with no priority-fee auction; we run from
Vienna/US and lose those races structurally). See README for the full thesis.

## What was built

- **Contract** `contracts/src/HyperLendLiquidator.sol` — Aave-v3 flash-loan liquidator
  (`flashLoanSimple` → `liquidationCall` → LiquidSwap swap → repay → sweep), plus a capital
  fallback path. Compiles (solc 0.8.23). **7/7 Foundry unit tests pass** (flow + every safety gate).
- **Bot** `bot/` — executor loop cloned from the wc/katana framework: flock single-instance,
  `DRY_RUN` gate (default ON), hard gas limit (no `eth_estimateGas`), priority fee 0 (non-operative
  on HyperEVM), kill-switch (daily gas + consec reverts), dedup, TG alerts on fire/revert/kill-switch
  only, heartbeat OFF. `HL_` config prefix.
- **Discovery** `analysis/monitor.py` — on-chain: `Borrow`-log borrower set (persisted, monotonic)
  → Multicall3 `getUserAccountData` sweep → per-asset `getUserReserveData` + oracle prices for the
  near-edge set → Aave close-factor + collateral-capped sizing.
- **Exit** `bot/liqd.py` — LiquidSwap `api.liqd.ag/v2/route` client → swap calldata for the callback.
- **Tests** — `analysis/test_aave.py` (7/7 pure sizing tests pass); `contracts/test/…t.sol` (7/7).
- **Ops** — `bot/deploy.sh`, `bot/run.sh`, `bot/hyperlend-executor.service`, `.env.example`.

## Verification log (all on-chain via `cast`/RPC, 2026-07-14)

| Check | Result |
|---|---|
| RPC `hyperliquid.drpc.org` chainId | `0x3e7` (999) ✓, archive OK |
| Backup RPCs | `rpc.hyperliquid.xyz/evm`, `rpc.hyperlend.finance` both chainId 999 ✓ |
| Pool `ADDRESSES_PROVIDER()` | `0x72c98246…170C` ✓ (matches docs) |
| AddressesProvider `getPool/getPriceOracle/getPoolDataProvider/getACLManager` | all resolve to the docs addresses ✓ |
| `FLASHLOAN_PREMIUM_TOTAL()` | **4** (0.04%) → flash loans available ✓ |
| `getReservesList()` | 18 reserves; symbols/decimals read per token ✓ |
| Liquidation bonuses (`getReserveConfigurationData`) | WHYPE 10%, wstHYPE 15%, UBTC 20%, UETH 15%, USDT0/USDC 8%, USDH 10%, kHYPE 10%, sUSDe 8% — **match anchor facts** ✓; USDe/USDHL/USR LT=0 (not collateral) |
| Oracle `BASE_CURRENCY_UNIT` | `1e8` (USD) ✓; `getAssetPrice` returns sane USD (HYPE $64.6, BTC $64.1k, USDT0 $0.999) |
| `LiquidationCall` topic0 | `0xe413a321…005286` — **matches anchor** ✓ |
| `Borrow` topic0 | `0xb3d08482…d7dce0` — confirmed against real logs (onBehalfOf = topic[2]) ✓ |
| Multicall3 code | present, 3808 bytes ✓ |
| `getUserAccountData` | returns HF (1e18) + base amounts; live book of 55–56 borrowers built ✓ |
| LiquidSwap `/v2/route` | success schema `{execution:{to,calldata}, amountOut, averagePriceImpact}`; router `0x744489ee…2f7a`; `amountIn`/`amountOut` are **human token units** (verified) ✓ |

## DRY-RUN evidence (full log in `data/dryrun.log`)

`python3 -u -m bot.validate` (promotes live near-edge candidates through the real `evaluate()` +
`fire()` path, DRY forced on):

```
block 40449701 | book 56 | positions 56 | real targets(HF<1) 0 | near-edge risk 5
gas est $0.0067/liq | min_profit $25.0 | path=flash | premium 4bps

kHYPE->WHYPE  0x2385233a  cover=$137,386  impact=0.00%   net=$+13,003.45  profitable=True  -> DRY, NOT sent
kHYPE->WHYPE  0x1d7afab9  cover=$2,667,035 impact=26.66%  net=$-2,667,797  profitable=False -> REJECTED (whale, can't exit)
PT-kHYPE...   0xa625e8ae  cover=$4,044,615  -> SKIP: no LiquidSwap route (exotic Pendle PT collateral)
UBTC->USDC    0x2f42d303  cover=$2,089     impact=0.02%   net=$+414.09     profitable=True  -> DRY, NOT sent
USDT0->WHYPE  0x489c82a8  cover=$46,904    impact=0.01%   net=$+3,551.53   profitable=True  -> DRY, NOT sent
```

Interpretation: the full pipeline works — discover → size (Aave 50% close factor) → live LiquidSwap
quote → net after flash-repay+premium+gas → profitability gate → **decline (guard=DRY)**. It
**accepts** mid-tier ($2k / $46k) and small-whale ($137k) tickets that exit cleanly, **rejects** a
$2.67M ticket (26–40% price impact, deeply negative net), and **skips** exotic PT collateral with no
route. Exactly the designed behavior. No `real target (HF<1)` existed at run time — correct for a
calm market (the option is dormant until a crash).

## Key design decisions

- **Zero-capital flash loan chosen** (not capital): `FLASHLOAN_PREMIUM_TOTAL=4` bps confirmed on
  the Pool, so `flashLoanSimple` is the default. Capital fallback (`HL_USE_FLASHLOAN=0`) is
  implemented and unit-tested in case the Pool's flash loan is ever paused.
- **LiquidSwap as the exit** — HyperLend's own reference bot (github.com/hyperlendx/liquidator,
  Rust) uses LiquidSwap; it aggregates ~19 HyperEVM DEXes and returns ready calldata. Same idiom as
  katana's Sushi client. Whale tickets that can't exit atomically would need the HyperCore CLOB —
  intentionally out of scope (those are the latency races we skip).
- **Discovery is on-chain** (no Aave indexer here): Borrow-log borrower set + Multicall3 HF sweep.
  Bounded per-pass window + persisted book; `HL_DEPLOY_BLOCK` for a one-time full backfill.
- **Debt-leg pricing/sizing in USD (oracle 1e8)**; profit floor converted to debt-asset wei so the
  on-chain `minProfit` gate holds for non-stable debt (e.g. WHYPE debt, common on kHYPE positions).

## Honest risks / gaps (for the operator to close before/at go-live)

1. **LiquidSwap output recipient (fork-test this).** The `/v2/route` calldata bakes the swap; I did
   not confirm on a fork that the Router sends output to `msg.sender` (our contract) vs a baked
   recipient. If it routes elsewhere, the contract's `CannotRepay` check reverts the whole tx →
   **no loss**, just a missed liq. Confirm on an `anvil --fork` before live so misses aren't silent.
2. **No mainnet liquidation has been executed** (by design). The flash→liq→swap→repay path is
   unit-tested against mocks and every address is verified, but the first real fill should be a
   fork test (there is currently no HF<1 position to test against on live). Gas limit 2.5M is
   generous but confirm actual usage on the fork.
3. **Big kHYPE/HYPE-collateral book, WHYPE-debt.** The near-edge set is dominated by large
   kHYPE→WHYPE positions ($5M–$8M) that **cannot exit on-chain** (26–40% impact). The bot correctly
   declines them; realistically our catchable flow is the smaller mid-tier ($2k–$50k) tail, which is
   thinner than the whale flow. This is the option's known limitation — it pays in a crash when many
   mid-tier positions cross at once and the pros are saturated.
4. **Priority fee non-operative** → we cannot win a same-block latency race from Vienna/US against
   Tokyo colo. Accepted. Mitigation is presence + reliability, not speed.
5. **kHYPE / PT / sUSDe route depth** varies; the bot skips no-route pairs and gates on price
   impact (`HL_MAX_IMPACT=5%`), so a thin route just means a declined target, never a loss.
6. **Book warmth on cold start.** First run only sees `HL_INCR_WINDOW` (20k blocks) of borrowers;
   run once with `HL_DEPLOY_BLOCK=<pool deploy block>` to backfill the full history, then the book
   persists. (Pool deploy block not pinned here — find via first Pool tx; not required for calm-market
   operation since the near-edge set refreshes each pass.)

## Next steps (operator, after review)

1. `anvil --fork-url $HL_RPC` → fork-test one real liquidation (resolve risk #1/#2).
2. Optionally pin `HL_DEPLOY_BLOCK` and backfill the full borrower book once.
3. Deploy (`bot/deploy.sh`), fund HYPE gas, set `HL_CONTRACT`, verify DRY, then flip `DRY_RUN=0`.
   The operator creates the git remote later (as with wc/katana); nothing is pushed from here.
