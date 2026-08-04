"""HyperLend liquidator configuration (HL_ env prefix). Code defaults are SAFE: DRY_RUN on, no
contract set. The executor only sends when DRY_RUN=0 AND HL_CONTRACT is set. Same env-loading
idiom as the wc/katana bots — the same code runs in dry-run, fork, and live without edits.
"""
from __future__ import annotations
import os

from analysis.protocols import (
    ADDRESSES_PROVIDER, ORACLE, POOL, POOL_DATA_PROVIDER, FLASHLOAN_PREMIUM_BPS,
)

CHAIN_ID = 999  # HyperEVM
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(REPO, "data")

# --- RPC ---------------------------------------------------------------------------------------
# Read endpoint(s): rotated on failure (keep head/logs/reaction spread across these like katana).
# Write endpoint: where the signed tx is broadcast (defaults to the official node).
READ_RPCS = [r.strip() for r in os.environ.get(
    "HL_READ_RPCS",
    "https://hyperliquid.drpc.org,https://rpc.hyperliquid.xyz/evm,https://rpc.hyperlend.finance",
).split(",") if r.strip()]
RPC_WRITE = os.environ.get("HL_RPC", os.environ.get("HL_RPC_WRITE", "https://rpc.hyperliquid.xyz/evm"))

# --- core protocol addresses (verified on-chain — see analysis/protocols.py) -------------------
POOL_ADDR = POOL
ADDRESSES_PROVIDER_ADDR = ADDRESSES_PROVIDER
ORACLE_ADDR = ORACLE
DATA_PROVIDER_ADDR = POOL_DATA_PROVIDER
FLASH_PREMIUM_BPS = FLASHLOAN_PREMIUM_BPS

# --- executor knobs (env-overridable) ----------------------------------------------------------
DRY_RUN = os.environ.get("DRY_RUN", "1") != "0"            # default: DO NOT send
CONTRACT = os.environ.get("HL_CONTRACT", "")               # deployed HyperLendLiquidator (req to fire)
# 04.08 («го на все»): в бою контракт v2 (0x5C20F458…) — пропускает своп при coll==debt. Этот флаг
# открывает same-asset классу путь через бота (~$44k исторического бонуса был недостижим). ОТКАТ
# HL_CONTRACT на v1 (0xCBAB63AA…) ОБЯЗАН сопровождаться HL_SAME_ASSET=0: v1 свопает безусловно,
# и same-asset выстрел на нём ревертится (маршрута в себя не существует).
SAME_ASSET = os.environ.get("HL_SAME_ASSET", "1") == "1"
PRIVATE_KEY = os.environ.get("HL_PRIVATE_KEY", "")
KEYFILE = os.path.expanduser(os.environ.get("HL_KEYFILE", "~/.hyperlend-bot/key"))
if not PRIVATE_KEY and os.path.exists(KEYFILE):
    PRIVATE_KEY = open(KEYFILE).read().strip()

# path: flash (zero-capital, default) vs capital (contract pre-funded with the debt asset)
USE_FLASHLOAN = os.environ.get("HL_USE_FLASHLOAN", "1") != "0"

MIN_PROFIT_USD = float(os.environ.get("HL_MIN_PROFIT", "25"))   # off-chain net floor AND on-chain gate
MIN_DEBT_USD = float(os.environ.get("HL_MIN_DEBT_USD", "500"))  # ignore dust positions
MAX_IMPACT = float(os.environ.get("HL_MAX_IMPACT", "0.05"))     # skip if LiquidSwap price impact > this
WATCH_HF = float(os.environ.get("HL_WATCH_HF", "1.10"))         # refine reserves below this HF
REPORT_HF = float(os.environ.get("HL_REPORT_HF", "1.15"))

# 04.08 ПРЕМИССА «latency-FCFS, no priority auction» ОПРОВЕРГНУТА замером (STATE 04.08):
# малые блоки HyperEVM упорядочены по tip убыв. (нарушения — только нонс-цепочки одного
# отправителя), победители часа раздачи 10.10 платили 80–1,225 gwei, наш tip=0 ставит нас
# ПОСЛЕДНИМИ в любом спорном блоке. При этом переплата вредна: оракул-push сам является tx
# с tip, и tx с tip ВЫШЕ пуша исполняется ДО обновления цены — реверт (реверты 10.10 платили
# 13–15k gwei). Политика: tip = TIP_PRIZE_FRAC от приза, зажатый в [TIP_MIN, TIP_MAX].
# Приор [5, 1000] gwei — из эмпирики победителей; уточняется shadow-телеметрией.
# ОТКАТ: HL_TIP_MODE=off — прежнее поведение (PRIORITY_GWEI, по умолчанию 0).
PRIORITY_GWEI = float(os.environ.get("HL_PRIORITY_GWEI", "0"))
TIP_MODE = os.environ.get("HL_TIP_MODE", "auto")                 # auto | off
TIP_MIN_GWEI = float(os.environ.get("HL_TIP_MIN_GWEI", "5"))
TIP_MAX_GWEI = float(os.environ.get("HL_TIP_MAX_GWEI", "1000"))
TIP_PRIZE_FRAC = float(os.environ.get("HL_TIP_PRIZE_FRAC", "0.05"))   # ≤5% приза на чаевые
# HARD gas limit — eth_estimateGas is unreliable/silently-passing on the flashloan+liq+swap
# callback path; NEVER estimate. Generous enough for flashLoanSimple -> liquidationCall -> swap
# -> repay -> sweep.
GAS_LIMIT = int(os.environ.get("HL_GAS_LIMIT", "2500000"))
GAS_UNITS_EST = int(os.environ.get("HL_GAS_UNITS", "1500000"))  # for the gas-USD kill-switch only
HYPE_USD = float(os.environ.get("HL_HYPE_USD", "45"))           # rough, gas-USD estimate only
# --- EOA gas-balance guard (katana lesson, ported 24.07) ---------------------------------------
# Гарда НЕ БЫЛО ВООБЩЕ: осушённый кошелёк вскрылся бы только штормом «insufficient funds»
# посреди каскада — то есть ровно тогда, когда бот обязан стрелять. Нода отвергает tx, если
# balance < GAS_LIMIT*maxFeePerGas (ПОЛНЫЙ конверт комиссии, не фактический расход).
# Пол готовности = max(один конверт, BALANCE_FIRES выстрелов по текущей базе).
# Стоит один eth_getBalance + один заголовок раз в BALANCE_CHECK_SEC.
BALANCE_CHECK_SEC = float(os.environ.get("HL_BALANCE_CHECK_SEC", "600"))
BALANCE_ALERT_SEC = float(os.environ.get("HL_BALANCE_ALERT_SEC", "3600"))
BALANCE_FIRES = int(os.environ.get("HL_BALANCE_FIRES", "3"))

POLL_SEC = float(os.environ.get("HL_POLL_SEC", "3"))            # base cadence (legacy once-loop only)
# --- amortized hot-set loop (see bot/executor.py loop()) ---------------------------------------
# The loop no longer blocks on a full-book sweep. Each iteration it (a) re-reads the HF of the
# HOT SET (borrowers with HF < HOT_HF) so a cross to <1 is caught within one iteration, and (b)
# advances a rolling full-book cursor by SWEEP_CHUNK borrowers to refresh hot-set membership. A
# compact status line is logged EVERY iteration, so the log is never silent for more than one
# cadence — which is what makes the 600s deadman stop false-firing.
HOT_POLL_SEC = float(os.environ.get("HL_HOT_POLL_SEC", "2"))    # sleep between iterations (~2-3s)
# HOT_HF: hot-set membership ceiling. Measured HF distribution (debt>=$500, book of ~25.5k, 7404
# with debt) 2026-07-17: <1.15=65, <1.30=202, <1.50=377, <2.0=722. Default 1.30 keeps the hot
# set ~200 (a few hundred, polled in ~1.5-2.5s) and catches any position within a ~23% burst of
# the line at hot-poll cadence. Widen toward 1.50 (377) before an anticipated mega-crash — the
# cost is a slightly larger hot poll. Positions healthier than HOT_HF that crash in are caught
# within one full-book cycle (the rolling sweep), a few minutes — the documented residual latency.
HOT_HF = float(os.environ.get("HL_HOT_HF", "1.30"))
# SWEEP_CHUNK: borrowers scanned per iteration by the rolling full-book cursor. On the fast
# official node a 500-call getUserAccountData aggregate3 is ~1.2s; unioned with the hot set the
# per-iteration read stays ~2 round-trips / a few seconds (< the 8s budget). 25.5k / 500 = ~51
# iterations, so the full book is re-swept every ~51*(work+HOT_POLL_SEC) ≈ 3-4 min continuously.
SWEEP_CHUNK = int(os.environ.get("HL_SWEEP_CHUNK", "500"))
# SWEEP_EVERY: катить курсор не каждую итерацию, а раз в N. Замер 30.07: итерация читает
# hot(~220) ∪ chunk(500) = ~720 аккаунтов за 1.1–1.4с, а блок HyperEVM идёт ~1с — мы опрашивали
# горячих МЕДЛЕННЕЕ, чем движется чейн, и пересечение HF<1 могло прожить целый блок незамеченным.
# При N=3 две итерации из трёх читают только hot (~0.35с), и горячая позиция перечитывается
# примерно вдвое чаще. Плата — полный цикл книги растягивается (~56с → ~90с), но это дёшево:
# потолок hot-set HOT_HF=1.30 очень щедрый, чтобы выпасть из наблюдения, позиции надо рухнуть
# с 1.30 до 1.0 внутри одного цикла. N=1 возвращает прежнее поведение ровно.
SWEEP_EVERY = int(os.environ.get("HL_SWEEP_EVERY", "3"))
# Cursor + hot-set persist here (repo data dir, NOT ~/.hyperlend-bot) so a restart resumes
# mid-book with a warm hot set instead of re-scanning from zero.
HOTSET_FILE = os.environ.get("HL_HOTSET_FILE", os.path.join(DATA_DIR, "hotset.json"))

# --- transport hardening (a single black-holing endpoint must never wedge the single-threaded
# loop). The stdlib socket `timeout` is per-recv, not total: a trickling/half-open response can
# block far past it (reproduced live — a 400-call multicall to one endpoint took 33s under a 25s
# socket timeout). So each RPC attempt also runs under a HARD total wall deadline (worker thread),
# and any endpoint that times out / hangs / 429s is benched and the next tried. -----------------
RPC_TIMEOUT = float(os.environ.get("HL_RPC_TIMEOUT", "8"))        # per-attempt socket timeout (s)
RPC_HARD_TIMEOUT = float(os.environ.get("HL_RPC_HARD_TIMEOUT", "10"))  # hard total wall cap (s)
RPC_RETRIES = int(os.environ.get("HL_RPC_RETRIES", "3"))         # attempts (~one per endpoint)
RPC_BENCH_SEC = float(os.environ.get("HL_RPC_BENCH_SEC", "30"))  # bench a failed/hung endpoint
# Успешный-но-медленный ответ бенчит узел (порт midnight slow_sec, 23.07: официальный узел
# отвечал 0.8-1.3с под multicall-нагрузкой против 0.27-0.49с у соседей и никогда не бенчился).
# Дефолт 3с консервативен под наши 500-вызовные чанки (~112KB: здоровый узел ≤1.2с, нагруженный
# легитимно до ~2.5с); 0 = выключить.
RPC_SLOW_SEC = float(os.environ.get("HL_RPC_SLOW_SEC", "3"))

# kill-switch / dedup
MAX_DAILY_GAS_USD = float(os.environ.get("HL_MAX_DAILY_GAS_USD", "5"))
# 04.08 («го» владельца): кап ограничивает УБЫТОК, не оборот. Успешный выстрел окупает свой газ
# призом в той же tx, поэтому его settle ВОЗВРАЩАЕТ заряд в суточный бюджет; в кап копятся только
# реверты/потерянные. Старая семантика (успех тоже жёг бюджет) глушила бота после первого же
# успеха каскадного часа (~$75-120 газа при base 375+ gwei > дневных $50). Откат = HL_CAP_COUNT_WINS=1.
CAP_COUNT_WINS = os.environ.get("HL_CAP_COUNT_WINS", "0") == "1"
MAX_CONSEC_REVERTS = int(os.environ.get("HL_MAX_CONSEC_REVERTS", "3"))
DEDUP_SEC = float(os.environ.get("HL_DEDUP_SEC", "60"))
# a REVERTED target must NOT be retried next pass (3 quick reverts on one bad target would trip
# the kill-switch) — block it for this cooldown instead
REVERT_COOLDOWN_SEC = float(os.environ.get("HL_REVERT_COOLDOWN_SEC", "300"))
HEARTBEAT_SEC = float(os.environ.get("HL_HEARTBEAT_SEC", "0"))  # OFF by default (quiet cadence)

RAW_TX = os.environ.get("HL_RAW_TX", "0") == "1"               # 0 = cast (default), 1 = eth_account

# --- ярус 1 гонки (04.08, GO владельца; каждая ручка — самостоятельный откат) -------------------
# Параллельный залп: eth_sendRawTransaction во ВСЕ write-эндпоинты одновременно, первый ack
# побеждает (порт e94b940 c Base: spawn 4мс vs 70-140мс; здесь — страховка от 429 одного узла
# в штормовой час, мы сами ловили -32005 на официальном узле в штиль). ОТКАТ: =0 (один RPC_WRITE).
PARALLEL_BROADCAST = os.environ.get("HL_PARALLEL_BROADCAST", "1") == "1"
BROADCAST_RPCS = [r.strip() for r in os.environ.get(
    "HL_BROADCAST_RPCS",
    RPC_WRITE + "," + ",".join(u for u in READ_RPCS if u != RPC_WRITE)).split(",") if r.strip()]
# Nonce prewarm: pending-nonce обновляется фоном раз в N секунд, выстрел берёт кэш вместо
# блокирующего RPC (~50-250мс с трассы). Инварианты WC-урока (send_ts пишет ТОЛЬКО
# _nonce_after_send, chain-вид не благословляет локальный бамп) не тронуты. ОТКАТ: =0.
NONCE_PREWARM_SEC = float(os.environ.get("HL_NONCE_PREWARM_SEC", "15"))
# Кэш baseFee для _fee_params: свежее чтение итерации (~1-3с) вместо RPC на пути выстрела.
# Старше BASEFEE_CACHE_SEC — как раньше, живой запрос. ОТКАТ: =0.
BASEFEE_CACHE_SEC = float(os.environ.get("HL_BASEFEE_CACHE_SEC", "2.5"))
# Shadow-race телеметрия: каждая ЧУЖАЯ ликвидация пула -> data/shadow_races.jsonl (кто взял,
# каким tip, каким индексом в блоке, был ли оракул-push рядом, видели ли МЫ жертву и прошли
# ли бы гарды). Единственный источник данных для тюнинга tip-политики и вилки predict.
# ОТКАТ: =0 (не влияет на горячий цикл — тяжёлая часть в daemon-потоке).
SHADOW = os.environ.get("HL_SHADOW", "1") == "1"
SHADOW_EVERY_SEC = float(os.environ.get("HL_SHADOW_EVERY_SEC", "60"))
SHADOW_FILE = os.environ.get("HL_SHADOW_FILE", os.path.join(DATA_DIR, "shadow_races.jsonl"))
SHADOW_CKPT = os.environ.get("HL_SHADOW_CKPT", os.path.join(DATA_DIR, "shadow_ckpt.json"))

# --- каденс после апдейта (04.08, после поправки 4237b15: поле берёт добычу через ~17с
# ПОСЛЕ апдейта цены — гонка решается скоростью обнаружения в этом окне, не спекуляцией) ---
# Событийный триггер: цикл слушает ValueUpdate обоих RedStone-адаптеров (1 дешёвый getLogs
# на итерацию, ~25мс на drpc-keep-alive); апдейт => немедленный hot-only проход без сна.
# ОТКАТ: HL_UPDATE_TRIGGER=0.
UPDATE_TRIGGER = os.environ.get("HL_UPDATE_TRIGGER", "1") == "1"
ORACLE_ADAPTERS = [a.strip() for a in os.environ.get(
    "HL_ORACLE_ADAPTERS",
    # 0xe4ae… = HYPE/BTC/USDT (+kHYPE_FUNDAMENTAL), 0x24c8… = ETH; связь доказана
    # getPriceFeedAdapter() у фасадов (STATE 04.08, поправка)
    "0xe4ae88743c3834d0c492eabc47384c84bcadc6a6,"
    "0x24c8964338deb5204b096039147b8e8c3aea42cc").split(",") if a.strip()]
TOPIC_VALUE_UPDATE = "0xf36866d965ee70c8632ff558f5cf8d41ee9ca1d0d0bc7700786e57be60747390"
# Pre-arm: для кромки hot-set (1.0 <= HF < PREARM_HF) выход котируется ЗАРАНЕЕ фоном; при
# пересечении HF<1 выстрел берёт кэш и не платит 1.7-3.5с LiquidSwap-квоты в горячий момент.
# Размер бронируется с бритьём PREARM_SHAVE (котировка на 97% от расчётного cover: дрейф
# позиции за TTL не должен опустить фактический seize ниже amountIn свопа — иначе реверт).
# Кэш применяется только при HF >= 0.95 (тот же close-factor режим, что при котировке).
# ОТКАТ: HL_PREARM=0.
PREARM = os.environ.get("HL_PREARM", "1") == "1"
PREARM_HF = float(os.environ.get("HL_PREARM_HF", "1.02"))
PREARM_TTL = float(os.environ.get("HL_PREARM_TTL", "45"))
PREARM_REFRESH_SEC = float(os.environ.get("HL_PREARM_REFRESH_SEC", "20"))
PREARM_MAX = int(os.environ.get("HL_PREARM_MAX", "2"))
PREARM_SHAVE = (97, 100)

# --- SPEC-FIRE: атомарный self-push + ликвидация (контракт v2 liquidateWithPush) -----------
# Тихое поле = ОДИН оператор, и ВСЕ его победы lag=0: он сам пушит подписанный RedStone-payload
# в адаптер и ликвидирует в той же tx (разбор гонки 04.08). Реактивный контур проигрывает ему
# структурно — триггер срабатывает, когда добыча уже взята тем же блоком. Паритет: армленная
# цель у кромки (1.0 <= HF < PREARM_HF), у которой СВЕЖАЯ подписанная цена гейтвея даёт
# HF_est < SPEC_HF_FIRE, стреляется через liquidateWithPush — пуш этой самой цены и ликвидация
# в одной транзакции. К РЕАКТИВНЫМ выстрелам (on-chain HF<1) пуш НЕ цепляется: свежая цена там
# способна «вылечить» жертву и сорвать наш же выстрел. Промах оценки стоит одного реверта
# (гейты контракта: HF-чек liquidationCall + min_profit) и учитывается kill-switch'ем.
# Карта актив->фиды и оба кэша (гейтвей + getLastUpdateDetails) живут в bot/spec.py.
# ОТКАТ: HL_SPEC_FIRE=0 — реактивный путь не меняется вовсе.
SPEC_FIRE = os.environ.get("HL_SPEC_FIRE", "1") == "1"
# Порог по оценочному HF: запас 0.2% на дрейф долей позиции и погрешность масштабирования
# (непокрытый остаток позиции считается неподвижным — формула в bot/spec.py).
SPEC_HF_FIRE = float(os.environ.get("HL_SPEC_HF_FIRE", "0.998"))
# Максимальный возраст подписанного пакета: адаптер терпит ~3 мин, но старый пакет = старая
# цена = оценка по ней уже не «свежее рынка» — 30с с запасом кроет каденс вотчера.
SPEC_MAX_AGE_MS = int(os.environ.get("HL_SPEC_MAX_AGE_MS", "30000"))
# Каденс вотчера: hot — у кромки есть армленные цели (кэш обязан быть тёплым к транзиту),
# cold — кромка пуста, кэш греется вполсилы (полный снапшот гейтвея ~350KB gzip за тик).
SPEC_POLL_HOT = float(os.environ.get("HL_SPEC_POLL_HOT", "1.5"))
SPEC_POLL_COLD = float(os.environ.get("HL_SPEC_POLL_COLD", "10"))

STATE_FILE = os.path.expanduser(os.environ.get("HL_STATE", "~/.hyperlend-bot/state.json"))
LOCK_FILE = os.path.expanduser(os.environ.get("HL_LOCK", "~/.hyperlend-bot/executor.lock"))

# Telegram (optional; same channel-file convention as the reference bots).
TG_ENV_FILE = os.path.expanduser("~/.claude/channels/telegram/.env")
TG_CHAT_ID = os.environ.get("HL_CHAT_ID", "265715923")
# The whole liquidator fleet posts into ONE chat, so every outgoing alert is prefixed with this
# short tag — without it an alert cannot be attributed to the bot that sent it.
BOT_TAG = os.environ.get("HL_BOT_TAG", "hyperlend")
