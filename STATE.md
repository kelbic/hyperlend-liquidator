# STATE — hyperlend-liquidator

**As of 2026-07-14. Status: 🟢 LIVE (DRY_RUN=0).**

Deployed & running:
- Contract `HyperLendLiquidator` = **`0xCBAB63AA7F8fA7F15445e85e64b2ADe4fEeC2bd6`** on HyperEVM (owner/bot key `0x46345D0c63eAa4d24002b099D4040A6BD8d673E3`).
- Gas funded via Relay (Base ETH → HYPE): started 0.0404 HYPE (~$2.6). Deploy tx `0x42602eec…`.
- Book backfilled from Pool deploy block 779363 → **25,469 borrowers** (`data/book.json`), full HF sweep each pass.
- Autonomy: flock single-instance (`/tmp/hyperlend-executor.lock` + `~/.hyperlend-bot/executor.lock`), cron watchdog (`@reboot` + every-minute) via `~/.hyperlend-bot/run.sh`, daemonized (ppid 1).
- Recipient risk closed by fork-test (see risk #1). DRY-live confirmed with the deployed contract (book 25469, near-edge 53, targets 0 in calm). First real liquidation = end-to-end calibration; revert-safe + kill-switch ($5/day gas, 3 consec reverts).
- Config `~/.hyperlend-bot/env` (DRY_RUN=0, HL_MIN_PROFIT=25, HL_MAX_IMPACT=0.05). TG alerts on fire/revert/kill-switch only (chat 265715923); ▶️ start alert fired.

**2026-07-16 review fixes (verified on-chain + fork-tested):**
- **liquidationProtocolFee modeled** — treasury takes 1000 bps (10%) of the liquidation BONUS on
  every reserve (`getLiquidationProtocolFee`, read per reserve into the book configs); we receive
  the fee-adjusted amount (1.090x at bonus 1.10 — matches all 16 historical liquidations). All
  sizing/quotes/net math now use the fee-adjusted seized figure.
- **MustNotLeaveDust + per-reserve close factor modeled** — sizing only produces revert-free
  shapes: full debt close / full collateral seize (debtToCover overshoots 1%; the Pool clamps) /
  partial leaving >= $1000(+5% margin) on both legs. Close factor mirrors the deployed lib:
  100% unless reserve debt AND collateral >= $2000 and HF > 0.95, else 50% of TOTAL debt.
- **Kill-switch alert spam fixed** (katana pattern): reverted targets get a 300s cooldown,
  kill-switch alert throttled 1/900s, start banner 1/600s, respawn with tripped guard exits
  quietly. Debt-leg sort fixed (largest value primary); runner-up collateral leg as no-route
  fallback. `~/.hyperlend-bot/deadman.sh` added (log silent >600s -> TG, 1/hour).
- **Risk #2 CLOSED** — `contracts/test/ForkLiquidation.t.sol` (3/3 vs live Pool, no archive
  needed: real position + mocked oracle price): fee-adjusted receipt = 1.090x exactly,
  debtToCover overshoot clamped to full close, dust revert at <$1000 leftover (passes >$1000),
  uint.max clamped to the 50% close factor.

**2026-07-17 — amortized hot-set detection loop + RPC transport hardening:**

Problem (measured): the loop re-swept `getUserAccountData` over the ENTIRE ~25,486-borrower book
every pass. Measured full sweep = **293.8s (~4.9 min)** (STATE claimed ~28s); spiked to ~20 min
under RPC stress, during which the log was SILENT so the 600s deadman false-fired. And it HUNG
>10 min blocked in a single `do_poll` — a single black-holing endpoint outlasted the 25s socket
timeout (reproduced live: a 400-call multicall to `rpc.hyperlend.finance` took **33.07s under a
25s socket timeout** — the socket timeout is per-recv, not total).

Redesign (mirrors the katana hot-set pattern; detection/scheduling only — economics BYTE-IDENTICAL,
the d0ad2ad/5a50e8c fee/dust/close-factor sizing and the fire path are unchanged, `process_targets`
was lifted verbatim out of `once()`):
- **Amortized hot set.** Each `loop()` iteration (`_hot_iteration`) reads `getUserAccountData` for
  (hot-set ∪ next rolling cursor chunk) in ONE bounded multicall, then (a) re-polls every hot
  member so a cross to HF<1 is caught within one iteration, (b) advances a full-book cursor by
  `HL_SWEEP_CHUNK` to refresh membership across all 25k every ~ceil(N/CHUNK) iterations, (c) fires
  any HF<1 via the unchanged `refine()`→`process_targets()` path, (d) drops fired borrowers from the
  hot set (re-seeded when the sweep next reaches them). A **compact status line is logged EVERY
  iteration** — this, not any deadman change, is the root-cause fix for the silence. Cursor + hot
  set persist to `data/hotset.json` (repo data dir, NOT `~/.hyperlend-bot`) so a restart resumes
  mid-book with a warm hot set. `analysis/monitor.py` was refactored into `sweep_accounts()` +
  `refine()`; `scan()` (CLI/`validate`/`once`) still does the full-book sweep and is unchanged.
- **HF distribution measured (book of 25,486; 7,404 with debt), debt≥$500:** HF<1.0=**0**,
  <1.15=65, <1.30=**202**, <1.50=377, <2.0=722. Chose **`HL_HOT_HF=1.30`** (202 members, a
  comfortable "few hundred", polled in ~2s; catches any position within a ~23% burst of the line at
  hot cadence). Env-tunable — widen toward 1.50 (377) before an anticipated mega-crash.
- **Transport hardened (`analysis/rpc.py`):** each attempt runs under a HARD total wall deadline
  (worker thread, independent of the per-recv socket timeout); a hung / timed-out / **rate-limited
  (`-32005`)** endpoint is benched and the next tried (fixes review M3). `multicall(retries=)` lets
  the loop pass `retries=1` (the Rpc already rotates), so worst-case per-iteration wall time is
  bounded (~a few × hard_timeout) even under a total tri-endpoint outage — never the old
  multi-minute silence. The CLI/backfill path is unchanged (hardening is opt-in via `hard_timeout`).
- **New knobs (defaults keep behaviour economically identical):** `HL_HOT_HF=1.30`,
  `HL_HOT_POLL_SEC=2`, `HL_SWEEP_CHUNK=500`, `HL_RPC_TIMEOUT=8`, `HL_RPC_HARD_TIMEOUT=10`,
  `HL_RPC_RETRIES=3`, `HL_RPC_BENCH_SEC=30`, `HL_HOTSET_FILE`.
- **Timing estimates.** Hot-set detection latency ≈ HOT_POLL_SEC + iter work ≈ **~2-5s** (a hot
  member crossing to <1 is fired the same iteration). Full-book cycle = ceil(25486/500)=51 iters ×
  (~2-4s work + 2s sleep) ≈ **~3-4 min** (residual latency for a healthy→<1 crash-entrant). DRY
  live validation: per-iteration read = ~500 (not 25k); a 10s endpoint stall was bounded to a 13.6s
  iteration and rotated, with the status line still printed (never silent). Fast-endpoint 500-call
  aggregate3 = ~1.8s.
- **Tests:** full suite green before/after — **35/35** (was 19). Added `analysis/test_rpc.py` (7:
  rotate-on-timeout, hard-deadline-on-hang, benching, rate-limit rotation, all-dead bounded return,
  backward-compat) and `bot/test_hotset.py` (9: membership add/drop/fire-removal, cursor
  wraparound/persistence, the no-gap invariant for a hot member crossing between sweeps, guard
  behaviour, fire-path-unchanged).

Honest caveats: (1) **crash-entrant residual latency** — a position healthier than HL_HOT_HF that
crashes straight under 1 is caught within one full-book cycle (~3-4 min), not at hot cadence;
mitigate by widening HL_HOT_HF before an anticipated event (a volatility-driven auto-widen is a
noted follow-up, deliberately not built — avoid over-engineering). (2) **RPC load** is comparable
to slightly lower than before (per iteration ~500-700 account reads vs a 25k sweep; the full book is
still covered every cycle), and gentler under stress (bench/rotate instead of hammering one node).
(3) Under a genuine sustained multi-endpoint outage, iterations log "iteration ERROR" and retry —
degraded but alive and never silent; raise HL_RPC_HARD_TIMEOUT if healthy endpoints legitimately run
slow. The deadman (`bot/deadman.sh`, 600s) is intentionally left as-is.

_Original build/review notes below (kept for reference)._

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

1. **LiquidSwap output recipient — ✅ CLOSED (fork-tested 2026-07-14, `contracts/test/ForkSwap.t.sol`).**
   Forked HyperEVM at latest, wrapped 1 native→WHYPE, approved the real Router `0x744489ee…2f7a`,
   called a live `/v2/route` WHYPE→USDC calldata, and asserted the output landed on the CALLER:
   **`USDC delivered to msg.sender (caller): 64961300` (64.96 USDC) → PASS.** The `/v2/route` API
   takes no recipient param (probed `to/from/receiver/recipient/…` — none bake an address); the
   router forwards final output to `msg.sender` = our contract, exactly what `executeOperation`
   needs. Revert-safe regardless (`CannotRepay`), but now positively confirmed.
1b. **LiquidSwap deadline buffer is TIGHT (~quote time, ≤~1 min).** Fork test: warp to fetch−15s →
   PASS, warp to fetch+60/+300/+900s → FAIL (deadline expired). The bot's `evaluate()`→`fire()` runs
   in the same pass (route fetched immediately before signing) and HyperEVM blocks are ~1s, so normal
   inclusion (1–2s) is well inside the buffer. RISK: in an extreme crash-burst congestion, if block
   inclusion lags past the deadline the swap reverts → `CannotRepay` → whole tx reverts (**no loss**,
   missed liq). Accepted for the cheap option; mitigation is the already-immediate quote→fire flow.
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
6. **Book warmth on cold start — RUN THE BACKFILL FIRST.** A fresh start only sees borrowers active
   in the last `HL_INCR_WINDOW` (~20k blocks ≈ 5.5h), so **older open positions are missed until they
   act again** (observed live: a fresh book had 28 borrowers and 0 near-edge, vs. the 3 real near-edge
   kHYPE positions that had borrowed >5.5h earlier). Fix is a one-time full backfill from the pinned
   **Pool deploy block 779363** (binary-searched on historical `eth_getCode`):
   `HL_DEPLOY_BLOCK=779363 HL_LOG_CHUNK=8000 python3 -u -m analysis.monitor`. The book then persists
   (`data/book.json`) and grows monotonically. getLogs is chunked (drpc HTTP-400s ranges >~8-9k; the
   chunker halves on error, so the backfill is correct, just ~thousands of requests → run once). A
   production optimization would be a HyperLend positions API/subgraph if one exists (operator to
   check); the on-chain backfill is the no-third-party fallback.

## Next steps (operator, after review)

1. `anvil --fork-url $HL_RPC` → fork-test one real liquidation (resolve risk #1/#2).
2. Optionally pin `HL_DEPLOY_BLOCK` and backfill the full borrower book once.
3. Deploy (`bot/deploy.sh`), fund HYPE gas, set `HL_CONTRACT`, verify DRY, then flip `DRY_RUN=0`.
   The operator creates the git remote later (as with wc/katana); nothing is pushed from here.

---

## 2026-07-20 — HL_RAW_TX=1 (внутренняя подпись вместо `cast send`)

**Было:** `fire()` через subprocess `cast send`, который БЛОКИРУЕТ до прихода рецепта —
**замерено канарейкой 19.07: 4.4 секунды**. В каскаде вторая цель простаивала всё это время,
на latency-FCFS цепи это прямая потеря позиции в очереди.

**Стало (8f1ea32 + f93c7bf + 54aa941):** ABI-кодирование calldata в процессе, локальная подпись
(EIP-1559, chainId 999), неблокирующий бродкаст, рецепты дозакрываются `_check_pending()` на
следующих проходах. Выстрел ≈2 сетевых round-trip вместо 4.4с блокировки.

### Что проверено ДО включения

Адверсариальная верификация дала **DEPLOY_BLOCKED** и нашла 8 дефектов, включая один, который
сделал бы бота небоеспособным ПРЯМО СЕЙЧАС: перенос ключа в `ETH_PRIVATE_KEY` не работает —
foundry 1.7.1 такой переменной НЕ ИМЕЕТ (только `ETH_KEYSTORE`/`ETH_KEYSTORE_ACCOUNT`/
`ETH_PASSWORD`). `cast` падал бы с "Error accessing local wallet", ненулевой код читается как
РЕВЕРТ ⇒ три подряд = kill-switch + `sys.exit(1)`. Откачено в f93c7bf, ключ снова в argv
(утечка в `ps` принята осознанно: молча не стреляющий ликвидатор хуже), поставлен регресс-страж
`test_cast_fallback_signs_via_argv_because_foundry_has_no_key_env`.

Все 8 закрыты: потеря tx при неоднозначном таймауте, уход `gas_usd` в минус через UTC-полночь,
вечное залипание `pending` при падающем чтении рецепта, невозможность уменьшить локальный nonce,
невидимая протухшая tx, `$0.00` при рецепте без `effectiveGasPrice`, целочисленный `status`,
`reset` стирающий in-flight.

### ЖИВАЯ КАНАРЕЙКА неоднозначной отправки (сценарий был покрыт ТОЛЬКО тестами)

Приём: искусственно занизить wall (`_rpc_write(..., budget=0.02)`) так, чтобы POST ушёл, а ответ
не успел. Это единственный способ проверить путь «таймаут при фактической доставке» не дожидаясь
реального сбоя сети годами.

Результат: `HardTimeout: hard deadline 0.02s exceeded` за **21 мс**, но транзакция **ВСЁ РАВНО
ДОШЛА** — блок 40948070, `status=0x1`, и предвычисленный `_signed_tx_hash` **СОВПАЛ** с реальным.
Nonce сдвинулся корректно (2→3).

Это доказывает главный предохранитель: старый код записал бы `send_error` и потерял tx из виду
(газ сожжён мимо дневного кэпа, цель освобождается через `DEDUP_SEC`=60с ⇒ **вторая ликвидация
того же заёмщика**). Новый заводит `pending` на локальном хэше и дозакрывает рецептом.

### Состояние

`HL_RAW_TX=1` в `~/.hyperlend-bot/env` (бэкап `hl_env.bak.*`). Тесты 83, все 5 модулей зелёные.
Задеплоен, 0 ошибок. **ОТКАТ:** `HL_RAW_TX=0` + рестарт — cast-путь остаётся рабочим фолбэком.

### Не закрыто (осознанно)

- `_rpc_write` — ЕДИНСТВЕННЫЙ эндпоинт без ротации. Расширен способ ОБРАБОТКИ таймаута, а не сам
  транспорт: лежащий write-эндпоинт = выстрелы не уходят. Естественное продолжение — `HL_WRITE_RPCS`
  (список, как в wc/midnight).
- Ключ в argv виден в `ps` локальному пользователю. Закрывать keystore'ом (`ETH_KEYSTORE` +
  `ETH_PASSWORD`) — отдельное проверенное изменение; вопрос отпадает, когда cast-путь станет
  мёртвым кодом.
- Самописный раннер ловит только `AssertionError`: тест, бросивший другое исключение, обрывает
  прогон, и сводка не печатается (код возврата ненулевой). Читать «0 failures» на оборванном
  прогоне — ловушка.

## 2026-07-27 — админ-топология пула и ЗАМЕР ДОБЫЧИ (read-only разведка)

Заход был про поиск новых опционов для флота; для hyperlend он дал два операционно
важных факта. Всё получено только через `eth_call` / `eth_getLogs` /
`eth_getTransactionByHash` — конфигурация бота не тронута.

### Кто может менять риск-параметры (и почему лид-тайма нет)

Проверялась гипотеза «плановый срез LT даёт календарное окно с известной заранее
добычей» — аналог post-maturity Midnight. **Гипотеза закрыта: очереди в пути нет.**

| роль | держатель | тип |
|---|---|---|
| DEFAULT_ADMIN + POOL_ADMIN + EMERGENCY_ADMIN | `0x1b7a7d51ee86e1d9776986aefd2675312cf0c9da` | **EOA** (код 0 байт), с блока 779363 |
| DEFAULT_ADMIN, POOL_ADMIN | `0xaaaaaaaaa810bed1eda93a18fec940857ed17879` | OZ TimelockController, `getMinDelay()`=**604800** (7 сут) |
| RISK_ADMIN | `0x1a54a8c3…`, `0x84e19195…`, `0x01f55036…` | контракты |
| ASSET_LISTING_ADMIN | `0x1a54a8c3…`, `0x84e19195…` | контракты |
| EMERGENCY_ADMIN | `0xc2a0f2c78dd7e37c82aa3a8e37fc712a3ddb7cac` | Safe (`0x6a761202` = `execTransaction`) |

- **Таймлок существует, но collateral-конфиг через него не идёт.** Все **30** событий
  `CollateralConfigurationChanged` за 16 мес (08.03.2025 → 05.06.2026) слал EOA
  `0x1b7a7d51…` через батчер `0xbbbbbbbb81e9b92918aa51e0cdfb3b53f7d72432`
  (селектор `0x134008d3`). Через очередь — ни одного.
- 34 `CallScheduled` таймлока целились в ACLManager / AddressesProvider = раздача ролей.
- **Срезов LT всего 2**, оба 24.03.2025: WHYPE 75→63, wstHYPE 65→63. Дальше — повышения.
- USR обнулён (LTV/LT/бонус `0/0/0`, 22.03.2026) Safe-мультисигом, мимо таймлока;
  ликвидаций после — **0** в 200k блоков (долг расчистили заранее).

**Следствие для эксплуатации:** LT может измениться **мгновенно и без предупреждения**
(EOA с POOL_ADMIN). Это риск-фактор, а не источник добычи: заранее увидеть срез нельзя.
Дешёвый монитор, если когда-нибудь понадобится, — не очередь таймлока, а сам факт
`CollateralConfigurationChanged` на конфигураторе `0x8cb4310dd38f6fd59388c9de225f328092bdc379`.

### Сколько на пуле реально денег (136.6 суток, 12M блоков)

Ключевой ответ на «почему бот видит 0 целей»: **их почти нет, и это не наша проблема
детекта.**

| метрика | значение |
|---|---|
| ликвидаций | 667 |
| суммарный бонус | ~**$21,090** = **$154/день на ВСЕХ ликвидаторов вместе** |
| медиана бонуса | **$0.03** |
| p90 / p95 / p99 | $28 / $100 / $568 |
| с бонусом ≥ $25 | **71 из 667** |
| крупнейший эпизод | **$9,149** (wstHYPE, блок 35984166, покрыто $67.8k) = **43% всего за 4.5 мес** |
| последние 13 суток | 34 ликвидации, бонус **$4.13**, максимум $1.73 |
| топ-ликвидаторы | `0x2f18fc90…` 161, `0xdd8692bc…` 92, `0x7a563598…` 65, `0x6f7d45e5…` 64 |

Динамика падает: 1851 ликвидация за блоки 16.8–24.8M → 442 за 32.8–40.8M → 14 за
последние 839k.

*Оговорка по методу:* USD считались по **текущим** ценам оракула для исторических
событий — абсолютные суммы это оценка, порядок величины устойчив, медиана от цены
практически не зависит.

**Вывод:** $90M нотионала HYPE-лупов ≠ $90M опциона. Платит только редкий крупный
единичный эпизод, поэтому правильная позиция для этого бота — **дежурство с нулевой
стоимостью в тишине**, а не оптимизация потока. Порог `HL_MIN_PROFIT` менять под
«больше выстрелов» бессмысленно: 90% событий лежат ниже $28 суммарного бонуса ДО газа.

## 2026-07-30 — лестница чанков: $10.4k, которые бот не видел

**Премисса.** Флотовый обсчёт «опционов на события» показал: у hyperlend самая большая книга
(25,596 заёмщиков, 114 позиций у края HF<1.20 на $24.5M долга), но **0 выстрелов за 359,836
проходов**. Разбор двух крупнейших целей: WHYPE→USDC на $734k и $342k, чистый бонус $32,890 и
$15,322 — `evaluate()` котировал ПОЛНЫЙ размер, получал impact 45–53% и корректно отказывался.
Бот не ошибался, он молчал: дыра не оставляла следа в логе.

**Замер лестницей вручную** (та же цель, доли `debt_pulled`):

| доля | cover | net | impact |
|---|---|---|---|
| 1/1 | $365,446 | −$317,927 | 51.2% |
| 7/20 | $127,267 | −$79,653 | 29.7% |
| **1/4** | **$90,905** | **+$7,663** | **0.23%** |
| 3/50 | $21,817 | +$1,889 | 0.20% |

Маршрутизатор переключает маршрут **скачком** (29.7% → 0.23% между соседними ступенями), а не
плавно. Поэтому сокращать обход экстраполяцией impact нельзя — линейная оценка проскочила бы
лучшую ступень.

**Сделано** (порт katana-паттерна, работающего с 20.07):
- `analysis/aave.py`: у `size_liquidation` появился `max_cover_wei`. Ограничение ставится на
  `max_liq_wei` — **одной строкой**, чтобы чанк обязательно прошёл ветку (c) с правилом
  `MustNotLeaveDust` (обе остаточные ноги ≥ $1000). Масштабировать готовый результат
  арифметикой нельзя: такой чанк ревертнёт на Pool.
- `analysis/monitor.py`: строка цели несёт сырые входы сайзинга (`debt_wei`, `coll_wei`,
  `total_debt_base`, `hf_1e18`) — лестница пересчитывает размер ИХ ЖЕ боевой функцией.
- `bot/executor.py`: `evaluate()` стал лестницей поверх `_evaluate_one()`; статические ступени,
  затем спуск, ограниченный экономикой (порог + газ / полный бонус). ЛЕНИВО: выход на первом
  прибыльном чанке, поэтому обычная цель стоит ровно одну котировку, как раньше.
- **Бюджет обхода пропорционален призу.** Первая версия с фиксированными 6с провалилась в бою:
  одна котировка LiquidSwap стоит 1.7–3.5с, обход обрывался на третьей ступени и цель с бонусом
  $32.9k возвращалась «неприбыльной» — та же дыра, только глубже. Теперь бюджет =
  `бонус × 0.001с`, пол 6с, потолок 40с (чтобы каскад не встал на одной цели).

**Результат на живой книге:** исполнимых целей 5 → **6**, сумма **$14,741 → $25,104**.
Крупнейшая, ранее недостижимая, даёт **+$10,727 при impact 0.32%**.

Тесты: **99 зелёных** (+9: cap с dust-правилом, ленивость на полном размере, находка чанка
при неподъёмном полном размере, откат к полному размеру в логе, деградация на старом кэше
книги без сырых входов, NoRouteError на крупном размере, экономическая граница спуска,
масштабирование бюджета).

## 2026-07-30 — почему 0 выстрелов: премисса проверена чейном

Скан 30 суток по `LiquidationCall` пула (2.6M блоков, окна по 1000 — лимит RPC): **63 события**.
Рынок НЕ пуст. Но дальше три слоя разбора меняют вывод:

**1. Дискавери исправна.** Жертв уникальных 55 — и **все 55 были в нашей книге** (25,596
заёмщиков). Мы их видели.

**2. Экономика: 58 из 63 событий — пыль, и пропускать их правильно.** Суммарно погашено долга
$17,447, **медиана события $0.65**, 57 событий меньше $100. Порог `HL_MIN_PROFIT=$25` отсекает
их по назначению.

| размер события | штук |
|---|---|
| <$100 | 57 |
| $100–1k | 2 |
| $1k–10k | 3 |
| $10k–100k | 1 |

**3. Реальный промах — 5 событий на $1,527 бонуса** (не $48k, как можно было подумать по
книге). Крупнейшее: $12,361 UBTC→USDC, бонус ≈$1,113. Проверил их выход тем же LiquidSwap
задним числом: **у всех пяти маршрут есть, impact 0.04–0.27%**. Значит помешала не экономика
и не ликвидность, а **каденс обнаружения**: полный цикл амортизированного sweep по 25,596
заёмщикам занимает ~100с, hot-set (220) держит только тех, кто уже у края. Позиция, влетевшая
под HF<1 извне hot-set, видна нам с опозданием до ~100с — при 8 активных конкурентах этого
достаточно, чтобы опоздать.

Поле: 8 ликвидаторов, лидер `0xa8a1708c` (20 тейков), затем `0xd52b5909` (12) и `0x98ff6022` (9).
События кучные: 17 дней из 30 с событиями, максимум 14 за день.

**Вывод и что дальше.** Стоячая книга опционов ($25.1k исполнимых после лестницы чанков) и
поток ($1.5k/мес реального бонуса) — разные вещи, и вторая упирается в каденс. Следующий шаг
по hyperlend — **разобрать те 5 событий поимённо**: где была позиция за минуту до пересечения,
входила ли в hot-set, и что двинуло HF. Если они прыгали в HF<1 из зоны 1.05+, лечится не
ускорением sweep, а событийным триггером (движение оракула по их коллатералу), как predict-слой
у katana.

## 2026-07-30 — каденс: горячих опрашивали медленнее, чем идёт чейн

Разбор пяти упущенных событий (см. выше) показал, что выход у всех был, значит дело в скорости.
Замер итерации: читаем `hot(~220) ∪ chunk(500)` = ~720 аккаунтов за **1.1–1.4с**, а блок
HyperEVM идёт **~1с**. То есть горячая позиция перечитывалась реже, чем появлялся новый блок —
пересечение HF<1 могло прожить целый блок незамеченным.

Попытка восстановить траектории HF жертв до ликвидации **не удалась**: публичный узел не
архивный, историчные `getUserAccountData` возвращают нехронологичную кашу (∞ между двумя
1.004). Что установлено надёжно: все пятеро в течение часа до тейка стояли на HF 1.00–1.03,
то есть глубоко внутри потолка hot-set (`HOT_HF=1.30`) — они были видимы, мы просто медленнее.

**Правка:** курсор полной книги катится не каждую итерацию, а раз в `HL_SWEEP_EVERY=3`.
Две итерации из трёх читают только hot (~220 аккаунтов, ~0.35с), горячие опрашиваются примерно
вдвое чаще. Плата — полный цикл книги растягивается ~56с → ~90с; это дёшево при потолке 1.30:
чтобы выпасть из наблюдения, позиции надо рухнуть с 1.30 до 1.0 внутри одного цикла.
`HL_SWEEP_EVERY=1` возвращает прежнее поведение ровно.

Тесты: **104 зелёных** (+6: план тиков, идентичность при N=1, hot-only не двигает курсор и всё
равно ловит пересечение, членство переживает итерацию без чтения). Существующий тест на курсор
кодировал старый контракт — переписан так, чтобы явно гонять sweep-итерацию, и дополнен парным.

## 2026-08-04 — обвалы: вся история пула, и почему 30-суточный скан её не показал

Вопрос владельца («а обвалы там бывают вообще?») проверен замером всей истории, а не 30 суток.

**Скан углубился в 13 раз, потому что прошлый лимит окна был ложным.** В STATE от 30.07 стоит
«окна по 1000 — лимит RPC». Перемерено: `rpc.hyperlend.finance` отдаёт **500k-блочные окна за
2с**, и результат бит-в-бит сходится с суммой пяти окон по 100k (2M ломается 500-й). Вся история
пула — **85 запросов, 202 секунды** вместо тысяч. Прежний вывод строился на 2% доступных данных.

**Итог: 4078 ликвидаций, 08.05.2025 — 02.08.2026, $10,239,252 погашенного долга**, оценочный
бонус после протокольной доли (10%) **~$958,608**. Цены — дневные клоузы Hyperliquid; 5 токенов
вне реестра (USOL/USR/USDHL/beHYPE/PT-kHYPE-19MAR2026) доопределены on-chain, покрытие 100%.

**Деньги живут ТОЛЬКО в обвалах.** Дней с провалом HYPE ≥10% внутри суток — 17 из 400.

| | обвальные дни (17) | обычные дни с событиями (214) |
|---|---|---|
| средний долг за день | **$322,901** | $20,703 |
| суммарный бонус | **~$522,902** | ~$435,706 |

Один день **10.10.2025** (HYPE −27.7%) = **$4.53M долга, ~$410k бонуса, 146 событий**, из них
113 событий и $4.50M — **внутри одного часа 21:00–22:00 UTC**. Это 43% бонуса за все 15 месяцев.
Поле в тот день: 10 ликвидаторов, `0x2638a8d1ac` забрал ~$255k.

Медиана события за всю историю **$4.77**; 2248 событий из 4078 меньше $10. Выше нашего пола
($25 бонуса) — **626 событий на ~$948,675**, то есть 99% денег в 15% событий.

**Бот ни одного обвала не видел.** Живёт с 14.07.2026, последний обвал — 09.06.2026. Тишина 56
дней при медианном промежутке между обвалами **11 дней** (max в истории — 101 день). Ноль
выстрелов за 3 недели объясняется не поломкой, а тем, что окно ещё не открывалось.

**Газ в час раздачи — по чекам победителей, не по оценке.** baseFee 10.10 в 21:00–22:00: медиана
**34 gwei**, пик **3,689 gwei** (обычная медиана — 0.100 gwei). Реальные чеки 12 крупнейших:
gasUsed 0.68–1.38M, эффективная цена **452–51,001 gwei**, фактический расход **$17–$1,290** за
выстрел, требуемый узлом пол `GAS_LIMIT×maxFee` = **1.6–10.6 HYPE**.
При балансе 0.0402 HYPE бот был бы отвергнут узлом на **каждом** из них; в 57% проб того часа
пола не хватало вообще. Алерт 03.08 (пик 55 gwei) — это была рябь, а не шторм.

**`HL_MAX_DAILY_GAS_USD=5` при этом жёсткий стоп** (`executor.py:205`, `gas_usd >= cap` →
не стреляем). Один выстрел в час раздачи стоит $17–$1,290 ⇒ лимит выбирается ПЕРВЫМ выстрелом
и глушит остальные 112 событий того же часа.

**Отдельная находка: премисса «чаевые на HyperEVM неоперативны» замером НЕ подтверждается.**
`config.py:49` утверждает latency-FCFS без аукциона приоритета, `PRIORITY_GWEI=0`. Измерены
чаевые (`effectiveGasPrice − baseFee`) у победителей:

| выборка | медиана чаевых | p90 | tx с чаевыми >1 gwei | наш `maxFee=base*2+0` покрыл бы |
|---|---|---|---|---|
| 10.10.2025, час раздачи (60 tx) | 754 gwei | 13,074 | 60/60 | 35/60 |
| 22.11.2025, второй день (40 tx) | 518 gwei | 57,937 | 39/40 | **2/40** |
| 24.05.2026, обычный день (40 tx) | 64 gwei | 434 | 39/40 | **1/40** |

Платят все и всегда, и в стресс escalate'ят на два порядка. Это не доказывает, что чаевые
НУЖНЫ (выиграли бы они и без них — непроверяемо), но «чистое пожертвование» держаться больше не
может: в спорном блоке наш конверт ниже цены победителя в 39 случаях из 40.

**И структурный риск размера: `GAS_LIMIT=2500000` против малых блоков HyperEVM.** Замер: сейчас
малые блоки идут с `gasLimit=3,000,000` (39 из 40 блоков), большие 30M — примерно 1 из 40.
**В октябре 2025 малые блоки были 2,000,000** — то есть наша транзакция на 2.5M в них не влезала
бы вовсе, а 31 из 33 ликвидаций того часа сели именно в малые блоки. Сегодня мы проходим с
запасом 0.5M, но это один параметр сети от полного отключения, и отказ был бы молчаливым.

**Что это меняет по капиталу (решение владельца).** Пополнение до 1 HYPE, названное утром,
измерением опровергнуто как достаточное: пол в час раздачи был 1.6–10.6 HYPE.
- **~3 HYPE ($163)** — покрывает базу до ~600 gwei: обвальный день да, пиковые минуты нет;
- **~10 HYPE ($544)** — покрывает p90 часа раздачи и 3–4 фактических выстрела (расход 1.7–2.8
  HYPE каждый).
Дешёвая альтернатива части этих денег: снизить `GAS_LIMIT` — пол линеен по нему, а победители
тратили 0.68–1.38M. 1.8M дало бы −28% к полу; цена ошибки — out-of-gas и потерянный выстрел.

Ничего из перечисленного не менялось: `.env` боевого бота не тронут, изменения ждут «го».

**Проверено попутно и дефектом не является:** `minHF 0.0116` при `tgt 0` в логе — цели
фильтруются по `MIN_DEBT_USD=$500`, а `minHF` считается по всем прочитанным, включая пыль;
`last_heartbeat` не двигается 4.6 суток, потому что `HEARTBEAT_SEC=0` (выключен намеренно,
живость держит деадман по строке цикла).

## 2026-08-04 — вскрытие блоков часа раздачи: чаевые ПОКУПАЮТ место, оракул без push-транзакций

Разбор 8 блоков 10.10.2025 21:00-22:00 по полным чекам всех транзакций (dissect2, tmp джоба).

**1. Малые блоки HyperEVM упорядочены по цене газа, не FCFS.** Внутри блока tx стоят по tip
убыв.; единственные нарушения — нонс-цепочки одного отправителя (0x339d413c: 5 tx подряд,
0x03ac0b1b: 3 tx). Победители платили tip 80–1,225 gwei при base 324–1,833. Премисса
`config.py:49` («latency-FCFS, no priority auction», PRIORITY_GWEI=0) опровергнута для
контестед-блоков: с tip=0 мы стоим ПОСЛЕДНИМИ в любом спорном блоке, даже придя первыми.
Рядом с победителями сидят реверты конкурентов с tip 373–15,642 gwei (гонка реальна, платят
и проигравшие). Лидер часа — EOA 0x00003f87 (vanity-адрес с нулями = профи) → контракт
0x2638a8d1, взял 6 из 8 вскрытых блоков с УМЕРЕННЫМИ tip 163–1,225 против чужих 13k–224k:
он выигрывает не ценой, а чем-то ещё (нода/латентность/точность) — переплачивать не требуется.

**2. Оракул HyperLend не публикует апдейтов вовсе.** Все 6 источников (WHYPE/UBTC/UETH/kHYPE/
wstHYPE/USDT0) — 0 событий за 28ч (100k блоков), и в блоках раздачи оракульных tx нет. Цена
вычисляется на чтении (источники ~2.2-4KB кода, прямых PUSH20-ссылок на precompile 0x800+ в
самом контракте нет — вероятно, через промежуточный reader к HyperCore). Следствия:
- фронтранить НЕЧЕГО: HF<1 материализуется В блоке N молча, tx, исполнившаяся в N, уже видит её;
- спекулятивная tx, посланная ДО движения и севшая в N с высоким tip, исполняется первой — это
  Base-урок предочереди, усиленный tip-ordering'ом;
- прогноз цены уникально дешёв: источник цены = сама биржа Hyperliquid ⇒ её вебсокет — это
  ВХОД оракула в реальном времени, а не прокси (как Binance→Chainlink у katana).

**3. Наша трасса сегодня:** RTT до rpc.hyperliquid.xyz/evm с этой машины total ~275мс
(connect 14мс — TLS-руки съедают остальное; keep-alive обязателен), горячие читаются раз в
0.35–2.7с при блоке 1с, tip=0, одиночный write-RPC (сам ловил -32005 на чтении днём).

Выводы по доработкам — в ответе владельцу 04.08 (гонка = ярус-0 газ/капы + чаевые/залп/prewarm
к ближайшему обвалу, predict-слой от вебсокета HL следом). Конфиг не менялся, ждёт «го».

## 2026-08-04 — ЯРУС 0–1 ГОНКИ В БОЮ (GO владельца + ревью принято)

Ревью подтвердило план без изменений постановки; четыре уточнения встроены:
вилка predict (огонь по подтверждению vs по прогнозу) НЕ решается сейчас — её решит
shadow-статистика; прекомпайлы проверены ДО реверса формулы оракула; общие компоненты
с Midnight-окном 27.08 строятся один раз; ярус 0 — сразу.

**Разведка оракула (час вместо дня, как и предсказывало ревью — но исход третий).**
Прекомпайлов НЕТ: источники — фасады с вшитой константой `0x959a0351…` (0 байт кода =
подписант RedStone-пакетов, не вызов). `latestRoundData.updatedAt` разный у фидов
(WHYPE 233с, UBTC 40мин, UETH 75мин) ⇒ **push-модель**: релейер шлёт пакеты ~1.3KB /
~230k газа в адаптер `0xcae5…` (пойман с поличным в блоке 42262269, tip 0.30), запись в
storage БЕЗ событий — потому getLogs их и не видел. Следствия для гонки:
- триггер гонки = tx релейера; фронтранить нечего, но и «цена меняется молча» неверно;
- **сэндвич**: сортировка по tip означает, что tx с tip ВЫШЕ релейерского исполняется
  ДО обновления цены и ревертит — вот механика ревертов 10.10 с tip 13-15k gwei и
  побед лидера с tip 163-1225. Наш tip обязан быть НИЖЕ релейерского в спорном блоке;
- прогноз цены = вебсокет Hyperliquid (вход RedStone) — по-прежнему главный predict-рычаг.

**Код яруса 1 (тесты 128/128, было 104):**
- keep-alive HTTP-пул (`analysis/rpc.py http_post_json`): взял/вернул с эксклюзивным
  владением, ретрай-раз на лежалом соединении, тестовый сим через `_urlopen` сохранён;
- динамический tip (`_tip_wei`): доля приза `TIP_PRIZE_FRAC=5%`, кламп [5,1000] gwei,
  **потолок по балансу** (узел требует конверт целиком — чаевые не смеют превратить
  проходной выстрел в insufficient funds; на нищем кошельке tip падает до 0, не выстрел);
- параллельный залп (`_broadcast_raw`): все BROADCAST_RPCS одновременно, первый ack;
  семантика исходов бит-в-бит (any-ok / единогласный вердикт=SendUndelivered /
  иначе SendAmbiguous), «already known» = доставка; одиночный путь = прежний сим;
- nonce prewarm: кэш chain-вида раз в 15с, WC-инварианты (send_ts пишет только
  _nonce_after_send) не тронуты — тест это фиксирует;
- кэш baseFee (2.5с ≈ 2 блока, кормится из gas_cost_usd итерации) — путь выстрела
  без двух блокирующих RPC;
- shadow-телеметрия (`bot/shadow.py`): каждая чужая ликвидация → анатомия блока
  (все tx: idx/tip/gas/status, гипотеза push-tx), цены из НАШЕГО оракула, наша
  видимость жертвы (in_book/in_hot), floor/guard; тяжесть в daemon-потоке, чекпойнт,
  инбокс агента при бонусе ≥ нашего пола (HIL-доктрина 03.08). Это датасет для
  tip-перцентилей и решения вилки predict.

**Замер трассы после keep-alive перевернул географию:** тёплые вызовы drpc = 22-35мс
(евро-POP), hyperlend/official = 205-235мс (ориджины за CDN, DNS-гео обманчиво:
CloudFront Frankfurt/Cloudflare Toronto — это края, не серверы). READ_RPCS реордер:
drpc первым. Ответ на «нужен ли гео-VPS как у Base»: для ЧТЕНИЙ уже нет (22мс имеем),
для ЗАЛПА — вопрос открыт до shadow-данных (ack ≠ время до секвенсера; Hyperliquid
валидаторы в Токио, но параллельный залп кроет разницу до замера).

**Ярус 0 применён**: HL_MAX_DAILY_GAS_USD=50 (env, бэкап в jobs tmp). Баланс — за
владельцем (ищет средства; 3 HYPE = обвальный день, 10 HYPE = p90 часа раздачи).
Реестр откатов: docs/ROLLBACKS.md (6 флагов, каждый — прежнее поведение бит-в-бит).

## 2026-08-04 (позже) — ПОПРАВКА К СОБСТВЕННЫМ ВЫВОДАМ: сэндвича нет, гонка решается не в блоке апдейта

Разведка полосы tip'а (ревью: «первый продукт shadow — ряд релейерских tip'ов») сделана
РЕТРОАКТИВНО за час, потому что ряд лежит в истории. По дороге опровергнуты два моих
собственных утверждения из ebd50e9/547846c.

**Поправка 1: адаптер и «оракул без событий».** Настоящие адаптеры —
`0xe4ae8874…` (feedId HYPE/BTC/USDT) и `0x24c89643…` (ETH), связь доказана вызовом
`getPriceFeedAdapter()` у фасадов. `0xcae5…` из блока 42262269 к оракулу отношения не имеет —
я принял совпадение по времени за причину. **События ЕСТЬ**: адаптеры эмитят ValueUpdate
(`0xf36866d9…`), 528 апдейтов за 22ч штиля, медианный интервал ~4 мин. Вчерашнее «оракул
не публикует апдейтов вовсе» относилось к ФАСАДАМ (они и правда молчат) и было ошибочно
обобщено на весь оракул. Практический выигрыш: ряд релейера строится дешёвым getLogs.

**Ряд релейерских tip'ов (замер):**

| окно | p10 | медиана | p90 | max | доля idx==0 |
|---|---|---|---|---|---|
| штиль (22ч, 528 tx) | 0.0 | 0.2 | 5.0 | 130 | 44% |
| час раздачи 10.10 (160 tx) | 23.9 | 316 | 1250 | 5075 | 72% |

Релейеров двое: `0xe08496b4…` и `0x2327c3cd…` (в шторм подключались ещё два).

**Поправка 2 — и она отменяет дизайн-пункт ревью: СЭНДВИЧА В ДАННЫХ НЕТ.**
Реверты с tip 13–15k gwei, на которых я построил «сэндвич-механику», **не были
ликвидациями вообще**: их `to` — WHYPE (`0x5555…`) и `0xbbd472…`, то есть чужие
арбитражные транзакции. Ошибка методики: победители шлют tx в СВОИ контракты
(`0x2638a8d1…`, `0xd7a31895…`), а не в пул, поэтому и фильтр `to==POOL` даёт мусор —
ликвидации опознаются ТОЛЬКО по хэшам из событий Liquidate.

Пересчёт по хэшам, час раздачи (113 ликвидаций против 160 апдейтов цены):
- **в одном блоке с апдейтом: 3 из 113 (3%)**;
- лаг «блок ликвидации − ближайший предшествующий апдейт»: 0 блоков — 3 шт, 1 — 2 шт,
  2 — 11 шт, **3+ — 97 шт; медиана 17 блоков (~17с), p90 61**.

**Следствия, меняющие план:**
1. Полоса `tip < релейер − маржа` не нужна: в 97% случаев релейера в блоке нет вовсе.
   Ограничение сверху остаётся ЧИСТО ЭКОНОМИЧЕСКИМ (доля приза), как и было в коде.
2. **Вилка (а)/(б) закрыта данными, и раньше срока: побеждает (а) — огонь по подтверждению.**
   Спекуляция в блоке апдейта — это 3% поля, а не эдж; лидер `0x00003f87` берёт своё
   через ~17с после апдейта, то есть по подтверждению и с умеренным tip.
3. **Настоящий рычаг — каденс обнаружения в окне 2–60 блоков после апдейта**, ровно то,
   что 30.07 диагностировано как «горячих опрашивали медленнее, чем идёт чейн». Медиана
   поля 17с даёт нам реальный шанс: наш горячий опрос 0.35–2.7с внутри этого окна.
4. Predict-слой от вебсокета переоценивается: он покупает секунды ПЕРЕД апдейтом, а поле
   тратит 17 секунд ПОСЛЕ. Приоритет ниже, чем событийный триггер по ValueUpdate —
   дешёвый getLogs даёт точный момент, когда пересчитывать HF всей горячей книги.

**Оговорка замера:** ряд построен по фидам HYPE/BTC/USDT/ETH; у kHYPE/wstHYPE фасады
без `getPriceFeedAdapter()` (обёртки над курсом), их собственные апдейты в ряд не вошли —
для лага это несущественно (драйвер цены тот же HYPE), но для событийного триггера
их путь надо будет разобрать отдельно.

Код яруса 1 правок не требует: кап 1000 gwei и доля приза остаются в силе по п.1.

## 2026-08-04 — КАДЕНС ПОСЛЕ АПДЕЙТА В БОЮ (следующий пункт плана после поправки 4237b15)

Раз поле берёт добычу через ~17с ПОСЛЕ ValueUpdate, рычаг — скорость обнаружения в этом
окне. Два механизма (тесты 136/136, было 128):

**1. Событийный триггер (`poll_oracle_updates`)**: каждая итерация — один дешёвый getLogs
по двум адаптерам (`0xe4ae8874…` HYPE/BTC/USDT+kHYPE_FUNDAMENTAL, `0x24c8964338…` ETH;
topic `0xf36866d9…`, ~25мс на drpc keep-alive). Свежий апдейт ⇒ итерация hot-only (чанк
курсора подождёт) и сон 0.05с вместо 1с — транзиты HF доигрываются 1-3 блока. Курсор
двигают только события; отказ чтения = 0 (триггер — ускоритель, не точка отказа).
Худший путь обнаружения после апдейта: ~0.5-0.7с (hot-читка) против прежних до ~2.6с
(sweep-итерация) + 1с сна.

**2. Pre-arm кромки (`prearm_tick`/`_prearm_get`)**: позиции 1.0 ≤ HF < 1.02 с долгом
≥$500 котируются ФОНОМ (один поток единовременно, топ-2 по долгу, рефреш 20с): refine +
evaluate на размере, бритом до 97% (`PREARM_SHAVE`) — дрейф позиции за TTL 45с не должен
опустить фактический seize ниже amountIn свопа. При пересечении HF<1 process_targets
берёт готовую пару (t, ev) из кэша: LiquidSwap-квота (1.7-3.5с, замер 30.07) уходит с
горячего пути; экономику на живом размере страхует on-chain min_profit_wei (устаревшая
котировка ревертит, а не теряет деньги). Кэш применяется только при HF ≥ 0.95 (тот же
close-factor режим, что при котировке); использованный арм умирает с выстрелом.

Задержка выстрела после апдейта теперь: обнаружение ~0.5с + fresh_hf ~50мс + арм-хит 0
+ подпись/залп ~100мс ≈ **до ~1с против медианы поля 17с**. Откаты в docs/ROLLBACKS.md.

## 2026-08-04 — ПЕРВЫЕ ЖИВЫЕ ДАННЫЕ: pre-arm был тихим no-op, телеметрия слепа к USDHL,
## и найден класс «одинаковый актив» на ~$44k

**1. PRE-ARM НЕ АРМИЛ НИ РАЗУ (мой дефект, тесты 138/138 после починки).**
`refine()` кладёт HF>=1 в `risk`, а HF<1 — в `targets`; я читал `targets`, то есть у
кромки всегда получал пустой список. Отказ был НЕОТЛИЧИМ от «нечего армить», потому что
код печатал только успех — ровно [[dead-watchdog-worse-than-none]] в моём исполнении.
Починка: читаем `risk`; сайзим позицию в состоянии «только что пересекла» (hf=0.999 —
у здоровой позиции cover считается от её текущего HF, а нужен размер на пересечении);
итог прохода печатается ВСЕГДА (`prearm: кромка N -> строк M, армлено K, мимо: …`).
Регресс-тест фиксирует и источник строк, и гипотетический HF.

**2. Первые две shadow-записи (первые чужие ликвидации с момента запуска) пришли с
`debt_usd=None`:** актив USDHL отсутствовал в реестре. По факту события были пылью
($0.70 и $1.90, победитель `0x6f7d45e5` с tip 0.01 gwei — конкуренции нет), то есть
денег мимо не прошло, но дыра реальна: крупное событие в USDHL телеметрия оценить бы не
смогла и алерт в инбокс не отправила. Реестр дополнен пятью активами из полного скана
(USDHL/USR/USOL/beHYPE/PT-kHYPE-19MAR2026), символы и decimals прочитаны on-chain.
Обе жертвы были `in_book=True, in_hot=False` — то есть каденс их не держал; на пыли это
безразлично, но метрика «видели ли мы жертву» теперь работает и копится.

**3. НАЙДЕН СТРУКТУРНО НЕДОСТУПНЫЙ КЛАСС: coll == debt.** Живая позиция `0xec23c052…`
WHYPE->WHYPE отвергается как «no LiquidSwap route» — маршрута в тот же актив нет и быть
не может, а контракт свопает БЕЗУСЛОВНО (`swapTarget.call(swapCallData)`, revert при
неудаче), поэтому пустой своп не пройдёт: `_forceApprove` на нулевой адрес ревертит.
Замер класса по всей истории пула: **55 событий, долг $338,886, бонус ~$44,088; выше
нашего пола $25 — 9 событий на ~$43,976 (4.6% всей выручки пула)**. Крупнейшее одиночное
в истории — UETH->UETH $201,742 (15.07.2025), ушло конкуренту.
Лечение — правка контракта (`if (collateralAsset != debtAsset) { swap }`) + редеплой,
то есть решение владельца по капиталу/газу. В pre-arm такие позиции пока пропускаются с
явной пометкой в логе, а не молча.

# 2026-08-04 (вечер): разбор гонки — «наши шансы и не сольём ли $500»

## Главное открытие: тихое поле = ОДИН оператор с атомарным self-push

Все пять топ-«ликвидаторов» последних 30 дней (`0xae86edb5`, `0xab7b11c6`, `0x98ff6022`,
`0xd52b5909`, `0x22206351`) управляются ОДНИМ EOA `0x84d5e280…` через входной контракт
`0x7a0ea1f770…` — это 99.8% тихой месячной выручки ($5.9k). Его техника: **сам пушит
подписанный RedStone-payload в адаптер и ликвидирует В ТОЙ ЖЕ транзакции** (calldata
1.7–3.3KB, один ValueUpdate в логах tx, gas 0.8–1.0M). Пуш пермишенлесс — EOA не из
релейеров, эмпирически принят адаптером `0xe4ae8874…`. Итог: lag=0 у ВСЕХ его побед
(10/10 проверенных свежих событий ≥$5; плотность апдейтов 0.3–2.2% блоков — не артефакт).
Где пуш делает релейер, оператор садится в ТОТ ЖЕ блок сразу после пуша с tip чуть ниже
релейерского (blk 41956546: релейер tip 10.98 idx1, он 5.01 idx3) — мемпул он тоже смотрит.
Следствие: наш реактивный контур (реакция на подтверждённый ValueUpdate) проигрывает ему
СТРУКТУРНО — наш триггер срабатывает в тот момент, когда добыча уже взята тем же блоком.

## Каскадный час (10.10.2025 21:00 UTC, $407k/113 ликвидаций) — анатомия по лагу

Взвешено по деньгам, лаг победителя от ближайшего предшествующего пуша:
lag=0 — 2% ($6.2k); 1–4 блока — 12% ($49k); 5–17 — 34% ($139k); 18–60 — 51% ($206k).
**85% денег часа взято с лагом ≥5 блоков** — поле в каскад медленное. Победителей 9,
топ взял 63% (46 выстрелов), второй эшелон (`0xdd8692bc`, lag~17с) — $42k мелочью.
Сегодняшний self-push-оператор в том каскаде ОТСУТСТВОВАЛ — поле эволюционировало.
Tips в каскад: 63/95 крупных побед взяты с tip ≤1 gwei ($80k приза), контестед только
16 побед с tip>50 ($59k). BaseFee в каскад: med 375 gwei, p90 1073, max 2914.

## Ответ «не сольём ли $500»: $500 — это конверт, не ставка

Узел требует balance ≥ GAS_LIMIT×maxFee (полный конверт): тихо ≈0.63 HYPE ($28) с нашим
авто-tip, в каскад p90 ≈6.6 HYPE ($290) — 10 HYPE = один tx в полёте на пике. Худший
расход жёстко ограничен: kill-switch HL_MAX_DAILY_GAS_USD=50/день; реалистичный тихий
месяц ≈ 7 проигранных гонок × $2–9 реверта ≈ $15–60/мес. Слить всё нельзя короче утечки
ключа; слить $50 за день — расчётный максимум.

## ДЕФЕКТ КАПА: успех считается в суточный лимит (executor.py:1017)

Кап учитывает ВЕСЬ газ, включая успешные выстрелы. Успешный выстрел в каскад ≈ $75–120
газа при базе 375+ gwei → ПЕРВЫЙ ЖЕ УСПЕХ пробивает $50 и глушит бота на остаток часа
жатвы. Кап должен ограничивать УБЫТОК (реверты/gone), а не оборот: успешный выстрел
окупает свой газ из приза в той же tx. Нужна правка семантики (успех возвращает свой
газ в бюджет) или каскад-бакет по образцу Base — решение владельца, помечено к «го».

## Вывод по шансам

Тихо, как есть: ~0% против self-push-оператора (EV слегка отрицательный, ограничен капом).
Каскад, как есть (с газом и починенным капом): наш лаг 2–7 блоков против поля, где 85%
денег бралось с лагом ≥5, — реалистичная доля $40–75k на час октябрьского масштаба, даже
если оператор возьмёт сливки (63% в терминах октября).
Рычаг №1 = ТОТ ЖЕ self-push: RedStone-payload публичен, пуш пермишенлесс, контракту нужен
параметр `bytes oracleCalldata` (исполнить до flashloan) — та же правка/редеплой, что и
починка coll==debt. Плюс вотчер офчейн-стрима RedStone. После этого гонка симметрична
и решается латентностью детекта. Shadow теперь копит паттерн оператора автоматически.

## 2026-08-04 (день): v2 В БОЮ
- Кошелёк пополнен владельцем до **10.04 HYPE** (~$546) — полный конверт p90-шторма + параллельный залп.
- **Контракт v2 задеплоен и боевой**: `0x5C20F458a14849673ec1aec407f6ed22f82d07Af` (tx 0xdfe0bb03…, газ 0.0022 HYPE).
  Проверено на адресе: код 4767 байт, owner=кошелёк, POOL=0x00A89d7a…. v1 (0xCBAB63AA…) жив как откат.
- **Класс coll==debt открыт и в боте** (7f9bd91): evaluate без свопа (swapTarget=0, выручка=seized),
  prearm-фильтр «одинаковый актив» снят, гейт HL_SAME_ASSET=1 (при откате контракта на v1 — обязательно =0).
  Первый кандидат класса уже на кромке: 0xec23c052… (его prearm раньше отбрасывал).
- Дальше (по мере): вирирование liquidateWithPush в путь выстрела (analysis/redstone.py готов:
  latest_price + build_push_calldata), затем RedStone-вотчер как триггер. kHYPE/wstHYPE FUNDAMENTAL-фиды
  без getPriceFeedAdapter — трассировать отдельно.

## 2026-08-04 (день, продолжение): SPEC-FIRE ЗАДЕПЛОЕН — паритет техники с оператором поля

«Идем дальше в следующий крупный рычаг» (kelbic) = вирирование `liquidateWithPush` в путь
выстрела. Смысл: единственный оператор тихого поля берёт 99.8% выручки атомарным
self-push'ем (lag=0), наш реактивный контур проигрывает ему структурно. Теперь у нас тот же
приём: армленная цель у кромки (1.0 ≤ HF < 1.02), у которой СВЕЖАЯ подписанная цена гейтвея
даёт HF_est < 0.998, стреляется через liquidateWithPush — пуш этой цены и ликвидация одной
транзакцией.

**Разведка, поменявшая план (все факты — live eth_call):**
- Карта фасад→адаптер→фид доказана бит-в-бит: цена пула = значению фида адаптера без
  трансформаций (WHYPE=A.HYPE, UBTC=A.BTC, UETH=B.ETH, USDT0=A.USDT, USOL=A.SOL, стейблы=A;
  фасад USDH = фасад USDC — один адрес). Пуш адаптера двигает цену пула 1:1.
- **wstHYPE раскрыт**: цена = A.wstHYPE_FUNDAMENTAL × A.HYPE (точное произведение, оба фида
  на адаптере A) — гейтвейные FUNDAMENTAL-фиды это РЕЙШИО (1.0313), не цены. Пушабелен.
- kHYPE/beHYPE НЕ бьются ни с одной комбинацией фидов (kHYPE 56.2778 vs B.kHYPE 56.2733 —
  близко, но не равно) — вне карты, spec по ним не стреляет, реактивный путь как раньше.
- Наборы адаптеров шире STATE-записей: B несёт HYPE/BTC/USDT/ETH/kHYPE_F/stHYPE/kHYPE/SOL/
  beHYPE_F/стейблы; A — HYPE/BTC/USDT/wstHYPE_F/SOL/стейблы (probe getLastUpdateDetails).
- Гейтвей фильтр-параметры игнорирует (полный снапшот 1.7MB), но жмётся gzip'ом до ~350KB
  за 0.11с; ecrecover без coincurve = 6мс/подпись ⇒ кэш по base64-подписи обязателен
  (тик прогретого кэша 0.07с против 0.47с холодного — замерено).

**Дизайн (bot/spec.py + интеграция executor):**
- Вотчер-daemon: один GET гейтвея на ВСЕ фиды + getLastUpdateDetails обоих адаптеров одним
  multicall; каденс 1.5с при армленной кромке / 10с вхолостую (спальный трафик ~3GB/день).
- `plan()`: HF_est = HF × (f_c·(s_c−1)+1) / (f_d·(s_d−1)+1); масштаб и пуш-набор получают
  ТОЛЬКО фиды, чей пакет строго новее хранимого (не-новее пуш адаптер отвергнет — стрелять
  без сдвига цены = гарантированный HF-реверт); непушабельный фид корректно даёт масштаб 1.
  coll==debt самогасится (s_c=s_d ⇒ HF_est=HF) — этот класс берёт реактивный путь.
- Пуш НЕ цепляется к реактивным выстрелам (on-chain HF<1): свежая цена там способна
  «вылечить» жертву нашей же рукой. Только spec-путь, только armed-цели.
- Мульти-фидовый пуш требует require_signers=False с пред-валидированными пакетами: строгая
  проверка схлопнула бы второй фид (одни и те же 5 подписантов) — байт-тест это фиксирует.
- Гарды: свежесть пакета ≤30с, порог HF_est 0.998, recently_fired/pending-дедуп, тот же
  kill-switch (промах = реверт = учёт), staleness-диагноз в лог при армленной кромке с
  холодным кэшем ([[dead-watchdog-worse-than-none]]).
- Селектор liquidateWithPush 0x57dc5978 сверен с фордж-артефактом; энкодер байт-в-байт
  против `cast calldata` (пустой и некратный 32 payload, оба flash-режима).

Тесты 163/163 (было 143: +16 bot/test_spec.py, +4 пуш-энкодер/fire). Откаты в
docs/ROLLBACKS.md (HL_SPEC_FIRE=0 = полный откат слоя). Live smoke: 9 фидов валидны,
масштабы читаются с обоих адаптеров, staleness=None.

**Что дальше по слою:** первый живой spec-выстрел покажет фактический газ пуша (лимит 2.5M
не менялся — конверт тот же); kHYPE/beHYPE-фасады трассировать отдельно (байткод → адрес
источника); predict-lite (latest_price как опережающий триггер горячего опроса) — следующий
кандидат после первых данных spec-слоя.

## 2026-08-04 (вечер): ФОРК-КАНАРЕЙКА ОПРОВЕРГЛА ПРЕМИССУ SPEC-FIRE — пуш НЕ пермишенлесс

Владелец: «нам обязательно ждать данные? нельзя сделать форк/канарейку и самим проверить?» —
и это спасло деньги: слой, задеплоенный часом ранее, оказался структурно неработоспособным,
а его отказ — МОЛЧАЛИВЫМ.

**Дифференциальный тест (anvil-форк 42293069, ОДИН И ТОТ ЖЕ payload релейера 581Б,
состояние сброшено перед каждой отправкой):**

| отправитель | результат |
|---|---|
| релейер `0x2327c3cd…` (автор tx) | ValueUpdate, цена 55.2124 → 55.4922 |
| релейер `0xe08496b4…` | ValueUpdate, цена 55.2124 → 55.4922 |
| НАШ боевой EOA `0x46345D0c…` | `UpdateSkipDueToBlockTimestamp`, цена НЕ изменилась |
| случайный адрес | `UpdateSkipDueToBlockTimestamp`, цена НЕ изменилась |

Отказ молчаливый: **status=1**, tx «успешна», газ сожжён, цена на месте. В бою это
гарантированный реверт ликвидации по HF (мы не сдвинули цену) — и три подряд снимают бота
kill-switch'ем ровно в денежное окно. Слой выключен в бою (`HL_SPEC_FIRE=0`), дефолт в коде
переведён в 0, добавлен второй рубеж — порог девиации.

**Второй замер (история ValueUpdate адаптера A):** апдейты принимаются ТОЛЬКО при
|Δцены| >= 0.50% (наблюдены 0.501/0.504/0.507/0.508%, ничего ниже), а без движения цены
интервал доходил до 70 минут. То есть даже авторизованному апдейтеру мелкая правка не
проходит; наш реальный кандидат на пуш имел 0.35% и был бы no-op даже с правами.

**Премисса разбора 04.08 «оператор сам пушит и ликвидирует в той же tx» ПОД СОМНЕНИЕМ.**
За 20k блоков (5.5ч) ВСЕ 139 апдейтов — от двух релейеров прямо в адаптер; самопушеров нет.
За 60k блоков (17ч) в пуле всего 2 ликвидации, обе — прямой `liquidationCall` от
`0x6f7d45e5…` (164Б calldata, без пуша). Наиболее вероятное объяснение lag=0 у оператора:
он НЕ пушит, а САДИТСЯ В ТОТ ЖЕ БЛОК за пушем релейера (мемпул/предочередь) — ровно то, что
STATE уже фиксировал для blk 41956546 (релейер tip 10.98 idx1, он 5.01 idx3). Прежний вывод
«один ValueUpdate в логах его tx» надо перепроверить по ЛОГАМ ЕГО ТРАНЗАКЦИИ, а не по блоку:
это тот же класс ошибки методики, что уже ловился в «Поправке 2» (фильтр по блоку вместо tx).

**Что из работы дня остаётся ценным (и куда рычаг переезжает):**
Карта «фид → цена пула» доказана трассировкой фасадов (`cast call --trace`), а не подбором:

| актив | формула | адаптер |
|---|---|---|
| WHYPE | `HYPE` | A |
| wstHYPE | `HYPE × wstHYPE_FUNDAMENTAL` | A |
| **kHYPE** | **`kHYPE_FUNDAMENTAL/USD` напрямую** | A |
| **beHYPE** | **`HYPE × beHYPE_MAIN_FUNDAMENTAL`** | A |
| UBTC/USOL/USDT0/USDC/USDH/USDe/sUSDe | одноимённый фид (USDH берёт USDC) | A |
| UETH | `ETH` | B |
| PT-kHYPE-24SEP2026 | `HYPE` × временная скидка (не оракул) | A |
| PT-kHYPE-19MAR2026 | `HYPE` 1:1 (истёк) | B |
| USDHL | **Pyth** (`getPriceUnsafe`), не RedStone | — |
| USR | КОНСТАНТА $0.5, подвызовов нет вообще | — |

kHYPE/beHYPE закрыты (прежний «нет getPriceFeedAdapter()» был тупиком: ID фида оказался
`kHYPE_FUNDAMENTAL/USD`, а не `kHYPE_FUNDAMENTAL`). Из этого следует НОВЫЙ рычаг вместо
самопуша: **предсказание по гейтвею**. Как только фид уходит на >=0.5% от хранимого,
релейер ОБЯЗАН запушить в ближайшие секунды, и мы уже сейчас знаем будущую цену пула по
доказанной формуле ⇒ считаем пост-пуш HF всей кромки ЗАРАНЕЕ, армим и садимся в блок пуша
(техника, которую оператор и применяет). Машинерия для этого уже написана и работает:
кэши bot/spec.py (гейтвей gzip 0.11с + getLastUpdateDetails обоих адаптеров) — меняется
только потребитель: вместо «пушим сами» — «ждём чужой пуш, зная его результат заранее».

Прочее из канарейки: малые блоки HyperEVM = 3M газа (наш GAS_LIMIT 2.5M помещается),
большие = 30M; пуш 1 фида = 850Б/77k газа, 10 фидов = 7.5КБ/517k.

## 2026-08-04 (ночь): SPEC-CONSUMER В БОЮ — потребитель чужого пуша («го на все» kelbic)

Пивот из предыдущей секции реализован и задеплоен в тот же день. Полоса и тайминги
поставлены ИЗМЕРЕНИЕМ, не подбором (доктрина «форк/скан вместо ожидания данных»):

**Замер 156 пушей за 6ч** (analysis/scan_pushes.py → data/pushes_20260804.jsonl):
- Релейеров два: `0xe08496b4…` (81, только B) и `0x2327c3cd…` (75, A+B); все пуши —
  `updateDataFeedsValuesPartial` (0xb7a16251).
- **Задержка подпись-пакета→лендинг: p10=2.5с p50=6с p90=7с max=11с.** Мы поллим тот же
  гейтвей раз в 1.5с ⇒ видим девиацию до лендинга. Вилка из ревью («если <1 блока —
  спекулировать вслепую») разрешилась третьим путём: задержка МНОГОБЛОЧНАЯ, слепая
  спекуляция не нужна — окно знания 1–5.5с, в него сажаем зонды.
- **Tip релейера НЕ плоский: p10=0.05 p50=1.5 p90=9.9 max=86.5** — он покупает место
  агрессивно. Оператор в обеих сегодняшних гонках сидел на 0.01 ПОСЛЕ пуша (блоки
  упорядочены по tip убыванию — подтверждено анатомией обоих блоков).
- **Полоса зонда: tip=0.02** — выше оператора (в общем пуш-блоке мы РАНЬШЕ него в 95.5%
  пушей), ниже релейера (сэндвич — пуш с tip<0.02 — 7/156=4.5%, цена = один реверт).
- **Девиационные пуши ложатся при 0.50–0.63%** (HYPE max 0.630%) — измеренный overshoot;
  порог предсказания = порог релейера 0.005.
- **Стейблы не девиируют вообще** (USDT max Δ 0.002% на 4 пушах) — кромка потребителя =
  HYPE-семейство (WHYPE/wstHYPE/kHYPE/beHYPE), BTC, ETH, SOL. HYPE: 8 девиационных
  пушей за 6ч ≈ 1.3 окна/час.
- **Реверт зонда = 210,812 газа ≈ $0.001** (anvil-форк, боевой calldata по здоровой цели):
  окно из 12 зондов стоит ~$0.012 при добыче $70–100 с кромки.

**Что в коде** (все тесты 169/169):
- `bot/spec.py`: кэши/математика НЕ ТРОНУТЫ — сменилась интерпретация (docstring):
  план = «пуш релейера неизбежен и сделает цель ликвидируемой», гейт `gw строго новее
  chain` = «пуш ещё не лёг».
- `bot/executor.py`: `_spec_pass` шлёт ОБЫЧНЫЕ liquidate-зонды (не liquidateWithPush)
  раз в hot-итерацию, окна per-borrower с капом 12 и закрытием по паузе 5с; арм живёт
  сквозь зонды (окно кормится повторами). `fire(spec_probe=True)`: tip фикс 0.02,
  ключ учёта `borrower#pN` (уникальный — зонды не душат друг друга, ключ borrower
  остаётся реактиву), запись `spec=True`. `_check_pending`: spec-реверт ОЖИДАЕМ — не
  кормит consec_reverts (три зонда сняли бы бота в окно транзита), газ книжится фактом,
  канал = лог (алерты только там, где нужен человек; победа = обычный ✅). Каденс: при
  открытом окне свип пропускается (hot-only ~1с = зонд ~раз в блок), сон 0.05с.
- Энкодер liquidateWithPush жив (тесты держат) — на случай, если RedStone когда-либо
  откроет адаптер; в бою мёртв (sender-гейт доказан форком).

**Откаты**: HL_SPEC_FIRE=0 (весь слой), остальное — docs/ROLLBACKS.md. Рестарт чист
(pid 1899374, баннер «spec-watch started (consumer)», guard=OK).

**Дальше по слою**: первое живое окно покажет реальное покрытие блоков зондами
(лог: blk зонда vs blk пуша — добавить в разбор) и P(win) против оператора; USDHL/Pyth
(пермишенлесс by design) — бэклог-полигон; перепроверка «оператор самопушит» по логам
его tx — закрыта отрицательно ещё днём (139/139 от релейеров, обе ликвидации — прямые).

## 2026-08-04 (день): уточнения kelbic к потребителю — очередь зондов + дрейф tip'а

Ревью kelbic приняло конструкцию («обнулила цену спекуляции») и дало три уточнения; два
кодовых внедрены сразу (тривиально обратимое с гардами — доктрина 31.07), третье пассивное.

- **Очередь зондов при мультитранзите** (`HL_SPEC_QUEUE=1`, executor `_spec_pass`): в каскад
  один пуш открывает НЕСКОЛЬКО целей, а зонд — один на блок ⇒ порядок целей = деньги.
  Кандидаты ранжируются по net убыванию, за проход стреляет топ, остальные ждут следующего
  блока — их окна уже открыты и кормятся планом, кап топ-цели передаёт очередь дальше
  (лог зонда несёт `q1/N`). Откат `HL_SPEC_QUEUE=0` = веер (все цели за проход). Второй
  кошелёк из пула 27.08 — следующий рычаг, если очередь окажется узкой в живом каскаде.
- **Дрейф tip'а релейера** (`HL_SPEC_TIP_LOG`, spec `_tick_chain`/`_log_push_tips`): полоса
  0.02 — равновесие на сегодня; если релейер поднимет tip после наших побед в общем блоке,
  это видно за недели. Каждое продвижение chain-ts (= лендинг его пуша) ⇒ getLogs хвоста +
  чек tx ⇒ строка в `data/relayer_tips.jsonl` (~26/час, в вотчер-треде вне горячего пути;
  к моменту детекта окно этих фидов уже закрыто — chain догнал гейтвей). Сводка недельных
  перцентилей: `python3 analysis/tip_drift.py` (дедуп по tx, только st=1, Δp50 по неделям).
  Ответ на подъём ряда — пересчёт полосы, не гонка вверх. Откат `HL_SPEC_TIP_LOG=` (пусто).
- **Живое подтверждение** — пассивно: кромка армлена, HYPE девиирует ~1.3 раза/час,
  production сам генерирует acceptance test (P(win) + покрытие блоков за дни).

Тесты 174/174 (5 новых: очередь топ-net/передача по капу/веер-откат, детект продвижения
chain-ts, запись+дедуп tip-лога). Курс подтверждён kelbic: следующий крупный заход —
лестница midnight (форк к 17.08), hyperlend в режиме сбора боевых данных.
