"""Тесты эскалационного гейта депег-вотча.

Гейт — чистая функция, тестируем её напрямую: живой main() трогает боевой инбокс
(канон [[tests-never-touch-production-channels]]) и пишет строку в историю.

Сценарий 07.08, из-за которого гейт появился: тревога 00:17Z (съедено 38%) разобрана
агентом, 02:17Z (съедено 34%, та же пила, тот же кит) подняла его снова — на менее
тяжёлой картине. Между ними, в 01:17Z, условие тревоги ГАСЛО (верхний зуб пилы),
поэтому сброс состояния на просвете воспроизвёл бы спам, а не вылечил его.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.depeg_watch import assess, decide, should_realert

W1 = "0x2385233abb910357e2b97a16d40e0443e53d0769"
W2 = "0x000000000000000000000000000000000000dead"
T0 = 1_786_061_823  # 07.08 00:17Z — первая тревога эпизода


def _st(eaten: float, ts: float = T0, borrower: str = W1) -> dict:
    return {"ts": ts, "borrower": borrower, "eaten": eaten, "worst": 0.0092}


def test_first_alert_fires():
    assert should_realert(None, W1, 0.38, T0, step=0.10, rearm_h=24)


def test_same_picture_suppressed():
    # 02:17Z: съедено 34% < 38% доложенных — агент не нужен
    assert not should_realert(_st(0.38), W1, 0.34, T0 + 2 * 3600, step=0.10, rearm_h=24)


def test_small_worsening_suppressed():
    # рост в пределах шумовой полосы (наблюдалось ~4 п.п.) — не эскалация
    assert not should_realert(_st(0.38), W1, 0.45, T0 + 3600, step=0.10, rearm_h=24)


def test_escalation_step_fires():
    assert should_realert(_st(0.38), W1, 0.48, T0 + 3600, step=0.10, rearm_h=24)


def test_new_whale_fires():
    # сменился ближайший кит — это другая позиция, разбор с нуля
    assert should_realert(_st(0.38), W2, 0.20, T0 + 3600, step=0.10, rearm_h=24)


def test_rearm_after_window():
    # подавление пережило срок годности — суточное напоминание, пока эпизод длится
    assert should_realert(_st(0.38), W1, 0.34, T0 + 24 * 3600, step=0.10, rearm_h=24)
    assert not should_realert(_st(0.38), W1, 0.34, T0 + 23 * 3600, step=0.10, rearm_h=24)


def test_no_reset_on_transient_clear():
    # Просвет 01:17Z состояние НЕ трогает: гейт видит то же prev и после просвета.
    # (Сброса в коде нет вовсе — тест фиксирует контракт: prev старше просвета всё ещё
    # подавляет не-худшую картину.)
    prev = _st(0.38, ts=T0)
    clear_then_low_tooth = T0 + 2 * 3600  # 01:17 гасло, 02:17 снова истинно
    assert not should_realert(prev, W1, 0.34, clear_then_low_tooth, step=0.10, rearm_h=24)


def test_empty_whales_borrower_change_fires():
    # киты пропали (hotset не прочитался) — borrower "" отличается от прежнего, будим
    assert should_realert(_st(0.38), "", 0.0, T0 + 3600, step=0.10, rearm_h=24)


def _hist(*ratios: float) -> list[dict]:
    return [{"ratios": {"kHYPE": r}} for r in ratios]


def test_assess_inside_band_no_breakout():
    # нижний зуб известной пилы: просадка от пика есть, но низ окна не пробит
    dd = assess({"ratios": {"kHYPE": 1.0195}}, _hist(1.0255, 1.0190, 1.0250))
    d = dd["kHYPE"]
    assert d["drawdown"] > 0
    assert d["floor"] == 1.019 and d["below_floor"] == 0.0


def test_assess_breakout_below_floor():
    # уход ниже низа окна — новая территория, глубина пробоя считается от низа
    dd = assess({"ratios": {"kHYPE": 1.0100}}, _hist(1.0255, 1.0190, 1.0250))
    d = dd["kHYPE"]
    assert d["below_floor"] > 0
    assert abs(d["below_floor"] - (1.0 - 1.0100 / 1.0190)) < 1e-5  # поле округлено до 5 знаков


def test_assess_no_history_no_breakout():
    # без истории низ = текущее значение, пробоя нет по построению
    d = assess({"ratios": {"kHYPE": 1.02}}, [])["kHYPE"]
    assert d["floor"] == 1.02 and d["below_floor"] == 0.0 and d["samples"] == 0


# --- решающая метрика (переделка 15.08) --------------------------------------------------
# Замер 198ч: весь размах отношения 1.01%, худший часовой шаг −0.71%. Прежняя метрика
# (просадка-от-пика / запас) горела 10% часов на стационарном ряде и дала 7 пустых побудок
# за 10 суток. Ниже зафиксировано, что привычная пила МОЛЧИТ, а событие — будит.

def _whale(headroom: float) -> list[dict]:
    return [{"borrower": W1, "hf": round(1 / (1 - headroom), 4), "debt_usd": 239_058,
             "headroom": headroom}]


def test_band_sawtooth_is_silent():
    """Ровно картина тревоги 15.08: просадка 0.79% (= ширина полосы), низ НЕ пробит,
    запас кита 2.51%. Прежняя схема будила (0.0079/0.0251 = 31%), новая обязана молчать."""
    dd = assess({"ratios": {"kHYPE": 1.019636}}, _hist(1.027747, 1.017416, 1.0250))
    assert dd["kHYPE"]["below_floor"] == 0.0, "низ полосы не пробит — это предпосылка теста"
    assert dd["kHYPE"]["drawdown"] > 0.0075, "просадка от пика есть — и всё же не повод"
    v = decide(dd, _whale(0.0251))
    assert not v["fire"], f"пила внутри полосы не должна будить, повод={v['reason']!r}"


def test_breach_below_floor_eating_headroom_fires():
    """Пробой низа окна ИЗОЛИРОВАННО: просадка от пика (1.08%) намеренно держится НИЖЕ
    абсолютного порога 1.5%, иначе сработал бы триггер `drawdown` и тест проверял бы не то
    (первая редакция теста именно на это и попалась)."""
    dd = assess({"ratios": {"kHYPE": 1.0090}}, _hist(1.0200, 1.017416, 1.0190))
    assert dd["kHYPE"]["drawdown"] < 0.015, "предпосылка: движение уже полосы абс. порога"
    v = decide(dd, _whale(0.0251))
    assert v["fire"] and v["reason"] == "breach_eats_headroom", v
    assert v["breach"] > 0


def test_absolute_drawdown_still_fires():
    # движение шире полосы (>1.5%) будит даже без пробоя низа и без китов
    dd = assess({"ratios": {"kHYPE": 1.0100}}, _hist(1.0300, 1.0100))
    v = decide(dd, [])
    assert v["fire"] and v["reason"] == "drawdown"


def test_thin_headroom_fires_without_any_move():
    """Триггер, которого прежней схеме НЕ ХВАТАЛО: ряд стоит на месте (просадки нет,
    пробоя нет), но кит сполз к воде — например, процентами. Низ окна дрейфует вместе с
    полосой и такого не ловит, запас — ловит."""
    dd = assess({"ratios": {"kHYPE": 1.0250}}, _hist(1.0250, 1.0250))
    assert dd["kHYPE"]["drawdown"] == 0.0 and dd["kHYPE"]["below_floor"] == 0.0
    assert decide(dd, _whale(0.0251))["fire"] is False, "здоровый кит молчит"
    v = decide(dd, _whale(0.012))
    assert v["fire"] and v["reason"] == "headroom_abs"


def test_no_whales_and_quiet_series_is_silent():
    # hotset не прочитался И ряд спокоен — будить не о чем (пустой запас != тревога)
    dd = assess({"ratios": {"kHYPE": 1.0250}}, _hist(1.0250, 1.0255))
    assert not decide(dd, [])["fire"]


def test_realert_old_state_without_reason_does_not_crash_or_mute():
    """Состояние прежнего формата (ts/borrower/eaten/worst, поля reason НЕТ) обязано
    читаться без падения и НЕ глушить новую причину: незнание не даёт права молчать."""
    old = {"ts": T0, "borrower": W1, "eaten": 0.3143, "worst": 0.00789}
    assert should_realert(old, W1, 0.0, T0 + 3600, step=0.10, rearm_h=24,
                          reason="headroom_abs")


def test_realert_same_reason_still_suppressed():
    # та же причина и не худшая картина — по-прежнему подавляем (гейт 07.08 цел)
    prev = {"ts": T0, "borrower": W1, "eaten": 0.38, "worst": 0.0092,
            "reason": "breach_eats_headroom"}
    assert not should_realert(prev, W1, 0.34, T0 + 2 * 3600, step=0.10, rearm_h=24,
                              reason="breach_eats_headroom")


def test_realert_reason_change_fires():
    # пробой сменился «кит на кромке» — другой разбор, будим
    prev = {"ts": T0, "borrower": W1, "eaten": 0.38, "worst": 0.0092,
            "reason": "breach_eats_headroom"}
    assert should_realert(prev, W1, 0.10, T0 + 3600, step=0.10, rearm_h=24,
                          reason="headroom_abs")
