# hyperlend-liquidator

An Aave-v3 liquidator bot for **HyperLend** on **HyperEVM** (chainId 999) — a cheap "call option"
addition to the existing fleet of 3 live Morpho liquidators (midnight/Base, wc/World Chain,
katana/Katana). Same executor/alerting/single-instance framework; the key difference is
**Aave-v3.6 mechanics** (`flashLoanSimple` + `liquidationCall`) instead of Morpho.

> **Status: DRY-RUN, reviewed-ready. Nothing is deployed and no transaction has ever been sent.**
> The contract exists and is tested; the bot runs against live HyperEVM and correctly declines to
> send (guard=DRY). Deployment, gas-funding, and flipping `DRY_RUN=0` are operator steps (below).

## The thesis (why this bot is an option, not a whale-hunter)

HyperLend is a growing Aave-v3.6 fork ($261M borrowed / $439M TVL). Liquidation flow is **dust in
calm, real in crashes** (largest single liq ever $719k; 41 tickets ≥$50k over 14 months; field-wide
gross only ~$1.1M/14mo). The big-dollar crash tickets are won on **latency** by two crisis pros
co-located in **AWS-Tokyo**, on a chain where **priority fee is non-operative** (latency-FCFS, no
priority auction — you cannot outbid, only out-speed). We run from **Vienna/US**, so we
**structurally lose the whale races**.

So this bot is **not** a whale-hunter. Its realistic job is to **harvest mid-tier ($10k–$50k)
spillover** during crash bursts when the Tokyo pros hit capacity (24 distinct addresses have won
≥$10k tickets historically → mid-tier is contestable). Design consequences, enforced in code:
- **Don't burn gas racing whales you'll lose** — hard net-profit gate; decline anything that can't
  exit on-chain profitably (the DRY-run below correctly rejects a $2.67M ticket at 40% price impact).
- **Be reliable and present** — cheap to keep up (gas ~$0.007/liq); the option pays off in a crash.

## Architecture

```
analysis/         READ-ONLY on-chain infra (stdlib JSON-RPC; no web3)
  rpc.py            rotating read-only RPC client (UA header — drpc 403s without it)
  keccak.py         offline selectors / event topic0s (never hand-pasted)
  multicall.py      Multicall3 aggregate3 (N reads -> 1 round-trip)
  protocols.py      HyperLend address book + token registry (ALL verified on-chain)
  aave.py           Aave-v3 decoders + liquidation sizing (close factor, collateral cap)
  monitor.py        discovery (Borrow logs) + per-pass scan -> sized targets
bot/
  config.py         HL_-prefixed env config (DRY_RUN default ON)
  liqd.py           LiquidSwap (api.liqd.ag) route client -> swap calldata for the exit
  executor.py       live-signing loop: scan -> evaluate -> fire (flock, guards, DRY gate)
  validate.py       DRY-RUN pipeline validator (exercises the fire path on live candidates)
  run.sh / deploy.sh / hyperlend-executor.service
contracts/
  src/HyperLendLiquidator.sol   Aave-v3 flash-loan liquidator (+ capital fallback)
  test/HyperLendLiquidator.t.sol  7 unit tests (mocks): flow + every safety gate
  script/Deploy.s.sol
```

## Contract flow — `HyperLendLiquidator.sol`

Zero-capital, atomic, one transaction (flash path, default):
1. `liquidate(collateral, debt, user, debtToCover, useFlashloan=true, swapTarget, swapCalldata, minProfit)`
   flash-loans `debtToCover` of the **debt asset** from HyperLend's own Pool
   (`flashLoanSimple`, premium **0.04%** verified on-chain).
2. `executeOperation` callback: `Pool.liquidationCall(collateral, debt, user, debtToCover,
   receiveAToken=false)` — the Pool pulls the debt repayment and sends the **seized collateral +
   liquidation bonus** here.
3. Swap **all** seized collateral → debt asset via the **LiquidSwap Router** (generic
   `swapTarget`/`swapCallData` built off-chain by the bot from `api.liqd.ag`).
4. Approve the Pool to pull `debtToCover + premium`; the leftover debt asset is the **profit**,
   swept to the owner.

**Capital fallback** (`useFlashloan=false`): if the Pool's flash loan is ever paused, the operator
pre-funds the contract with the debt asset and it liquidates directly (no premium). Same
balance-based profit accounting.

**Safety (layered, all tested):** `onlyOwner` entry (swap calldata is always ours), `onlyPool`
callback with `initiator==this`, `nonReentrant`, **on-chain `minProfit` gate** (reverts a losing
liquidation even on a stale quote), can-repay check, return-data-checked ERC20 ops, force-approve
with allowance reset, market params passed as arguments (never hardcoded). The contract holds **no
standing funds** (not a honeypot); the hot wallet holds only gas.

## Discovery

Aave-v3 has no Morpho-style position indexer here, so discovery is **on-chain**:
1. **Borrower set** = `onBehalfOf` (indexed topic[2]) of every Pool `Borrow` log, built
   incrementally from a persisted checkpoint over bounded `getLogs` windows. A one-time **full
   backfill** from the Pool deploy block is available via `HL_DEPLOY_BLOCK`. The set is persisted
   (`data/book.json`) and grows monotonically; cured positions read back healthy and are skipped.
2. Each pass: one **Multicall3** sweep of `getUserAccountData(user)` over the whole book → HF.
3. For borrowers under the watch ceiling: Multicall3 `getUserReserveData(asset,user)` across
   reserves + `AaveOracle` prices → pick the best `(debt, collateral)` leg → **size** the
   liquidation (Aave close factor: 100% if debt<$2k or HF<0.95, else 50%; capped by collateral).

## Exit DEX — LiquidSwap (`api.liqd.ag`)

The seized collateral is swapped to the debt asset via **LiquidSwap** (Liquid Labs), a HyperEVM
DEX aggregator across ~19 DEXes — the same integration idiom as the katana bot's Sushi client. The
`/v2/route` API returns ready-to-execute calldata whose `to` is the LiquidSwap Router
`0x744489ee3d540777a66f2cf297479745e0852f7a`; that `(to, calldata)` is the exact
`(swapTarget, swapCalldata)` the contract callback consumes. Note: the API's `amountIn`/`amountOut`
are **human token units** (verified), and a 0.3% input haircut keeps the baked `amountIn` ≤ the
real seized collateral (no over-pull). For tickets too large to exit on-chain, the operator's
alternative is the HyperCore CLOB (~$118M/24h) — out of scope for the atomic bot; those are the
whale tickets we intentionally skip.

## Verified addresses (on-chain, 2026-07-14)

| Contract | Address |
|---|---|
| Pool | `0x00A89d7a5A02160f20150EbEA7a2b5E4879A1A8b` |
| PoolAddressesProvider | `0x72c98246a98bFe64022a3190e7710E157497170C` |
| AaveOracle (USD, 1e8) | `0xC9Fb4fbE842d57EAc1dF3e641a281827493A630e` |
| ProtocolDataProvider | `0x4f4d4cA1e0a8A21FE0B460613bEbe917f2eb4326` |
| ACLManager | `0x10914Ee2C2dd3F3dEF9EFFB75906CA067700a04A` |
| Multicall3 | `0xcA11bde05977b3631167028862bE2a173976CA11` |
| LiquidSwap Router | `0x744489ee3d540777a66f2cf297479745e0852f7a` |

Liquidation bonuses (read from `getReserveConfigurationData`, match the anchor facts): WHYPE 10%,
wstHYPE 15%, UBTC 20%, UETH 15%, USDT0 8%, USDC 8%, USDH 10%, kHYPE 10%, sUSDe 8%. USDe/USDHL/USR
have LT=0 (not seizable collateral). Flash-loan premium 0.04%.

## Reference: HyperLend's own liquidator

HyperLend ships an open-source reference liquidator at **github.com/hyperlendx/liquidator** (Rust):
same core design — **flash loan + DEX (LiquidSwap) atomic liquidation**. This build reuses that
sound flash+swap flow but keeps our fleet's executor/alerting/single-instance/DRY-gate framework
for consistency with the 3 live bots.

## DRY-RUN validation (live HyperEVM, no tx sent)

`python3 -u -m bot.validate` promotes the current near-edge candidates through the exact
`evaluate()` + `fire()` code the live loop uses, with `DRY_RUN` forced on. Representative live run
(block ~40.45M; see STATE.md for the full log):

```
block 40449312 | book 55 | positions 55 | real targets(HF<1) 0 | near-edge risk 5
gas est $0.0067/liq | min_profit $25.0 | path=flash | premium 4bps

--- UBTC->USDC 0x2f42d303b3… [promoted watch HF=1.1513] cover=$2,089 seized=3912251 bonus≈$418
    LiquidSwap: proceeds=2496817422, impact=0.07%, flashRepay=2090458363, net=$+406.27, profitable=True
  DRY_RUN: would liquidate … guard=DRY, NOT sent
--- USDT0->WHYPE 0x489c82a820… [promoted watch HF=1.1815] cover=$46,904 bonus≈$3,752
    LiquidSwap: proceeds=776916709349496611400, impact=0.01%, flashRepay=722737892634015562368, net=$+3,517.49, profitable=True
  DRY_RUN: would liquidate … guard=DRY, NOT sent
--- kHYPE->WHYPE 0x1d7afab94d… [promoted watch HF=1.0652] cover=$2,667,035
    LiquidSwap: impact=39.99%, net=$-2,667,797.54, profitable=False        # whale: correctly REJECTED
--- PT-kHYPE-24SEP2026->WHYPE 0xa625e8ae74… [promoted watch]
    SKIP: no LiquidSwap route (illiquid/exotic collateral)                 # exotic: correctly SKIPPED
```

The pipeline is proven end-to-end: discover → size → live-quote → net gate → **decline (DRY)**. It
accepts mid-tier tickets, **rejects** a whale that can't exit on-chain, and **skips** exotic
collateral — exactly the designed behavior.

## Operator go-live (after review) — the ONLY live steps

1. **Put a key in place**: `~/.hyperlend-bot/key` (chmod 600), fund the owner wallet with a little
   **HYPE** for gas.
2. **Deploy the contract** (one on-chain tx):
   `HL_KEYFILE=~/.hyperlend-bot/key ./bot/deploy.sh` → copy `Deployed to: 0x…`.
3. **Configure**: `cp .env.example ~/.hyperlend-bot/env` (chmod 600), set `HL_CONTRACT=0x…`, keep
   `DRY_RUN=1`.
4. **Verify DRY**: `./bot/run.sh once` (sees the book + candidates), then `./bot/run.sh validate`
   (exercises the fire path, still DRY).
5. **(Recommended) fork-test one real liquidation** end-to-end against a HyperEVM fork
   (`anvil --fork-url $HL_RPC`) to confirm the LiquidSwap Router routes output to the caller and
   the whole flash→liq→swap→repay path succeeds under the hard gas limit. See STATE.md §risks.
6. **Go live**: set `DRY_RUN=0` in `~/.hyperlend-bot/env`, install the systemd unit
   (`bot/hyperlend-executor.service`), `systemctl enable --now hyperlend-executor`.
   Kill-switch on breach → `python3 -m bot.executor reset` → restart.

## Hard safety guarantees in this repo

- `DRY_RUN=1` is the code default; the executor sends **only** when `DRY_RUN=0` **and**
  `HL_CONTRACT` is set. Every read path is a whitelisted `eth_*` method (no way to send through it).
- Gas is a **hard limit** (`HL_GAS_LIMIT`), never `eth_estimateGas` (it passes silently on
  reverting liq calls). Priority fee is 0 (non-operative on HyperEVM).
- flock single-instance; kill-switch (daily gas cap + consecutive reverts); dedup; on-chain
  `minProfit` floor as an independent second layer to the off-chain net gate.
