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

from analysis.depeg_watch import should_realert

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
