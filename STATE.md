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

### Ширина залпа ограничена конвертом, а не армом (замер 05.08)

Конверт = `GAS_LIMIT(2.5M) × maxFee`, maxFee = 2×base + tip; нода требует его ПОЛНОСТЬЮ и
на КАЖДУЮ tx в полёте, поэтому при `PARALLEL_BROADCAST=1` залп из N целей стоит N конвертов.
По замеренным базам каскада 10.10.2025 (med 375 / p90 1073 / max 2914 gwei), tip 50:

| режим | baseFee | конверт | залп на 10.04 HYPE |
|---|---|---|---|
| штиль (05.08) | 0.10 gwei | 0.126 HYPE ($7) | 79 |
| каскад, медиана | 375 gwei | 2.00 HYPE ($114) | **5** |
| каскад, p90 | 1073 gwei | 5.49 HYPE ($314) | **1** |
| каскад, пик | 2914 gwei | 14.7 HYPE ($840) | **0 — не хватает на одну** |

PREARM_MAX=6 ⇒ полный залп требует **12 HYPE** по медиане и **33 HYPE** по p90. Сейчас на
кошельке 10.04 — полный залп не выводится уже по медиане. Пополнение НЕЛЬЗЯ планировать
реактивно: перевод в час жатвы конкурирует за те же блоки. Держать заряд ЗАРАНЕЕ.
ВНИМАНИЕ на будущее: считать ширину по базе КАСКАДА, а не по тихой базе — расчёт по
0.1 gwei даёт «79 выстрелов» и создаёт ложное ощущение запаса (моя ошибка 05.08).

Узел требует balance ≥ GAS_LIMIT×maxFee (полный конверт): тихо ≈0.63 HYPE ($28) с нашим
авто-tip, в каскад p90 ≈6.6 HYPE ($290) — 10 HYPE = один tx в полёте на пике. Худший
расход жёстко ограничен: kill-switch HL_MAX_DAILY_GAS_USD=50/день; реалистичный тихий
месяц ≈ 7 проигранных гонок × $2–9 реверта ≈ $15–60/мес. Слить всё нельзя короче утечки
ключа; слить $50 за день — расчётный максимум.

## ДЕФЕКТ КАПА: успех считается в суточный лимит — ПОЧИНЕНО (проверено 05.08)

~~Кап учитывает ВЕСЬ газ, включая успешные выстрелы.~~ Правка семантики внесена и живёт:
`_settle_gas` (executor.py:1194) считает `win = status == 1 and not C.CAP_COUNT_WINS` и
возвращает провизорный заряд в бюджет: `st["gas_usd"] = max(0, gas_usd − rec.gas_usd) +
(0.0 if win else actual)`. Успешный выстрел стоит суточному капу НОЛЬ — он окупает свой
газ из приза в той же tx; кап ограничивает УБЫТОК (реверты/gone), как и требовалось.
Замок: `test_executor.py:956` («win must refund its charge»). Откат: `HL_CAP_COUNT_WINS=1`.

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
  чек tx ⇒ строка в `data/relayer_tips.jsonl` (единицы строк/час — хартбиты наших фидов 30-60 мин + девиации; в вотчер-треде вне горячего пути;
  к моменту детекта окно этих фидов уже закрыто — chain догнал гейтвей). Сводка недельных
  перцентилей: `python3 analysis/tip_drift.py` (дедуп по tx, только st=1, Δp50 по неделям).
  Ответ на подъём ряда — пересчёт полосы, не гонка вверх. Откат `HL_SPEC_TIP_LOG=` (пусто).
- **Живое подтверждение** — пассивно: кромка армлена, HYPE девиирует ~1.3 раза/час,
  production сам генерирует acceptance test (P(win) + покрытие блоков за дни).

Тесты 174/174 (5 новых: очередь топ-net/передача по капу/веер-откат, детект продвижения
chain-ts, запись+дедуп tip-лога). Курс подтверждён kelbic: следующий крупный заход —
лестница midnight (форк к 17.08), hyperlend в режиме сбора боевых данных.

## 2026-08-04 (вечер): аудит пакета уроков Base-liquidator (20 пунктов + live-cap)

Прогон опыта Charlotte по нашему коду. Вердикты с приёмкой:

**Live-cap выходных роутов — НЕ ПРИМЕНИМО в форме Base (проверено 04.08).** У Base статичные
max_in-капы прямых пулов протухали; у нас статических капов НЕТ по построению: выход = живая
квота LiquidSwap-агрегатора (1.7-3.5с), лестница чанков CHUNK_FRACTIONS с призо-пропорцио-
нальным бюджетом (30.07: киты $734k/$342k берутся на 1/16 с +$1,947 при impact полного
размера 45-53%), prearm-кэш котировок кромки TTL 45с / refresh 20с / shave 97%. Стареть
нечему дольше 45с.

**НО открытая дыра пакета («каскад с общим выходом») у нас есть в другой форме**: выстрел №1
осушает пул, цель №2 стреляет по prearm-кэшу возрастом до 45с → on-chain floor min_profit
превращает это в реверт (деньги целы, окно потеряно). Фикс-аналог consume-хука: на отправке
инвалидировать/бритвовать prearm-квоты с тем же выходным активом. Трогает огневой путь —
ЖДЁТ ПОДПИСИ kelbic (как и у Base).

**Связанное узкое место каскада: ~~PREARM_MAX=2~~ → поднят до 6** (config.py:219, проверено
05.08; PREARM_WORKERS=4). Ширина lag=0-каскада больше не упирается в арм. НОВОЕ узкое место
на том же месте — ГАЗ, см. «Ширина залпа ограничена конвертом» ниже.

**Уже стоит у нас (сверено по коду)**: механизм упорядочивания разобран (малые блоки = tip
desc, НЕ FCFS Base — фундамент потребителя); дешёвый промах = плотность (зонд $0.001);
reach-гейт лучше эвристики (окно только от подписанной цены гейтвея); общие ноги фидов =
DRIVERS-карта; HF/набор целиком он-чейн (API в контуре нет); nonce prewarm (04.08, WC-
инвариант); патронный резерв = полный конверт GAS_LIMIT×maxFee и зонды под тем же гейтом;
реестр откатов; хард-таймауты RPC 8/10с; доктрины 20 (память флота).

**Не применимо**: отбор рынков по займу (листинг курируемый, Aave-форк — скам-TVL не завезут);
уния API-сигналов (API в контуре нет).

**Найденные дыры (действия)**:
1. ФОРК-ЭКЗАМЕН полного пути (п.19) — ПРИОРИТЕТ: hyperlend НИ РАЗУ не стрелял в бою
   (565,739 проходов / 0 отправок на 04.08). Первый живой выстрел = первое исполнение
   flashloan→liquidationCall→swap→repay. Прогнать на anvil-форке против реальной армленной
   цели ровно ту калдату, что соберёт fire(); позитивный контроль = дельта баланса, замер
   газа против GAS_LIMIT=2.5M (флор до сих пор не подтверждён боем).
2. Каскад-пакет (PREARM_MAX + consume-хук) — дизайн есть выше, ждёт «го».
3. Бэклог: kHYPE/beHYPE разобрать accessList'ом реального апдейта (безногие фасады, урок
   JitoSOL) → расширить кромку потребителя; скан конкурентов по ПАТТЕРНУ проб (status=1,
   фикс-газ на пуле), не по ревертам — вердикт «один оператор» протухает; трасса
   триггер→отправка→ack по частям — с первого живого окна (blk зонда vs blk пуша уже пишем).

## 2026-08-04 (вечер): СРЕЗ КРОМКИ — книга кредитного плеча, а не крэша (блок 42311084)

Живой замер по горячему набору (199 адресов, sweep 1.8с). HF<1 сейчас 0. Кромка:

| адрес | HF | репей $ | бонус $ | пара (долг→залог) | видимость spec |
|---|---|---|---|---|---|
| 0xc51d4397 | 1.0015 | 602 | 81 | WHYPE→wstHYPE | разъезд только по wstHYPE_FUNDAMENTAL |
| 0xec23c052 | 1.0029 | 1,178 | 106 | WHYPE→WHYPE | САМОГАСИТСЯ (никогда не зонд) |
| 0x2385233a | 1.0258 | 117,475 | 10,573 | WHYPE→kHYPE | ВНЕ КАРТЫ |
| 0x1d7afab9 | 1.0616 | 2,280,550 | 205,250 | WHYPE→kHYPE | ВНЕ КАРТЫ |
| 0x3d0acf9d | 1.0716 | 138,209 | 12,439 | WHYPE→kHYPE | ВНЕ КАРТЫ |
| 0xa625e8ae | 1.0758 | 3,708,136 | 333,732 | WHYPE→PT-kHYPE-24SEP2026 | ВНЕ КАРТЫ |
| 0xe1b2d061 | 1.0767 | 237,739 | 21,396 | WHYPE→kHYPE | ВНЕ КАРТЫ |
| 0xc853e788 | 1.0831 | 297,303 | 21,406 | WHYPE→USDC | ВИДИМА (разъезд HYPE/USDC) |

**Вывод 1 — кромка иммунна к движению HYPE.** Долг WHYPE против залога kHYPE/wstHYPE/PT-kHYPE
= обе ноги одна семья: HF = s·C·LT/(s·D) — цена сокращается, HF не двигается. Это книга
кредитного плеча на стейкинге (занял HYPE под стейкнутый HYPE), а НЕ крэш-книга. Её HF
двигают только (а) расхождение kHYPE/HYPE (депег), (б) начисление процентов по займу WHYPE,
(в) смена LT. Обвал HYPE эту кромку не флипнет. Первая наивная оценка «падение залога 2.5%»
неверна для таких пар — считать дистанцию можно только по РАЗЪЕЗДУ ног (урок замера).

**Вывод 2 — обе армленные цели не могут дать зонд.** 0xec23 (coll==debt) самогасится по
конструкции plan(); 0xc51d двигает только wstHYPE_FUNDAMENTAL (ставка стейкинга, дрейфует
ВВЕРХ = от ликвидации). Значит гипотеза ревью «две армленные цели = acceptance test
потребителя» ОПРОВЕРГНУТА: приёмку они не дадут никогда.

**Вывод 3 — kHYPE это не бэклог, а главный рычаг.** В радиусе 8% от ликвидации лежит
~$605k бонуса, из них ~$584k на kHYPE/PT-kHYPE — вне карты DRIVERS ⇒ предиктивный слой слеп,
видит ОДНУ позицию из восьми ($21.4k, 0xc853, HYPE-ралли +7.7% против долга). Реактивный путь
их берёт (карта нужна только предикту), но с лагом +1 блок = проигрыш оператору. Задача №6
(accessList-разбор kHYPE/beHYPE/PT) поднимается над №5 и №7.

Скрипты замера: jobs/d9c2c3f6/tmp/edge_snapshot.py, whales.py (одноразовые, вне репо).

## 2026-08-04 (вечер): kHYPE РАЗОБРАН ACCESSLIST'ОМ — слепая зона предикта закрыта

Задача №6 из пакета уроков («каждый безногий оракул разбирать accessList'ом», урок JitoSOL)
выполнена; попутно ОПРОВЕРГНУТ мой же вывод часом ранее.

**Метод**: `eth_createAccessList` на `AaveOracle.getAssetPrice(asset)` даёт ровно те слоты
адаптеров, что читает фасад (proxy-слоты EIP-1967 отсеиваются); слот значения опознаётся
сверкой с `getLastUpdateDetails` по всем 861 фидам гейтвея. Брутфорс keccak по раскладке
mapping НЕ сработал (RedStone использует именованную storage-локацию) — сверка значением
оказалась и проще, и надёжнее.

**Результаты (бит-в-бит)**: kHYPE = `kHYPE_FUNDAMENTAL/USD`@A **одним слотом**, 56.850876;
beHYPE = HYPE×beHYPE_MAIN_FUNDAMENTAL@A, Δ 0.00000%; wstHYPE = HYPE×wstHYPE_FUNDAMENTAL@A,
Δ 0.00000%; PT-kHYPE-24SEP2026 = HYPE@A × дисконт Pendle; PT-kHYPE-19MAR2026 = HYPE@B ×
дисконт. Дисконт RedStone не пушит, но в МАСШТАБЕ он сокращается — замер дрейфа PT/HYPE
0.995877→0.995850 за 8ч (0.0027% против порога 0.5%), поэтому масштабирование по HYPE законно.

**ОПРОВЕРЖЕНИЕ вчерашнего вывода**: «кромка иммунна к цене HYPE, потому что обе ноги одной
семьи» — неверно НА УРОВНЕ ОРАКУЛА. Экономически kHYPE и HYPE связаны, но фасады читают
РАЗНЫЕ фиды с независимыми расписаниями: в 156 пушах/6ч tx с HYPE — 35, с kHYPE* — 17,
**общих НОЛЬ**; ряд kHYPE/HYPE за 8ч гулял 1.0194..1.0246 (±0.5%). Пуш HYPE переоценивает
долг WHYPE, залог kHYPE стоит на старом значении до своего пуша — это и есть окно. Урок:
экономическую связь активов нельзя переносить на оракул, пока не измерено расписание пушей.

**Что изменилось в бою**: DRIVERS 10→14 активов, WATCH 9→12 пар. Предикт видел 1 позицию
ближней кромки из 8 (~$21k из ~$605k бонуса) — теперь видит все, включая китов $205k и $334k.
Реактивный путь не тронут. Тесты 179/179 (5 новых: карта семейства, отсутствие самогашения
пары WHYPE-долг/kHYPE-залог, просадка собственного фида kHYPE, масштаб PT по HYPE, страховка
самогашения одинаковых ног). ОТКАТ: `HL_SPEC_KHYPE=0` (проверен: 10 активов / 9 пар).

## 2026-08-04 (ночь): БАЗОВАЯ ЧАСТОТА ЛИКВИДАЦИЙ И СТРЕСС-ТЕСТ КНИГИ

**История пула (getLogs, 14 дней / 1,209,600 блоков): 47 ликвидаций = 3.36/день**, но поток
кластерный: 13 событий в одни сутки, 10 в другие, 8 в третьи, и трое суток ПУСТЫЕ — ~65%
объёма пришлось на 3 дня из 14. Пары долг→залог: USDC→WHYPE 13, USDC→kHYPE 6, USDT0→WHYPE 5,
USDe→wstHYPE 4, USDT0→wstHYPE 3, USDT0→kHYPE 2, UETH→WHYPE 2, USDe→kHYPE 2, USDHL→USDT0 2,
прочее по 1. **44 из 47 = стейбл/ETH-долг против залога HYPE-семейства** — классический
крэш-класс, флипается просадкой HYPE. Ликвидаторов минимум 8 (топ: 0x6f7d45e5 — 11,
0xd52b5909 — 9, 0x98ff6022 — 8, 0xae86edb5 — 8): поле конкурентное, тезис «один оператор»
для этого горизонта НЕВЕРЕН.

**Стресс-тест текущей книги** (197 позиций hot-set с сайзингом, масштабирование ног
HYPE-семейства):

| движение HYPE | позиций HF<1 | бонус $ | репей $ |
|---|---|---|---|
| −0.5% … −5% | 0 | 0 | 0 |
| −10% | 1 | 155 | 1,726 |
| −20% | 26 | 193,571 | 2,149,692 |
| −30% | 45 | 279,326 | 3,100,126 |
| +10% | 1 | 60 | 668 |

**Парадокс объяснён**: сегодняшняя книга у кромки — почти вся кредитное плечо на стейкинге
(долг WHYPE против залога kHYPE/wstHYPE/PT), чей HF к цене HYPE иммунен ЭКОНОМИЧЕСКИ (обе
ноги двигаются вместе; в таблице их HF не меняется вовсе). Крупнейшие позиции книги —
$552k, $334k, $320k, $225k бонуса — все этого класса и на обвал не реагируют. Поток же
3.36/день дают позиции ДРУГОГО класса (стейбл-долг), которые подползают к единице процентным
дрейфом и падают от мелких движений; в стресс-таблице их мало, потому что до кромки им далеко
(ближайшая USDC→WHYPE: HF 1.1753 → 1.0578 при −10%).

**Что реально порождает события** (по убыванию вклада): (1) просадка HYPE ≥10-20% — рубит
крэш-класс пачкой, это и есть каскад; (2) процентный дрейф — непрерывный ручеёк 3.36/день,
не требует вообще ничего; (3) депег kHYPE/wstHYPE к HYPE — единственный рычаг против книги
плеча (ей нужно ~5-6% сжатия ряда, замер сегодня: ряд гулял 1.0194..1.0246); (4) депег
стейблов USDe/USDHL со стороны долга; (5) срез LT управлением — мгновенный сдвиг всей книги.

**Для зонд-слоя**: приёмка требует цели с РАЗЪЕХАВШИМИСЯ ногами в пределах одного пуша
(~0.5-0.6% по HF). Сейчас таких нет: 0xec23 самогасится, 0xc51d двигает лишь фид ставки
стейкинга. Ближайшая пригодная — 0x2385233a (WHYPE→kHYPE, $10.6k) на дистанции 2.5%, то есть
4-5 последовательных пушей HYPE вверх без ответа kHYPE, либо депег. Вывод: ждать приёмку
«за дни» неверно — она придёт с первым же настоящим движением, а не с фоновой девиацией.

## 2026-08-05: КОШЕЛЁК ОБЪЯСНЁН + ПАКЕТ УЛУЧШЕНИЙ А-Е (го kelbic)

**Кошелёк 0x4634…73E3 (живой замер)**: 10.038 HYPE, nonce=4 (сетап: деплой контракта и
т.п.), боевой расход $0.00 (fires=0). Роль баланса — НЕ капитал (репей несёт флешлоун
внутри tx), а: (1) конверт-пропуск ноды `balance ≥ GAS_LIMIT×maxFee` — до 2.5 HYPE на
один спорный выстрел с tip у капа, резервируется КАЖДОЙ висящей tx; (2) газ: зонд $0.003,
выстрел ~$1, спорный tip по киту до ~$100 (≤5% приза); (3) пол гейта BALANCE_FIRES=3.
10 HYPE ≈ 4 максимальных конверта в полёте — пополнение сейчас не нужно.

**Принятый пакет (после #4 форк-экзамена, #5 каскада, #7 скана конкурентов)**:
- **А. Таймер-класс**: дрейф HF от процентов детерминирован → вычислять timestamp
  пересечения HF=1 для позиций 1.00-1.02 со стейбл-долгом и класть выстрел в первый блок.
  Атакует ручеёк 3.36/день, где реакцией мы 0/47. Премисса-чек: сколько из 47 событий —
  чистый дрейф (без пуша оракула рядом). Shadow-first, живой огонь по отдельному «го».
- **Б. Preflight китовых выходов**: квотер LiquidSwap на kHYPE→стейбл $300-500k ДО
  каскада (стресс: −20% = $2.15M репея; урок Base weETH — обрыв роута вскрылся на бою).
- **В. Сжатие GAS_LIMIT** по реальному газу форк-экзамена (2.5M «на глаз» → замер+запас;
  конверт ∝ лимиту, −40% лимита = +66% параллельных выстрелов на те же 10 HYPE).
- **Г. USDHL/Pyth self-push**: Pyth permissionless (в отличие от сендер-гейта RedStone) —
  единственный рынок, где мы можем быть ТРИГГЕРОМ пуша, не потребителем. Рынок мелкий
  (2 ликв./14д), ценность = уникальная механика «сам пушнул — сам ликвиднул».
- **Д. Депег-вотч kHYPE/HYPE** (часовой тренд, Вена): киты $552k/$334k/$320k падают только
  от сжатия ряда ~5-6%; сжатие >1.5% → пре-арм китов заранее.
- **Е. Пересчёт tip-прайора [5,1000]** по relayer_tips.jsonl + 47 победителям (80-1225 gwei)
  через ~неделю накопления.

## 2026-08-05 (день): ФОРК-ЭКЗАМЕН ПРОЙДЕН — НАЙДЕН И ПОЧИНЕН ДЕФЕКТ, УБИВАВШИЙ ВСЮ КРУПНУЮ КНИГУ

### Главное: eMode-бонус (коммит bc4e475)

Модель брала `liquidationBonus` из конфига **резерва** (kHYPE = 11000), а пул применяет
бонус **eMode-категории заёмщика** (10500). Расхождение 4.13% против haircut'а 0.3% ⇒ в
calldata свопа зашивался amountIn от завышенного залога, роутер падал на «LiquidSwap:
Insufficient token balance», контракт отдавал `SwapFailed()` 0x81ceff30. Это не «неточная
оценка», а **гарантированный реверт на каждом выстреле по плечевой книге** — то есть по
всей крупной книге. Вживую не всплыло только потому, что бот ещё не стрелял (fires=0).

Живой замер категорий: их 6, у ВСЕХ бонус 10500 (премия 5%) против резервных 11000-11500
(10-15%). Кит 0x1d7afab9 и 0x2385233a — категория 5 (залоги {kHYPE,+2}, займ WHYPE),
0xec23c052 — категория 1. Aave v3.2+: принадлежность лежит в масках категории, поле
eModeCategory в конфиге резерва нулевое на всех 18 резервах — по нему искать бесполезно.

**Приёмка по дельте состояния (форк, боевой haircut 0.3%)**:
- до фикса: status 0x0, SwapFailed, долг заёмщика не изменился;
- после: **status 0x1, долг −$420,504**, HF 0.9035→0.9024, gasUsed 1,402,329;
- бонус кита в модели: $401k → $190k (премия 9% → 4.5%) — теперь честный.

**Контроль на ЖИВОМ боте после перезапуска** (лучше любого теста): цель в категории 1
пересчиталась net $104.8 → $51.9 (ровно вдвое), а контрольная цель БЕЗ категории
(0xc51d4397, eMode=0) осталась $77.2 без изменений. Правка бьёт ровно туда, куда должна.

### Б. Preflight китовых выходов (#9) — карта глубины снята

Замеры воспроизводимы (3/3 идентичных пробы), потеря считается против цены оракула:

| нога | чисто до | обрыв | поведение за обрывом |
|---|---|---|---|
| kHYPE→WHYPE | **$600k** (−0.05%, т.е. чуть в плюс) | $700k (95.2%) | выход насыщается, роут капнут |
| wstHYPE→WHYPE | $65k (0.15%) | $70k (3.7%), $75k (9.9%) | $100k = 32% |
| kHYPE→USDT0 | $25k (0.40%) | $50k (6.7%), $100k (66%) | насыщение на $33.4k |
| PT-kHYPE-24SEP2026 | **роута нет** | — | HTTP 404 на любом размере, от $1k |
| PT-kHYPE-19MAR2026 | **роута нет** | — | HTTP 400 на любом размере |
| beHYPE→WHYPE | **роута нет** | — | HTTP 400 на любом размере |

Выводы: (1) главная нога китов kHYPE→WHYPE держит до $600k за выстрел — лестница чанков
доходит до неё сама (у кита сработал f=0.1); (2) **PT-kHYPE-SEP26 ($333k бонуса, главный
приз книги) и beHYPE НЕ ПОДДЕРЖАНЫ квотером вообще** — это не глубина, а отсутствие токена:
без второго DEX/Pendle-роутера эти цели недостижимы, сколько бы бонуса ни показывала модель;
(3) выход в стейблы — узкое горло ($25k), но он и не нужен: долг китов номинирован в WHYPE.
Инструмент: `analysis/preflight_exits.py` (только чтение, кошелёк не трогает).

### В. Газ (#10) — замер есть

gasUsed **1,402,329** (кит, мультихоп-своп, флешлоун) и 936,830 (тот же путь, другой роут)
против лимита 2,500,000. Лимит малого блока HyperEVM замерен = **3,000,000**, наши 2.5M в
него укладываются — ловушки «уехать в большой блок» нет. Запас 1.8× к худшему замеру;
рекомендация — лимит НЕ трогать: экономия конверта копеечная, а промах по газу стоит и
газа, и гонки.

### Ж. Гигиена сигнатур по флоту (коммит dabc604)

Проверены все четыре бота: hyperlend = Aave v3 (топик совпал с каноном 0xe413a321…005286),
katana и wc = Morpho Blue `Liquidate(bytes32,address,address,uint256×5)`, midnight берёт
сигнатуры из `EventsLib.sol` на закреплённом коммите. Хардкода хэшей нет нигде — все считают
topic0 из строки офлайн. Закреплено `analysis/test_topics.py`.

### Инфраструктура форк-экзамена (три источника тихой лжи, все встречены за один прогон)

1. `analysis/rpc.py` **не читает окружение** (`self.urls = urls or DEFAULT_RPCS`) —
   `HL_READ_RPCS` разбирает только `bot/config.py`. Скрипт ставил env и мёл живую сеть.
2. Форк с **не-архивного** узла = заголовок блока N + состояние головы ⇒ `lastUpdateTimestamp`
   резерва «в будущем» ⇒ `panic 0x11` в Aave на каждом аккаунте.
3. **Кэш форка** `~/.foundry/cache/rpc/<chain>/<block>/` переживает рестарт и смену апстрима.

Все три возвращают пустой корректный результат «целей нет». Обязательный набор контролей:
номер блока клиента == база форка; подменённая величина видна ЧЕРЕЗ ТОТ ЖЕ клиент; опорный
вызов сходится с архивом в том же блоке (HF 1.2456/1.2710/1.1592 совпали до знака).

### Инцидент

Перезапуская бота, погасил по общему шаблону `bot.executor loop` **katana** (у ботов флота
одинаковая командная строка) — крон поднял её через ~2 минуты, ликвидаций в окне не было.
Урок к [[proc-kill-self-match]]: во флоте матчить процесс по `cwd` из `/proc`, а не по cmdline.

## 2026-08-05 (вечер): ЗАКРЫТА ВСЯ ОЧЕРЕДЬ «ГО НА ВСЁ» + ПОПРАВКИ РЕВЬЮВЕРА

### Е. Чаевые: платим рыночную цену места, а не долю приза (1b164a5)

Разбор 21 реальной ликвидации за 6 суток и 19 блоков целиком:
* **порядок внутри блока по tip подтверждён строго** — 398 пар «раньше = больше чаевых»
  против 93 (81%). Кросс-блочная корреляция idx с tip (−0.008) была НЕГОДНЫМ тестом:
  в разных блоках разные соперники, сравнивать можно только внутри блока;
* место стоит дёшево: верх блока = медиана 2.0 gwei (p75 5.0, максимум 20.02), победители
  ликвидаций платили 0.0–5.01;
* прежняя формула делала драйвером долю приза и на ручейке назначала абсурд: приз $200 →
  115 gwei (в 5.7 раза выше любого наблюдавшегося платежа), $1,000 → 575 gwei (в 28 раз).

Теперь драйвер — рынок (медиана p90 за 20 блоков через `eth_feeHistory`, обновляется В ЦИКЛЕ
рядом с baseFee: на пути выстрела RPC недопустим), доля приза — потолок. При призе ≥$5k платим
потолок сразу: метрика запаздывает по построению, а терять кита ради экономии $80 нельзя.
Живой пересчёт: приз $200 было 148 gwei ($12.03) → стало 5.3 gwei ($0.43).

### #7. Конкурентов ЧЕТЫРЕ, один держит половину потока

EOA `0x84d5e280…` шлёт через ТРИ разных контракта-исполнителя (0x98ff6022, 0xab7b11c6,
0xae86edb5) — 11 из 21 выстрела, 52% потока. Остальные: 0x6f7d45e5 (6, EOA==контракт,
tip 0.01), 0x5e1e220d (3, tip 1.0), 0xdb201b4a (1, tip 0.0). **Ключевое совпадение:**
релей-контракт лидера `0x7a0ea1f7…` — это один из АДАПТЕРОВ пуша оракула из журнала бота.
Он и пушит цену, и ликвидирует; отсюда его lag=0.

> **ПОПРАВКА 05.08 (разбор гипотезы ревьювера, см. ниже):** «`0x7a0ea1f7…` — это адаптер»
> НЕВЕРНО. Его байткод не содержит ни одной функции адаптера RedStone
> (`updateDataFeedsValuesPartial`, `getLastUpdateDetails`, `getValueForDataFeed`,
> `getUniqueSignersThreshold`, `getAuthorisedSignerIndex`), зато содержит `owner()`. Это
> ЕГО СОБСТВЕННЫЙ входной контракт: в tx `0x2c634d8b…` он вызывает настоящий адаптер
> `0xe4ae8874…` (лог №0 = `ValueUpdate` от адаптера), а лог №38 — его собственное событие.
> Адаптеров-приёмников ДВА (`0xe4ae8874…`, `0x24c89643…`); третий адрес в журнале — цель,
> а не приёмник (сканер подставляет `to`/первый лог, отсюда путаница).

### А (#8). Премисса измерена: таймеру адресуемо 38%, не весь поток

13 из 21 (62%) ликвидаций имели пуш оракула в окне [блок−3; блок] — там оператор недостижим.
Без пуша — 8 из 21 (38%), это и есть потолок таймер-класса. Поправка ревьювера подтверждена.

**Отдельно про метод:** первая версия проверки искала пуши по фасадам оракула и дала
21/21 «без пуша» — ложный ноль. Поймал позитивный контроль по собственному журналу бота
(`data/relayer_tips.jsonl`, 135 записей): НИ ОДИН из трёх реальных адаптеров в набор фасадов
не входит — пуш адресуется адаптеру, а не фасаду. Без контроля вывод «таймер берёт 100%»
ушёл бы в стратегию как факт.

### З (#15). Маршруты выхода: два из трёх закрыты (56fe3da)

**Найдена наша собственная ошибка:** пять из шестнадцати адресов несли битую контрольную
сумму EIP-55 (beHYPE, PT-19MAR2026, USDHL, USR, USOL). HTTP-квотеры её валидируют — liqd
отвечал 400, и это читалось как «роута нет». Починено, замок в `analysis/test_topics.py`.
С верным адресом liqd по beHYPE отвечает УСПЕХОМ и константой 1.690 WHYPE на любой вход
(impact ровно 90%) — отравленная котировка, а не отказ.

`bot/routers.py`: Pendle для PT (ветка swap/redeem по on-chain `isExpired()`, не по дате в
коде), Kyber как универсальный запасной. Замеры: PT $200k → impact 0.072%, $1M → 0.058%;
beHYPE $29k → 497.94 WHYPE через Kyber.

**PT-кит всё ещё недостижим, но диагноз точный.** Форк-экзамен: evaluate выбрал Pendle,
чанк 0.35, net $590k — маршрут строится. Транзакция ревертит, и это НЕ газ-лимит и НЕ
недостача залога (проверено запасом haircut 6% и газом 2.9M — тот же SwapFailed): падает
вложенный своп агрегатора внутри маршрута PT→WHYPE по out of gas, а малый блок HyperEVM
даёт всего 3M. Лёгкая ветка PT→kHYPE (2085 байт против 4421, без агрегатора) в бюджет
влезает, но даёт kHYPE, а долг в WHYPE; запасной ноги у кита нет — весь залог в PT.
⇒ задача #16: контракт v3 с двумя ногами свопа. **Требует подписи владельца (деплой).**

### #5. Каскад: пре-арм квотит параллельно, глубина 2→6 (eb65d07)

Замер квотера: 10 последовательных + лестница 2/4/8 параллельных — НИ ОДНОГО отказа,
медиана 1.48с→1.80с. Потолок держал не API, а последовательный обход. Consume-хук уже был.

### Г (#11). Механика Pyth подтверждена, стройка отложена по объёму

accessList: путь цены USDHL уходит в `0x2880ab15…` (20,568 байт — размер Pyth) и не
пересекается с RedStone-путём (через адаптер `0xe4ae8874…`, он же принимает пуши). Механика
реальна и уникальна, но весь долг рынка ~$11.6k. Возвращаться, если объём вырастет на порядок.

### Инструмент против инцидента с katana (fleet-watch 7dd0062)

`~/.fleet-watch/fleetctl.sh` — опознание и перезапуск бота ИСКЛЮЧИТЕЛЬНО по `/proc/PID/cwd`,
никаких `-f` шаблонов (у всех ботов флота командная строка одинаковая). Приёмка перезапуска —
смена PID И ожившая запись в логе. Проверено боевым перезапуском hyperlend.

## 2026-08-05 (ночь): КОНТРАКТ v3 В БОЮ — PT-кит стал достижим

**Адрес v3: `0x0cf80b56c78013d63741b31ba01811cec8ca088c`** (деплой tx `0x9eeb12d9…`, gasUsed
1,077,423). Проверен по цепи: owner = наш кошелёк, POOL верный, оба селектора на месте.
**ОТКАТ: `HL_CONTRACT=0x5C20F458a14849673ec1aec407f6ed22f82d07Af`** (v2, одна нога — совместим,
двуногих выстрелов просто не будет, PT-цели снова станут недостижимы).

### Что решено

PT-кит `0xa625e8ae` ($7.7M долга, залог целиком в PT-kHYPE, запасной ноги нет) был недостижим
не по экономике. Прямой маршрут PT→WHYPE у Pendle тянет вложенный своп агрегатора и падает по
out of gas: SwapFailed и при 2.5M, и при 2.9M, причём залога хватало (проверено запасом haircut
6%), а малый блок HyperEVM даёт всего 3M. Лёгкая ветка PT→kHYPE (2085 байт против 4421, без
агрегатора) в бюджет влезает, но даёт не тот актив — долг номинирован в WHYPE.

`liquidateTwoLeg(...)`: залог → midAsset → долг. Вторая нога включается только явным
swapTarget2, поэтому одноногий путь прежний (закреплено тестом). Аргументы свёрнуты в struct
`Legs` — иначе stack-too-deep; прятать их в calldata-структуру значило бы заставить бота
кодировать кортеж с двумя динамическими bytes вручную, а это лишний источник ошибок ровно на
пути выстрела. Включён `via_ir`.

На стороне бота: `routers.quote_pt_two_leg` строит обе ноги, импакт СУММИРУЕТСЯ (гейт обязан
видеть весь путь, а не половину), `_encode_liquidate` выбирает селектор по наличию второй ноги
в котировке — решение принимает КОТИРОВКА, а не флаг конфига.

### Потолок глубины — без него две ноги не помогли бы

Выше обрыва **котировка врёт**: на $1.15M kHYPE→WHYPE квотер вернул impact −0.03% («лучше
оракула»), а своп ревертнул в самом пуле (custom error `0xd93c0665`). Порог импакта такую
котировку не ловит по построению. Размер теперь режется по ЗАМЕРУ (kHYPE $600k, wstHYPE $65k,
beHYPE $55k, env-переопределяемо), и лестница чанков сама спускается под обрыв: на f=0.15 был
реверт, на f=0.06 — успех.

### Приёмка на форке (родным конвейером, по дельте состояния)

```
evaluate сам выбрал f=0.06, venue=pendle+liqd, impact 0.019%, net $97,898
селектор 0x2caf0ff6 (две ноги), status 0x1
долг заёмщика −$457,826, gasUsed 1,801,289 = 60% лимита малого блока
```
Ручной прогон до этого: −$464,727 при 1,384,844 газа. Тесты: forge 18 (из них 5 на две ноги),
python 213.

Бот перезапущен на v3 (`fleetctl restart hyperlend`), армит цели, guard=OK.

## 2026-08-05 (день): ВАЛИДАЦИЯ ЗАКРЫТИЯ БЭКЛОГА — все пункты подтверждены позитивным контролем

Правило прогона: приёмка по дельте состояния/живому чтению, не по записям и не по тишине в логе.

### Что подтверждено

- **Тесты**: python 217 passed (8.6с), forge 18 passed + 2 skipped (см. фикс ниже). Git чист.
- **Контракт v3 в цепи**: код 4528 байт на `0x0cf80b56…`, все 9 селекторов артефакта найдены в
  живом байткоде (включая все три боевых пути: `liquidate` 0x3c78a656, `liquidateTwoLeg`
  0x2caf0ff6, `liquidateWithPush` 0x57dc5978), owner = наш кошелёк. `HL_CONTRACT` переключён,
  откат на v2 задокументирован прямо в env.
- **Бот живой**: guard=OK (1476/2000 строк), 0 трейсбеков, pre-arm армит кромку (2 цели,
  net $52/$78). Все четыре бота флота пишут лог ≤2с.
- **eMode живьём** (дефект bc4e475): категории читаются (6 шт., все бонус 10500), реестр китов
  сошёлся с таблицей ревьювера — `0x2385233a`=cat5, `0x1d7afab9`=cat5, PT-кит `0xa625e8ae`=cat6;
  для его НАСТОЯЩЕГО залога (PT, индекс 17 в битмапе 0x30000) бонус = 10500, для kHYPE — резервные
  11000. Отказ чтения печатал бы `[emode]` в лог — в логе пусто И путь доказан живым RPC.
- **Рыночный tip живьём** (Е): `eth_feeHistory` отдаёт награды, медиана p90 = 0.1 gwei.
- **Депег-вотч (Д)**: крон 17-й минуты жив, `depeg_history.jsonl` пишется, таблица китов свежая
  (ближайший запас 2.61%).
- **Гигиена сигнатур (Ж) по флоту**: 64-hex константы в katana/wc/midnight — market-id Morpho и
  цели, НЕ топики событий; хардкода topic0 нет нигде.
- **Рестарт-пути**: run.sh — kill по pid-файлу; fleetctl — только cwd-признак.

### Два шва, найденные валидацией (оба закрыты)

1. **Голый `forge test` был красным по умолчанию**: ForkLiquidation.setUp + ForkSwap требовали
   `HYPE_RPC` и падали без него. Красный-по-умолчанию приучает игнорировать красное. Теперь
   `vm.skip` при пустом env (1d19695); живой прогон как раньше.
2. **mn-profile.sh убивал по `pgrep -f 'schedule' | head -1`** — тот же класс лотереи, что
   погасил katana 05.08; уникальность паттерна была совпадением, не гардом. Переведён на
   `fleetctl pid midnight` (fleet-watch 2cb0769).

### Урок валидации

Дважды чуть не объявил живой eMode-путь дефектным из-за СВОЕЙ ошибки: восстанавливал полные
адреса китов из префиксов сводки (хвост выдуман — eth_call честно даёт 0 по несуществующему
адресу). Адрес из префикса не восстанавливается: только полный, из данных (exam_targets.json,
data/). Позитивный контроль обязан начинаться с проверки собственных входов.

# 2026-08-05 (день): ГИПОТЕЗА РЕВЬЮВЕРА О PERMISSIONLESS-ПУШЕ — ЗАКРЫТА, ГЕЙТ НАЙДЕН В БАЙТКОДЕ

Ревьювер оспорил вывод 04.08 «нас не пускают»: канарейка отвалилась по
`UpdateSkipDueToBlockTimestamp`, а это событие срабатывает ПОСЛЕ проверки подписи ⇒ пакет был
старым, а не отвергнутым, и self-push должен воспроизводиться. Проверено — гипотеза
**опровергнута**, но она была разумной: гейт по отправителю маскируется ровно под это событие.

## Что сделано (`contracts/test/ForkSelfPush.t.sol`, `analysis/selfpush_probe.py`)

1. **Свежий пакет с публичного гейтвея** (5 пакетов, все 5 подписантов авторизованы, возраст
   10с, пакет НА 13 МИНУТ свежее записанного on-chain) → `updateDataFeedsValuesPartial` с
   чистого EOA на форке головы: `success`, но состояние НЕ изменилось. Единственное событие —
   `UpdateSkipDueToBlockTimestamp("HYPE")` от адаптера. Устаревание как причина исключено.
2. **Интервал как причина исключён замером:** за 14ч 95 `ValueUpdate`; минимальный принятый
   интервал HYPE = **2с** (SOL/BTC/USDC/USDT — 3с). Наша попытка шла через 870с после
   последнего апдейта.
3. **Решающий эксперимент — реплей принятого пуша с подменой отправителя.** Берём реальную
   транзакцию релейера `0x8e3b724a…` (блок 42374098), форкаем блок−1, шлём ТЕ ЖЕ БАЙТЫ:
   - **контроль** (сам релейер `0x2327c3cd…`) — `ValueUpdate`, состояние сдвинулось ⇒ стенд
     честен (без этого контроля любой отрицательный результат ничего не стоит);
   - **опыт** (посторонний EOA) — `UpdateSkipDueToBlockTimestamp`, состояние не сдвинулось.
   Единственная изменённая переменная — `msg.sender`.
4. **Список допущенных снят перебором отправителей** (`test_whoCanPush`, тот же блок/байты):

   | отправитель | лёг |
   |---|---|
   | `0x2327c3cd…` релейер #1 | **да** |
   | `0xe08496b4…` релейер #2 | **да** |
   | `0x7a0ea1f7…` контракт лидера | **да** |
   | `0x84d5e280…` EOA лидера | нет |
   | `0xae86edb5…` его контракт-исполнитель | нет |
   | `0x0cf80b56…` наш контракт | нет |

5. **Гейт найден в байткоде имплементации.** Адреса вшиты константами:
   - `A_impl 0x84c698e6…` (HYPE/BTC/USDT/USDC/kHYPE_FUNDAMENTAL/…): релейер #1, релейер #2,
     **и контракт лидера `0x7a0ea1f7…`**;
   - `B_impl 0x131141e6…` (ETH): только два релейера — **лидера там НЕТ**.
   Публичного геттера списка нет (`isAuthorisedUpdater`, `getAuthorisedUpdaters`,
   `hasRole`, `getMinIntervalBetweenUpdates` — ни одного селектора в коде).

## Выводы

- **Self-push для нас закрыт навсегда** — не подписью, не свежестью, не интервалом, а
  вшитым в имплементацию адаптера списком апдейтеров. Открывается только редеплоем адаптера
  со стороны RedStone. `HL_SPEC_FIRE` из роллбека боевым НЕ выходит.
- **Преимущество лидера — не скорость, а ДОСТУП.** Он вписан в байткод оракула наравне с
  релейерами; отсюда lag=0 на всех его победах. Гонку с ним по латентности выиграть нельзя
  в принципе: он ликвидирует в той же транзакции, что двигает цену.
- **Асимметрия, которую можно использовать: на адаптере B (ETH) лидера НЕТ.** Рынки, чей
  ценовой вход идёт через `0x24c89643…` (UETH, PT-kHYPE-19MAR2026), для него такие же
  реактивные, как для нас — там гонка честная.
- **Урок метода:** гейт по отправителю у этого адаптера НЕ реверт и НЕ отдельное событие —
  он неотличим от устаревания пакета. Отличить можно только реплеем принятой транзакции с
  подменой одного отправителя. Позитивный контроль (тот же пуш от релейера) обязателен:
  без него «не легло» одинаково читается как «нас не пускают» и как «стенд врёт».

# 2026-08-05: ЗАМЕР ПОТОКА — «можем ли мы вообще участвовать в ежедневных ликвидациях»

`analysis/scan_liquidations.py` (якорный позитивный контроль), окно 30 суток, блоки
39739768–42374985:

- **79 ликвидаций, приз всего $10,247** ($342/сут на ВЕСЬ протокол, медиана выстрела — центы).
- **Всё, что дороже $10, — это 9 событий на $10,211 (99.6% денег), и ВСЕ ДЕВЯТЬ атомарные:**
  пуш оракула лежит в той же транзакции. Реактивный приз ≥$10 за месяц — **$0**.
- Пять контрактов лидера взяли $10,240 из $10,247 = **99.93%**. Остальные одиннадцать
  адресов (включая 0xa8a1708c с 20 выстрелами) собрали центы.
- Батчей нет: 79 ликвидаций в 79 транзакциях, максимум 2 в одном блоке, газ 0.68–1.76M.
- Наш бот за это время: **590,217 проходов, 0 целей, 0 выстрелов** — мы не проиграли ни
  одной достижимой гонки, достижимых просто не было.

**Вывод.** Тихий режим для нас закрыт, и закрыт НЕ латентностью, а доступом (гейт в байткоде
адаптера, см. выше). Тюнинг скорости, лестницы чаевых и плотность зондов в этом режиме не
покупают ничего: приза, до которого можно дотянуться реакцией, в потоке нет.

**Где слой остаётся живым.** (1) Каскад: лидер НЕ батчит — одна жертва на транзакцию, ≤2 на
блок; в каскаде 10.10.2025 (113 ликвидаций/$407k за час) 85% денег ушло с лагом ≥5 блоков, и
атомарного оператора там не было вовсе. Наш контур — это опцион на каскад, а не ежедневный
бизнес. (2) Адаптер B (`0x24c89643…`): лидера в его списке НЕТ, значит на рынках, чья цена
идёт оттуда (UETH, PT-kHYPE-19MAR2026), гонка честная — но за 30 суток там $0 приза, так что
это страховка, а не доход.

**Что НЕ отменяется:** `HL_SPEC_FIRE` — это слой-ПОТРЕБИТЕЛЬ чужого пуша (зонды садятся в
блок пуша после релейера), а не self-push. Его премисса жива и он остаётся боевым; умер
именно self-push, который мы так и не включали.

## Дополнение 05.08 (ответ на возражение ревьювера): кросс-тест A/B закрывает тему

Возражение: «0x7a0ea1f7 — исполнитель, значит он просто делает self-push, а это
воспроизводимо; админ-поверхности на адаптерах нет ⇒ permissioned-варианта быть не может;
permission-check в Solidity ревертит, а мы видели скип».

Поставлен опровергающий тест максимальной силы — **тот же фид HYPE, тот же приём, два
адаптера** (принятые пуши релейеров, реплей на их же блоках, меняется только отправитель):

| отправитель | адаптер A (blk 42378240) | адаптер B (blk 42378225) |
|---|---|---|
| релейер #1 `0x2327c3cd…` | лёг | лёг |
| релейер #2 `0xe08496b4…` | лёг | лёг |
| контракт лидера `0x7a0ea1f7…` | **лёг** | **скип** |
| наш контракт `0x0cf80b56…` | скип | скип |

Один и тот же адрес, один и тот же фид, пакеты одинаковой свежести и девиации: на A кладёт,
на B — нет. Ни одна гипотеза «условий по данным» (устаревание, интервал, девиация, порог
подписантов — 3 на обоих) этого объяснить не может. Совпадает ровно с байткодом: `0x7a0ea1f7`
есть константой в `A_impl`, в `B_impl` его нет.

Два довода возражения проверены и не подтвердились:
1. **Админ-поверхность есть** — у ОБОИХ прокси общий ProxyAdmin `0x663b50c9…` (1690Б кода).
   Список апдейтеров не имеет сеттера именно потому, что вшит в имплементацию: меняется
   апгрейдом, а не транзакцией. Отсутствие `owner()` на имплементации — следствие, не улика.
2. **«Permission ревертит»** — конвенция, а не поведение этого контракта: у него отказ по
   отправителю выходит через `UpdateSkipDueToBlockTimestamp`, измерено напрямую.

Попутно: тезис «адаптер A отдаёт только HYPE» неверен — A держит минимум 9 живых фидов
(HYPE, BTC, UBTC, SOL, USDC, USDT, kHYPE_FUNDAMENTAL/USD, USDe, sUSDe). Реверт на `kHYPE`
означает, что этого ИМЕНИ фида на A нет (есть `kHYPE_FUNDAMENTAL/USD`), а не что адаптер
односоставный. Операционная поправка ревьювера при этом верна и остаётся в силе: реверт по
отсутствующему фиду нельзя путать со скипом при приёмке канареек.

**Следствие для программы:** v4 pre-leg, predict-lite как триггер огня и разворот
`HL_SPEC_FIRE` в self-push строить НЕ на чем. Живым остаётся слой-потребитель чужого пуша.

## Сверка конспекта владельца 05.08 — три поправки по числам

**1. «Таймерный класс — 38% потока, это наш хлеб» → 38% ВЕРНО ПО ШТУКАМ, но это $7/мес.**
Замер 8/21 (38%) — доля СОБЫТИЙ без пуша в окне. На 30-дневном скане (`liq30d.json`, 79
ликвидаций, $10,247) картина по деньгам:

| порог приза | всего | атомарных $ | реактивных $ |
|---|---|---|---|
| ≥$0 | 74 | 10,240 | **7** |
| ≥$1 | 17 | 10,235 | 3 |
| ≥$10 | 9 | 10,211 | **0** |
| ≥$1000 | 4 | 9,623 | **0** |

Доля без пуша: **54% по штукам, 0.1% по деньгам**. Крупнейший реактивный приз за 30 суток —
**$2.99**. Таймер-класс на СЕГОДНЯШНЕЙ книге — пыль, делимая на 5+ конкурентов, а не хлеб.
Это свойство текущей книги (в ней нет позиций, дрейфующих к краю на ставке), а не потолок
класса — поэтому тень оправдана КАК ЗАМЕР, но как источник дохода не планируется.

**2. «В каскаде 10.10 ~30% ушло мимо лидера» → его там НЕ БЫЛО ВООБЩЕ (100%).**
Мимо прошло не 30%, а всё: сегодняшний атомарный оператор в том каскаде отсутствовал (поле
эволюционировало). Значит 10.10 — НЕ доказательство «он не успевает доесть каскад», а снимок
поля БЕЗ него. Тезис «хвост каскада наш» держится на аналогии, не на замере. Реальные числа
часа: **$407k приза / 113 ликвидаций** (день: $4.53M долга, ~$410k бонуса, 146 событий).
Чисел «$3.8M за час», «$642k», «$486k» в наших данных НЕТ — вероятно, спутан объём долга с
призом. Живой замер придёт с первым каскадом ПРИ нём; до тех пор тезис — гипотеза.

**3. «10 HYPE хватает на ~7 выстрелов в пиковом газе» → 5 по медиане, 1 по p90, 0 на пике.**
См. «Ширина залпа ограничена конвертом» выше.

**Подтверждено независимо:** весь жир читает адаптер A, где лидер в белом списке (`bot/spec.py`
DRIVERS: WHYPE/wstHYPE/kHYPE/beHYPE/UBTC/USDT0/USDe/USDC/USDH/sUSDe/USOL → A). Адаптер B, где
лидера в списке НЕТ, кормит только **UETH** и **PT-kHYPE-19MAR2026**. PREARM_MAX=6 — верно.

## 05.08 КАСКАДНЫЙ ТЕЗИС НЕ ПОДТВЕРЖДЁН — два замера против него

Вопрос владельца: «нет гарантий, что в каскад/шторм всё со стола не заберут зашитые в белые
списки лидеры». Проверено — гарантий действительно нет, и оба довода «за» рассыпались.

**1. Пропускная способность — не его ограничение (запас 99.4%).** HyperEVM = 0.983 с/блок
(61 блок/мин, замер 1000 блоков). При «≤2 ликвидации в блок, 1 жертва на tx» потолок =
**122 ликв/мин**. Каскад 10.10.2025 шёл со скоростью **1.88 ликв/мин** (113 событий/час,
ВСЕ 9 победителей вместе), топ выбирал **0.77 ликв/мин** = **0.6% своего потолка**. Довод
«он не батчит ⇒ не успеет доесть каскад» арифметически неверен: он мог взять весь тот час
в одиночку при загрузке ~1.5%. Единственная причина, по которой 85% ушло с лагом ≥5 блоков —
его в том каскаде НЕ БЫЛО.

**2. Капиталом он тоже не ограничен — он на флешлоуне, как и мы.** Сначала замер сказал
обратное (контракты лидера держат $0, событие Aave FlashLoan не найдено) — **вывод был мой
и он был ЛОЖНЫМ**: искал topic0 события Aave **v2**. Разбор трансферов крупнейшей победы
(`0xaffcc674…`, $3,755): UETH 9.3387 приходит с aToken `0xdba3b256…`, возвращается 9.3424 —
премия **0.0396% = ровно flashLoanSimple 0.04%**, и POOL эмитит topic
`0xefefaba5e921573100900a3ad9cf29f222d995fb3b6045797eaea7521bd8d6f0` (Aave v3 FlashLoan).
Урок повторный: **отсутствие события ≠ отсутствие механизма**, если сигнатуру подобрал по
памяти. Позитивный контроль здесь — арифметика премии, а не поиск лога.

**Вывод.** У лидера нет ни потолка пропускной способности, ни потолка капитала, и есть
доступ, которого нет у нас. Разрыва, который мы собирались эксплуатировать в каскаде, в
данных НЕТ. Тезис «хвост каскада наш» держится только на 10.10 — снимке поля БЕЗ него.
⇒ **Поднимать капитал под этот тезис нельзя.** Слой остаётся дешёвым лотерейным билетом на
том, что уже вложено; наращивать вложение — только после доказательства разрыва на живом
каскаде ПРИ нём.

### Если ширина залпа всё же нужна — она берётся бесплатно, не деньгами

Конверт = `GAS_LIMIT × maxFee`, где `maxFee = 2×base + tip` (двойка ЗАШИТА, executor.py:789).
Наш собственный замер расхода — **1,384,844 газа** (двуногий форк-экзамен, $464k долга,
`twoleg_result.json`); максимум у конкурентов на живых чеках 1,761,683, медиана 937,336.
`GAS_LIMIT=2.5M` = **1.81× нашего замера** — запас, за который мы платим шириной залпа:

| вариант | конверт @375 gwei | @1073 | залп на 10.04 HYPE | полный залп 6 |
|---|---|---|---|---|
| сейчас 2.5M × 2.0 | 2.000 HYPE | 5.49 | 5 | 12.0 HYPE — НЕ помещается |
| 2.0M × 2.0 | 1.600 | 4.39 | 6 | 9.6 |
| **2.0M × 1.5** | **1.225** | **3.32** | **8** | **7.4 — помещается** |
| 1.8M × 1.5 | 1.103 | 2.99 | 9 | 6.6 |

`2.0M × 1.5` даёт полный залп PREARM_MAX=6 **на уже имеющиеся 10 HYPE** и втрое больше
выстрелов на p90. Цена — два реальных риска: узкий `maxFee` застревает, если база скакнёт
между подписью и включением (в каскад она скачет), узкий `GAS_LIMIT` даёт OOG на тяжёлом
выходе (запас над нашим замером падает 1.81×→1.44×). ЖДЁТ ПОДПИСИ kelbic: это огневая
политика, и я её не трогал.

## 05.08 вечер: ПЕРЕПРОВЕРКА выводов по hyperlend (независимыми путями)

По запросу владельца все опорные выводы перемерены заново, другим маршрутом, чем получены.

**1. Байткод-гейт — подтверждён свежим чтением цепи.** Impl-слоты EIP-1967 перечитаны:
A=`0x84c698e6…` (8712Б), B=`0x131141e6…` (8416Б), общий ProxyAdmin `0x663b50c9…`.
Лидер-исполнитель `0x7a0ea1f7…` (полный адрес взят из tx.to его крупнейшей победы, не из
памяти): в A — ЕСТЬ, в B — НЕТ. Релейеры из нашего журнала пушей `0x2327c3cd…`/`0xe08496b4…`:
в ОБОИХ. Нюанс, объясняющий whoCanPush: EOA лидера `0x84d5e280…` в списках НЕТ нигде — в
белом списке его КОНТРАКТ, поэтому пуш от его EOA скипается, от контракта ложится.

**2. Флешлоун лидера — подтверждён честным keccak + целочисленным декодом.** Сигнатуры
посчитаны заново: v3 `FlashLoan(...uint8...)` = `0xefefaba5…` (есть в его tx), v2 = 
`0x631042c8…` (то, что я искал в первый раз — механизм моей ошибки воспроизведён). Декод
события со верным смещением слов (data[0]=initiator, не amount!): initiator=`0xae86edb5…`,
asset=UETH, amount=9.338702, premium=0.003735 ⇒ ставка ровно **0.0400%**. Первый декод в
этой же перепроверке тоже был кривым (premium из слова №2 дал 0%) — смещения слов событий
проверять по ABI, не на глаз.

**3. Скан 30 суток — агрегаты пересчитаны из сырья, окно проверено датами.** Окно ровно
30.0 сут (блоки 39739768..42374985 = 06.07 13:19 – 05.08 13:24 UTC); первая ликвидация
13.07 — первая неделя июля ПУСТАЯ, «22.5 сут» это разлёт событий, не окна. 79 строк,
$10,247. **Уточнение формулировки**: «99.6% атомарно» смешивало два числа. Точно так:
события ≥$10 несут 99.6% всех денег ($10,211/$10,247), и внутри них атомарны **100%**
(9 из 9). Реактивных ≥$10 — ноль штук, $0. Жертв на tx: max 1; ликвидаций на блок: max 2.
5 строк с бонусом −$0.00 — пыль округления.

**4. Классификатор atomic — проверен приёмкой.** Топ-3 атомарных: лог адаптера в самой tx
есть 3/3. Контроль-якорь `0x6e9cce69…` помечен atomic=False — и это ВЕРНО (прямой вызов
пула, 13 логов, адаптера нет): классификатор не красит всё в атомарное.

**5. Пропускная и конверт — арифметика повторена.** Блок-тайм 0.9836с и по 1k, и по 10k
блоков; потолок ≤2/блок = 122 ликв/мин против 1.88/мин всего рынка в каскад 10.10 — запас
лидера ≥98.5%, причём «≤2/блок» это его наблюдённый СПРОС, не доказанный потолок (реальный
потолок выше ⇒ вывод только крепнет). Конверт нечувствителен к допущению tip: при base 375
tip=50 → 2.000 HYPE (залп 5), tip=1 (типичный каскадный) → 1.877 HYPE (залп ТЕ ЖЕ 5).
EOA 10.036813 HYPE, `CAP_COUNT_WINS` дефолт «0» — успех возвращает газ в бюджет (замок-тест
на месте).

**Вердикт перепроверки: все выводы стоят.** Поправлена одна формулировка (99.6%/100% в п.3)
и найдены два инструментальных урока: (а) сигнатуру события — только keccak'ом от полной
ABI-строки, никогда по памяти; (б) смещения слов в data — только по ABI (initiator-адрес
на месте amount дал бы «премия 0%» и ложное «свой капитал» ВТОРОЙ раз подряд).
