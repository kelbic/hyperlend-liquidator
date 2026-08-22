"""Offline unit tests for the shadow-race notify gate (no network). Run:
PYTHONPATH=. python3 bot/test_shadow.py

Covers the 2026-08-07 triage fix (block 42516651): the inbox wake-up threshold is the FULL
fire floor (MIN_DEBT_USD AND MIN_PROFIT_USD — dd19a4e canon), not MIN_PROFIT alone. A $363-debt
race cleared the $25 profit floor and woke the agent for a position the bot deliberately
excludes via HL_MIN_DEBT_USD=500 — sub-filter races stay in the jsonl but must not wake.
Also covers the per-tx oracle-update counter (Pyth PriceFeedUpdate / RS ValueUpdate): the
42516651 winner carried the Pyth price update INSIDE its own liquidation tx (atomic self-push).
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from bot import config as C          # noqa: E402
from bot import shadow as S          # noqa: E402


def test_fire_floor_gate() -> None:
    lo_debt = C.MIN_DEBT_USD - 1
    hi_debt = C.MIN_DEBT_USD + 1
    lo_bonus = C.MIN_PROFIT_USD - 1
    hi_bonus = C.MIN_PROFIT_USD + 1
    # оба порога взяты — будим
    assert S._above_fire_floor(hi_debt, hi_bonus)
    # ровно на порогах — будим (>=)
    assert S._above_fire_floor(C.MIN_DEBT_USD, C.MIN_PROFIT_USD)
    # прецедент 42516651: бонус выше пола, долг ниже размерного фильтра — НЕ будим
    if C.MIN_DEBT_USD > 363.08:
        assert not S._above_fire_floor(363.08, 26.14)
    assert not S._above_fire_floor(lo_debt, hi_bonus)
    # долг взят, бонус мал — НЕ будим
    assert not S._above_fire_floor(hi_debt, lo_bonus)
    # цена не добыта (None, как в записях 04.08 по USDHL до фикса символов) — НЕ будим
    assert not S._above_fire_floor(None, hi_bonus)
    assert not S._above_fire_floor(hi_debt, None)
    assert not S._above_fire_floor(None, None)
    # нулевые значения не проходят bool-гейт
    assert not S._above_fire_floor(0.0, hi_bonus)


def test_oracle_update_topics() -> None:
    # константа Pyth сверена вычислением keccak по сигнатуре события
    from analysis.keccak import event_topic0
    assert S.TOPIC_PYTH_PRICE_UPDATE == event_topic0(
        "PriceFeedUpdate(bytes32,uint64,int64,uint64)")
    assert C.TOPIC_VALUE_UPDATE in S._TOPIC_ORACLE_UPDATES
    assert S.TOPIC_PYTH_PRICE_UPDATE in S._TOPIC_ORACLE_UPDATES


def test_upd_counting_shape() -> None:
    """Счётчик upd — та же свёртка, что в _enrich: топик[0] лога в множестве апдейтов."""
    logs = [{"topics": [S.TOPIC_PYTH_PRICE_UPDATE]},
            {"topics": [C.TOPIC_VALUE_UPDATE]},
            {"topics": ["0x" + "ab" * 32]},
            {"topics": []}]
    n = sum(1 for lg in logs if lg["topics"] and lg["topics"][0] in S._TOPIC_ORACLE_UPDATES)
    assert n == 2


# --------------------------------------------------------------------- вскрытие 22.08 (43827262)
# ФИКСТУРА — ЗАМЕР, не самоотчёт продукта: анатомия блока 43827262 прочитана с цепи
# (rpc.hyperlend.finance, полный листинг из 6 tx; drpc/официальный узел прячут системную
# tx HyperCore на idx 0 — см. _enrich). Ровно на ней прежняя эвристика push_idx промахнулась.
BLK_43827262 = [
    # системная tx HyperCore: gasPrice 0, gasUsed 0 — её не видят два из трёх узлов
    {"idx": 0, "to": "0xb88339cb7199", "from": "0x6b9e773128f4", "gas": 0, "st": 1,
     "tip_gwei": -5.28, "inb": 68, "upd": 0},
    {"idx": 1, "to": "0x1d90dbcf3072", "from": "0x830470bd27af", "gas": 841248, "st": 1,
     "tip_gwei": 8.98, "inb": 3524, "upd": 0},
    # НАСТОЯЩИЙ пуш RedStone: адаптер 0xe4ae8874, релейер 0x2327c3cd, ValueUpdate(HYPE)
    {"idx": 2, "to": "0xe4ae88743c38", "from": "0x2327c3cdc64c", "gas": 65951, "st": 1,
     "tip_gwei": 1.13, "inb": 581, "upd": 1},
    # победитель — ликвидация в ТОМ ЖЕ блоке, tip НИЖЕ пуша (техника lag=0)
    {"idx": 3, "to": "0x2f18fc900071", "from": "0xa8b4d313b3de", "gas": 617099, "st": 1,
     "tip_gwei": 0.10, "inb": 164, "upd": 0},
    # проигравший тем же контрактом с другого EOA — реверт по HF после победителя
    {"idx": 4, "to": "0x2f18fc900071", "from": "0x4dc3b0e827b0", "gas": 372106, "st": 0,
     "tip_gwei": 0.10, "inb": 164, "upd": 0},
    {"idx": 5, "to": "0x000000000000", "from": "0x0af2f002d3d3", "gas": 166718, "st": 1,
     "tip_gwei": 0.10, "inb": 1540, "upd": 0},
]


def _legacy_push_idx(anatomy):
    """Эвристика ДО фикса 22.08 — держится в тестах как НЕГАТИВНЫЙ КОНТРОЛЬ: если она на
    фикстуре даёт тот же ответ, что новая, фикстура дефект не воспроизводит и тест пуст."""
    from analysis.protocols import POOL
    return [r["idx"] for r in anatomy
            if r["st"] == 1 and r["inb"] >= 600 and 100_000 <= r["gas"] <= 600_000
            and r["to"].lower() != POOL[:14].lower()]


def test_push_idx_from_measured_update_event() -> None:
    # новая функция берёт ровно tx с оракул-событием
    assert S._push_indices(BLK_43827262) == [2]
    # НЕГАТИВНЫЙ КОНТРОЛЬ: прежняя эвристика на этой же фикстуре промахивалась в обе
    # стороны — теряла настоящий пуш (inb=581 < 600) и метила посторонний idx 5
    legacy = _legacy_push_idx(BLK_43827262)
    assert 2 not in legacy, "фикстура не воспроизводит промах — тест бесполезен"
    assert legacy == [5]


def test_push_idx_sees_atomic_self_push() -> None:
    """Прецедент 07.08 (42516651): победитель везёт апдейт Pyth в СВОЕЙ же tx — его
    индекс обязан попасть в push_idx, иначе «атомарный self-push» снова читается как
    «пуша рядом не было»."""
    anatomy = [{"idx": 0, "to": "0xabcdebae0000", "from": "0xabcdebae0000", "gas": 500_000,
                "st": 1, "tip_gwei": 1.4, "inb": 900, "upd": 1}]
    assert S._push_indices(anatomy) == [0]


def test_push_idx_empty_when_no_oracle_tx() -> None:
    assert S._push_indices([r for r in BLK_43827262 if not r["upd"]]) == []


def test_bonus_measured_beats_model() -> None:
    """Замер (seized-cover) — эталон; модель остаётся фолбэком без цены залога."""
    debt_usd, seized_usd = 18455.43, 20946.91          # с цепи, блок 43827262
    v, src = S._bonus_usd(debt_usd, seized_usd, "wstHYPE")
    assert src == "measured" and abs(v - 2491.48) < 1.0
    # цену залога добыть не удалось -> модель, помеченная как модель
    v2, src2 = S._bonus_usd(debt_usd, None, "wstHYPE")
    assert src2 == "modelled" and v2 > 0
    # ни того ни другого -> None, и такой бонус не будит (гейт _above_fire_floor)
    v3, src3 = S._bonus_usd(None, None, "wstHYPE")
    assert v3 is None and src3 == "none" and not S._above_fire_floor(None, v3)
    # отрицательной премии не бывает: сейз меньше погашенного -> 0, не будит
    v4, _ = S._bonus_usd(1000.0, 900.0, "wstHYPE")
    assert v4 == 0.0 and not S._above_fire_floor(1000.0, v4)


def test_enrich_idx_from_receipt_and_block_meta() -> None:
    """Шов проверяется НА САМОЙ _enrich, не на помощнике: узел отдаёт листинг блока без
    системной tx, а квитанции несут каноничные индексы — запись обязана взять индексы
    квитанций и поднять idx_shift."""
    from analysis.protocols import ORACLE

    base = 5_275_000_000
    # листинг узла: 5 tx (системная скрыта), квитанции нумеруют от 0..5 (каноничные)
    hidden = {0: None}
    listed = [r for r in BLK_43827262 if r["idx"] != 0]
    txs = [{"hash": "0x%064x" % r["idx"], "transactionIndex": hex(i),
            "to": r["to"], "from": r["from"], "input": "0x" + "ab" * r["inb"]}
           for i, r in enumerate(listed)]
    by_hash = {t["hash"]: r for t, r in zip(txs, listed)}

    class FakeRpc:
        def get_block(self, n, full=False):
            return {"baseFeePerGas": hex(base), "transactions": txs}

        def call(self, method, params):
            r = by_hash[params[0]]
            return {"transactionIndex": hex(r["idx"]), "gasUsed": hex(r["gas"]),
                    "status": hex(r["st"]),
                    "effectiveGasPrice": hex(base + int(r["tip_gwei"] * 1e9)),
                    "logs": [{"address": "0xe4ae88743c3834d0c492eabc47384c84bcadc6a6",
                              "topics": [C.TOPIC_VALUE_UPDATE]}] * r["upd"]}

        def eth_call(self, to, data, tag="latest"):
            assert to == ORACLE
            return hex(int(81.318638 * 1e8))          # wstHYPE, обе ноги события

    S._price_cache.clear()
    written, woke = [], []
    orig_append, orig_notify = S._append, S._notify_inbox
    S._append = written.append
    S._notify_inbox = lambda text, key: woke.append((text, key))
    try:
        ev = {"block": 43827262,
              "tx": "0x%064x" % 3,                     # победитель
              "coll": "0x94e8396e0869c9f2200760af0621afd240e1cf38",
              "debt": "0x94e8396e0869c9f2200760af0621afd240e1cf38",
              "victim": "0xaf421e572b76d6c4596d4fea0faee7e742706dfa",
              "cover": 226952081207627508270, "seized": 257590612170657221887,
              "liquidator": "0x2f18fc900071bb73b6b2e73d910f3ee154f1a0ab"}
        S._enrich(FakeRpc(), [ev], {"in_book": {}, "in_hot": {}},
                  {"balance_hype": 0.156803, "guard": "ok"})
    finally:
        S._append, S._notify_inbox = orig_append, orig_notify
        S._price_cache.clear()

    assert len(written) == 1 and "err" not in written[0], written
    rec = written[0]
    # индексы — каноничные (из квитанций), несмотря на укороченный листинг
    assert [r["idx"] for r in rec["block_txs"]] == [1, 2, 3, 4, 5]
    assert rec["idx_shift"] is True, "съезд листинга и квитанций обязан быть виден в данных"
    assert rec["ntx"] == 5 and rec["trunc"] is False
    # пуш опознан по событию, победитель — по хэшу
    assert rec["push_idx"] == [2]
    assert rec["win"]["idx"] == 3 and rec["win"]["upd"] == 0
    # премия — ЗАМЕР
    assert rec["bonus_src"] == "measured"
    assert abs(rec["bonus_usd_est"] - 2491.5) < 2.0
    assert abs(rec["debt_usd"] - 18455.4) < 2.0
    assert len(woke) == 1 and "43827262" in woke[0][0]

    # НЕГАТИВНЫЙ КОНТРОЛЬ шва: узел без съезда не поднимает флаг
    S._price_cache.clear()
    written2 = []
    S._append, S._notify_inbox = written2.append, lambda *a, **k: None
    try:
        for i, t in enumerate(txs):
            by_hash[t["hash"]] = dict(by_hash[t["hash"]], idx=i)
        S._enrich(FakeRpc(), [ev], {"in_book": {}, "in_hot": {}}, {"balance_hype": 0.1, "guard": "ok"})
    finally:
        S._append, S._notify_inbox = orig_append, orig_notify
        S._price_cache.clear()
    assert written2[0]["idx_shift"] is False


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok {t.__name__}")
    print(f"{len(tests)}/{len(tests)} shadow tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())


# --- floor_ok: три состояния = три значения (разбор гонки 43838096, 22.08) --------------
def test_floor_ok_three_states():
    """Прежнее `(...) or None` НЕ МОГЛО вернуть False: законный «газа не хватило»
    приходил в jsonl как null и был неотличим от «баланс не прочитан». Тест держит
    именно средний случай — он и есть весь смысл правки."""
    from bot import shadow, config as C

    base_storm = 825_071_739_814                       # base блока 43838096, 825.07 gwei
    envelope = C.GAS_LIMIT * 2 * base_storm / 1e18     # 4.13 HYPE при GAS_LIMIT=2.5M

    # 1) баланс не прочитан -> None (единственный законный None)
    assert shadow._floor_ok(None, base_storm) is None

    # 2) боевой поплавок 20.08 против шторма -> ИМЕННО False, не None
    assert shadow._floor_ok(0.156803, base_storm) is False

    # 3) хватает -> True
    assert shadow._floor_ok(envelope * 1.01, base_storm) is True

    # 4) тихий фон (11.5 gwei): тот же поплавок вооружён
    assert shadow._floor_ok(0.156803, 11_500_000_000) is True


def test_floor_ok_negative_control():
    """Негативный контроль К САМОМУ ТЕСТУ: воспроизводим СНЯТЫЙ дефект и убеждаемся,
    что тест выше его ловит. Иначе суита зелена и с фиксом, и без него."""
    from bot import config as C

    base_storm = 825_071_739_814
    bal = 0.156803
    buggy = (bal is not None and bal * 1e18 >= C.GAS_LIMIT * 2 * base_storm) or None
    assert buggy is None                                # старое поведение: False -> None
    assert buggy is not False                           # ...и оно провалило бы кейс (2)
