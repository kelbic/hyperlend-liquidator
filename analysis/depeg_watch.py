"""Депег-вотч LST-ряда HyperEVM: сколько сжатия отделяет китов от ликвидации.

ЗАЧЕМ ИМЕННО ЭТО. Крупная книга hyperlend — плечевой стейкинг: залог kHYPE/wstHYPE/beHYPE/PT
против долга WHYPE. У такой позиции HF не зависит от цены HYPE вовсе (обе ноги двигаются
вместе) — она зависит ТОЛЬКО от ОТНОШЕНИЯ цены залога к цене WHYPE. Отношение LST/HYPE в
норме монотонно растёт (капают стейкинг-награды), поэтому его СЖАТИЕ — единственный триггер,
способный уронить китов, и единственный, который стоит караулить заранее.

Арифметика запаса точная и простая. Для позиции «LST против WHYPE» HF линеен по отношению:
если отношение сожмётся в f раз, то HF' = HF × f. Значит ликвидация наступает при
    сжатие_до_падения = 1 − 1/HF
(HF 1.0640 -> 6.0%; сходится с наблюдением «киты падают на 5-6%»).

Что делает вотч: раз в час пишет снимок отношений и запас каждого кита и будит АГЕНТА
(не человека: он тут ничего не подписывает — канон [[alerts-only-where-human-acts]]).

ЧТО СЧИТАЕТСЯ ПОВОДОМ (переделано 15.08 — см. ниже). Прежняя решающая метрика — «просадка
от максимума окна, делённая на запас ближайшего кита» — оказалась откалибрована по ШУМУ.
Отношение kHYPE/WHYPE стационарно и ходит узкой полосой: за 198ч замера весь размах
1.01% (1.017416–1.027747), худший часовой шаг вниз −0.71%, худшая просадка за любые 6ч
−0.69%. Просадка-от-пика поэтому НАСЫЩАЕТСЯ на ширине полосы B≈1.01% и перестаёт зависеть
от опасности: при запасе кита H тревога «съедено ≥30%» структурно неизбежна, как только
H ≤ B/0.30 = 3.35%. У ближайшего кита H=2.51% ⇒ условие истинно 10% часов, порог сидит
ровно на p90 стационарного распределения, и ре-арм раз в сутки превращал это в ежедневную
побудку ни о чём: 7 срабатываний за 05–15.08, ВСЕ с «сжатием» 0.75–0.96% (= ширина полосы)
и НИ ОДНОГО пробоя низа. Это ровно тот класс дефекта, что уже назван в
[[emptiness-alarm-needs-measured-rate]]: тревога на медиане тишины.

Решающих метрик теперь три, и все меряют СОБЫТИЕ, а не положение внутри полосы:
  1. пробой НИЗА окна (below_floor) — новая территория; доля съеденного запаса считается
     от него, а не от просадки-от-пика, поэтому привычная пила больше не будит;
  2. абсолютная просадка ≥ DRAWDOWN_ALERT (1.5% — заведомо шире полосы 1.01%);
  3. абсолютный запас ближайшего кита ≤ HEADROOM_ABS (1.5% ≈ два худших часовых шага до
     воды). Этого триггера прежней схеме НЕ ХВАТАЛО: низ окна дрейфует вместе с полосой,
     поэтому медленное сползание пробоя не даёт, а запас — не врёт. Кит, припаркованный
     под этим порогом, будет будить раз в сутки: это не спам, а кромка.

Повторная побудка — только по ЭСКАЛАЦИИ. Отношение ходит шумовой полосой ~0.8% с периодом
в часы (07.08: два подъёма агента за ночь на ОДНОЙ картине, второй — на менее тяжёлой),
поэтому «условие снова истинно» ≠ «есть новая работа». Состояние последней тревоги живёт в
data/depeg_alert_state.json и НЕ сбрасывается на просветах: просвет — верхний зуб той же
пилы, сброс по нему вернул бы побудку на каждом нижнем. Будим снова, если съедено выросло
на ступень, сменился ближайший кит, или прошло REARM_H часов (пока эпизод длится, это же
даёт суточное напоминание — у подавления есть срок годности).

Только чтение: оракул + пул. Ни подписи, ни отправки.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis.aave import SEL_GET_ASSET_PRICE, SEL_GET_USER_ACCOUNT_DATA, decode_user_account_data
from analysis.multicall import multicall
from analysis.protocols import ADDR_TO_SYMBOL, ORACLE, ORACLE_BASE_UNIT, POOL, TOKENS
from analysis.rpc import Rpc

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
HIST_PATH = os.path.join(DATA_DIR, "depeg_history.jsonl")
ALERT_STATE_PATH = os.path.join(DATA_DIR, "depeg_alert_state.json")

# Ряд, чьё отношение к WHYPE решает судьбу плечевой книги.
LST = ["kHYPE", "wstHYPE", "beHYPE", "PT-kHYPE-24SEP2026", "PT-kHYPE-19MAR2026"]
BASE = "WHYPE"

WINDOW_H = float(os.environ.get("HL_DEPEG_WINDOW_H", "168"))     # окно базы, часов (7 суток)
DRAWDOWN_ALERT = float(os.environ.get("HL_DEPEG_DRAWDOWN", "0.015"))   # 1.5% от базы
HEADROOM_FRAC = float(os.environ.get("HL_DEPEG_HEADROOM_FRAC", "0.30"))  # доля съеденного запаса
# Абсолютный порог запаса: 1.5% ≈ два худших часовых шага (−0.71%) до воды. Замер 15.08 по
# 198ч истории; ближайший кит сейчас 2.51%, то есть порог НЕ горит на текущей картине.
HEADROOM_ABS = float(os.environ.get("HL_DEPEG_HEADROOM_ABS", "0.015"))
MIN_DEBT_USD = float(os.environ.get("HL_DEPEG_MIN_DEBT", "100000"))    # кто считается китом
ESCALATION_STEP = float(os.environ.get("HL_DEPEG_ESCALATION_STEP", "0.10"))  # +10 п.п. съеденного
REARM_H = float(os.environ.get("HL_DEPEG_REARM_H", "24"))              # срок годности подавления


def read_ratios(rpc: Rpc) -> dict:
    """{символ: отношение цены к WHYPE} по ценам оракула, одним multicall."""
    syms = [BASE] + LST
    addrs = [TOKENS[s]["address"] for s in syms]
    res = multicall(rpc, [(ORACLE, SEL_GET_ASSET_PRICE + a[2:].rjust(64, "0")) for a in addrs])
    px = {}
    for s, (ok, ret) in zip(syms, res):
        if ok and len(ret) >= 66:
            px[s] = int(ret[2:66], 16) / ORACLE_BASE_UNIT
    base = px.get(BASE)
    if not base:
        raise RuntimeError("нет цены WHYPE — база отношения недоступна")
    return {"prices": px, "ratios": {s: px[s] / base for s in LST if s in px}}


def whale_headroom(rpc: Rpc, borrowers: list[str]) -> list[dict]:
    """Для каждого заёмщика: HF и сжатие отношения, после которого он под водой (1 − 1/HF).

    Оценка ВЕРНА для чистой связки «LST-залог против WHYPE-долга» и КОНСЕРВАТИВНА для
    смешанных: доля залога вне LST-ряда двигается вместе с долгом, поэтому реальный запас
    у смешанной позиции больше, а не меньше — мы не проспим падение, максимум разбудим зря.
    """
    if not borrowers:
        return []
    res = multicall(rpc, [(POOL, SEL_GET_USER_ACCOUNT_DATA + b[2:].rjust(64, "0"))
                          for b in borrowers])
    out = []
    for b, (ok, ret) in zip(borrowers, res):
        if not (ok and len(ret) >= 2 + 6 * 64):
            continue
        a = decode_user_account_data(ret)
        debt = a.get("total_debt_base", 0) / ORACLE_BASE_UNIT
        hf = a["health_factor"] / 1e18
        if debt < MIN_DEBT_USD or hf <= 0 or hf > 5:
            continue
        out.append({"borrower": b, "hf": round(hf, 4), "debt_usd": round(debt),
                    "headroom": round(1.0 - 1.0 / hf, 4) if hf > 1 else 0.0})
    out.sort(key=lambda r: r["headroom"])
    return out


def load_history(hours: float) -> list[dict]:
    if not os.path.exists(HIST_PATH):
        return []
    cutoff = time.time() - hours * 3600
    rows = []
    with open(HIST_PATH) as f:
        for ln in f:
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if r.get("ts", 0) >= cutoff:
                rows.append(r)
    return rows


def assess(cur: dict, hist: list[dict]) -> dict:
    """Просадка каждого отношения от максимума окна. Для растущего ряда максимум = база.

    Ряд kHYPE на деле не монотонен: фид — композит (prim:redstone/fundam,
    sec:chainlink/fundam, emerg:redstone/market), и отношение ходит меандром ~±0.5%
    (полоса ~1.0195–1.0255, замер 05–08.08; гипотеза — переключение источников, не
    доказано: аксессоры под-фидов закрыты). Поэтому кроме пика окна считаем его НИЗ:
    просадка внутри известной полосы — привычная пила, пробой ниже низа — новая
    территория. Низ окна сам дрейфует вместе с полосой, так что «внутри полосы» —
    контекст для разбора, не отбой: решающей метрикой остаётся съеденный запас."""
    out = {}
    for s, r in cur["ratios"].items():
        past = [h["ratios"][s] for h in hist if s in (h.get("ratios") or {})]
        peak = max(past + [r])
        floor = min(past) if past else r
        out[s] = {"ratio": round(r, 6), "peak": round(peak, 6),
                  "drawdown": round(1.0 - r / peak, 5) if peak else 0.0,
                  "floor": round(floor, 6),
                  "below_floor": round(max(0.0, 1.0 - r / floor), 5) if past else 0.0,
                  "samples": len(past)}
    return out


def decide(dd: dict, whales: list[dict], drawdown_alert: float = None,
           headroom_frac: float = None, headroom_abs: float = None) -> dict:
    """Решение «будить или нет» — чистая функция, чтобы её можно было проверить тестом.

    Числитель доли съеденного запаса — ПРОБОЙ НИЗА окна (новая территория), а НЕ просадка
    от пика: просадка-от-пика насыщается на ширине полосы и потому будит на медиане тишины
    (обоснование и замер — в шапке модуля). Просадка-от-пика осталась только как (а) быстрый
    абсолютный триггер на движение шире полосы и (б) контекст в тексте тревоги.
    """
    drawdown_alert = DRAWDOWN_ALERT if drawdown_alert is None else drawdown_alert
    headroom_frac = HEADROOM_FRAC if headroom_frac is None else headroom_frac
    headroom_abs = HEADROOM_ABS if headroom_abs is None else headroom_abs

    worst = max((d["drawdown"] for d in dd.values()), default=0.0)
    worst_sym = max(dd, key=lambda s: dd[s]["drawdown"]) if dd else None
    breach = max((d.get("below_floor", 0.0) for d in dd.values()), default=0.0)
    breach_sym = (max(dd, key=lambda s: dd[s].get("below_floor", 0.0))
                  if breach > 0 else None)
    nearest = whales[0]["headroom"] if whales else None
    eaten = (breach / nearest) if (nearest and nearest > 0) else 0.0

    if worst >= drawdown_alert:
        reason = "drawdown"
    elif nearest and nearest > 0 and eaten >= headroom_frac:
        reason = "breach_eats_headroom"
    elif nearest is not None and 0 < nearest <= headroom_abs:
        reason = "headroom_abs"
    else:
        reason = ""
    return {"fire": bool(reason), "reason": reason, "worst": worst, "worst_sym": worst_sym,
            "breach": breach, "breach_sym": breach_sym, "nearest": nearest, "eaten": eaten}


def should_realert(prev: dict | None, borrower: str, eaten: float, now: float,
                   step: float = None, rearm_h: float = None,
                   reason: str = None) -> bool:
    """Гейт повторной побудки: та же картина не будит дважды.

    Состояние НЕ сбрасывается, когда условие тревоги временно гаснет: просвет — верхний
    зуб той же шумовой пилы, и сброс по нему вернул бы побудку на каждом нижнем. Новая
    побудка — только если картина ХУЖЕ последней доложенной (съедено выросло на ступень),
    сменился ближайший кит, или подавление пережило свой срок годности (rearm_h).
    """
    step = ESCALATION_STEP if step is None else step
    rearm_h = REARM_H if rearm_h is None else rearm_h
    if not prev:
        return True
    if borrower != prev.get("borrower"):
        return True
    # Смена ПРИЧИНЫ — другая картина, а не та же хуже/лучше: пробой низа и «кит на кромке»
    # требуют разного разбора. Состояние старого формата поля не имеет (prev.get -> None):
    # тогда сравнение считаем несостоявшимся и падаем на сторону ПОБУДКИ, а не тишины
    # (канон [[unknown-must-close-the-gate]] — незнание не даёт права молчать).
    if reason is not None and reason != prev.get("reason", None):
        return True
    if eaten >= prev.get("eaten", 0.0) + step:
        return True
    if now - prev.get("ts", 0) >= rearm_h * 3600:
        return True
    return False


def load_alert_state() -> dict | None:
    try:
        with open(ALERT_STATE_PATH) as f:
            return json.load(f)
    except Exception:                                                # noqa: BLE001
        return None


def save_alert_state(state: dict) -> None:
    tmp = ALERT_STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, ALERT_STATE_PATH)


def notify_agent(text: str, key: str) -> None:
    """Тревога адресована АГЕНТУ: пре-арм и разбор — его работа, человеку тут нечего подписывать."""
    try:
        sys.path.insert(0, os.path.expanduser("~/.fleet-watch"))
        from notify import notify                                    # type: ignore
        notify(text, source="hyperlend-depeg", hil=False, key=key, dedup_sec=3600)
    except Exception as e:                                           # noqa: BLE001
        print(f"  [notify] не доставлено: {type(e).__name__}: {e}")


def main() -> int:
    rpc = Rpc()
    cur = read_ratios(rpc)
    hist = load_history(WINDOW_H)
    dd = assess(cur, hist)

    book_path = os.path.join(DATA_DIR, "hotset.json")
    hot = []
    try:
        hot = sorted({a.lower() for a in json.load(open(book_path))["hot"]})
    except Exception:                                                # noqa: BLE001
        pass
    whales = whale_headroom(rpc, hot)

    row = {"ts": int(time.time()), "prices": {k: round(v, 6) for k, v in cur["prices"].items()},
           "ratios": {k: round(v, 8) for k, v in cur["ratios"].items()},
           "whales": whales[:5]}
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HIST_PATH, "a") as f:
        f.write(json.dumps(row) + "\n")

    print(f"отношения к {BASE} (просадка от максимума окна {WINDOW_H:.0f}ч):")
    for s, d in sorted(dd.items()):
        print(f"  {s:<20} {d['ratio']:.6f}  пик {d['peak']:.6f}  "
              f"просадка {d['drawdown'] * 100:+.3f}%  проб {d['samples']}")
    if whales:
        print(f"\nкиты (долг ≥ ${MIN_DEBT_USD:,.0f}), запас до воды = 1 − 1/HF:")
        for w in whales[:5]:
            print(f"  {w['borrower'][:12]}… HF={w['hf']:.4f} долг ${w['debt_usd']:,} "
                  f"запас {w['headroom'] * 100:.2f}%")
    else:
        print("\nкитов в горячем наборе нет")

    v = decide(dd, whales)
    worst, worst_sym = v["worst"], v["worst_sym"]
    nearest, eaten = v["nearest"], v["eaten"]
    band = dd.get(worst_sym) or {}
    band_note = (f"ПРОБОЙ низа окна на {v['breach'] * 100:.2f}% — новая территория"
                 if v["breach"] > 0
                 else f"внутри полосы окна (низ {band.get('floor', 0.0):.6f} не пробит)")
    print(f"\nхудшая просадка {worst * 100:.3f}% ({worst_sym}: {band_note}); "
          + (f"ближайший запас {nearest * 100:.2f}%; новой территорией съедено "
             f"{eaten * 100:.1f}% запаса" if nearest else "")
          + (f" => повод: {v['reason']}" if v["fire"] else " => повода нет"))

    if v["fire"]:
        borrower = whales[0]["borrower"] if whales else ""
        prev, now = load_alert_state(), time.time()
        if should_realert(prev, borrower, eaten, now, reason=v["reason"]):
            names = ", ".join(f"{s} {d['drawdown'] * 100:+.2f}%" for s, d in dd.items()
                              if d["drawdown"] >= DRAWDOWN_ALERT / 2) or "—"
            # китов может не быть вовсе (hotset не прочитался) — просадка всё равно
            # должна доехать до агента, а не упасть тут на whales[0]
            whale_part = (f"Ближайший кит {borrower[:10]}… HF={whales[0]['hf']:.4f}, "
                          f"запас {nearest * 100:.2f}% — новой территорией съедено "
                          f"{eaten * 100:.0f}%. "
                          if whales else "Китов в горячем наборе НЕТ (проверить hotset). ")
            why = {"drawdown": f"просадка {worst * 100:.2f}% шире полосы",
                   "breach_eats_headroom": "пробой низа съел запас кита",
                   "headroom_abs": f"кит НА КРОМКЕ: запас ≤ {HEADROOM_ABS * 100:.1f}%",
                   }.get(v["reason"], v["reason"])
            notify_agent(
                f"📉 hyperlend [{why}]: сжатие LST-ряда {worst * 100:.2f}% "
                f"({names}; {band_note}). "
                f"{whale_part}Проверить пре-арм и глубину выхода kHYPE→WHYPE.",
                key="hl-depeg")
            save_alert_state({"ts": int(now), "borrower": borrower, "reason": v["reason"],
                              "headroom": round(nearest, 5) if nearest else None,
                              "eaten": round(eaten, 4), "worst": round(worst, 5)})
            print("→ агент разбужен")
        else:
            print(f"→ побудка подавлена: картина не хуже доложенной "
                  f"(съедено {eaten * 100:.0f}% против {prev.get('eaten', 0) * 100:.0f}% "
                  f"в тревоге {time.strftime('%d.%m %H:%M', time.gmtime(prev.get('ts', 0)))}Z, "
                  f"ступень {ESCALATION_STEP * 100:.0f} п.п., ре-арм {REARM_H:.0f}ч)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
