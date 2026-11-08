"""Пробой вниз с объёмом → шорт: честные уточнения. (1) без февраля–марта 2022 (остановка торгов искажает); (2) плата за шорт 0.04%/день
сверх 0.3% на круг; (3) выходы по причине вместо времени: тезис сломался (закрытие выше уровня пробоя — минимума 20 дней, который пробили),
трейлинг 3% от лучшего закрытия, стоп 5%, предохранитель 5/10 дней; (4) портфель ≤8 и ≤15 позиций. Запуск из корня: python research/breakdown_exits.py."""
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research.breakout_check import LIQUID, portfolio, signals  # noqa: E402
from research.price_strategies import SPLIT, build_panel, index_daily, load_daily  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
COST = 0.3
BORROW = 0.04  # % в день за шорт
EXCL = (pd.Timestamp("2022-02-21"), pd.Timestamp("2022-04-15"))


def run(panel, syms, hold_max, thesis=False, trailing=None, stop=None, exclude_2022=True, borrow=True, side=-1):
    close, opn, dates = panel["close"], panel["open"], panel["close"].index
    tr = []
    for i, sym in signals(panel, syms, side):
        if i + 2 >= len(dates):
            continue
        d0 = dates[i + 1]
        if exclude_2022 and EXCL[0] <= d0 <= EXCL[1]:
            continue
        o = opn[sym].iloc[i + 1]
        if np.isnan(o):
            continue
        level = (close[sym].rolling(20).min().shift(1) if side == -1 else panel["high"][sym].rolling(20).max().shift(1)).iloc[i]
        best, why, j_exit = -1e9, "время", min(i + hold_max, len(dates) - 1)
        for j in range(i + 1, min(i + hold_max, len(dates) - 1) + 1):
            c = close[sym].iloc[j]
            if np.isnan(c):
                continue
            r = side * (c / o - 1) * 100
            best = max(best, r)
            if stop is not None and r <= -stop:
                why, j_exit = "стоп", j
                break
            if trailing is not None and best > 0 and best - r >= trailing:
                why, j_exit = "трейлинг", j
                break
            if thesis and j > i + 1 and ((side == -1 and c > level) or (side == 1 and c < level)):
                why, j_exit = "тезис", j
                break
        c = close[sym].iloc[j_exit]
        if np.isnan(c):
            continue
        days = j_exit - i
        pnl = side * (c / o - 1) * 100 - COST - (BORROW * days if (borrow and side == -1) else 0)
        tr.append({"entry": d0, "exit": dates[j_exit], "sym": sym, "pnl": pnl, "why": why, "days": days})
    return tr


def summ(name, tr):
    pn = np.array([t["pnl"] for t in tr])
    d = pd.to_datetime([t["entry"] for t in tr])
    bef, aft = pn[d < SPLIT], pn[d >= SPLIT]
    cum = np.cumsum(pn)
    dd = float((np.maximum.accumulate(cum) - cum).max())
    why = defaultdict(list)
    for t in tr:
        why[t["why"]].append(t["pnl"])
    by_year = defaultdict(list)
    for t in tr:
        by_year[t["entry"].year].append(t["pnl"])
    print(f"  {name}: n={len(pn)} (~{len(pn) / max(1, (d.max() - d.min()).days / 7):.1f}/нед) приб. {np.mean(pn > 0):.0%} средняя {pn.mean():+.2f}% медиана {np.median(pn):+.2f}% "
          f"сумма {cum[-1]:+.0f}% просадка(сумма) {dd:.0f}% дней в сделке {np.mean([t['days'] for t in tr]):.1f} | до 2024 {bef.sum():+.0f}% ({np.mean(bef > 0) if len(bef) else 0:.0%}) "
          f"после {aft.sum():+.0f}% ({np.mean(aft > 0) if len(aft) else 0:.0%})")
    print("     выходы: " + ", ".join(f"{k} {len(v)} ({np.mean(v):+.2f}%)" for k, v in sorted(why.items(), key=lambda kv: -len(kv[1]))) +
          " | по годам: " + ", ".join(f"{y}: {sum(v):+.0f}%" for y, v in sorted(by_year.items())))


def main():
    daily = load_daily()
    panel = build_panel(daily, index_daily())
    liq = [s for s in LIQUID if s in panel["close"]]
    print(f"ликвидные: {len(liq)}; издержки {COST}% на круг + {BORROW}%/день за шорт; февраль–апрель 2022 исключены")
    print("\nПРОБОЙ ВНИЗ → ШОРТ")
    base = run(panel, liq, 5)
    summ("5 дней по времени", base)
    portfolio("", base, 8)
    summ("5 дней, БЕЗ исключения 2022 и без платы за шорт", run(panel, liq, 5, exclude_2022=False, borrow=False))
    for name, kw in (("тезис (закрытие выше уровня пробоя), макс 10 д", dict(hold_max=10, thesis=True)),
                     ("тезис + трейлинг 3%, макс 10 д", dict(hold_max=10, thesis=True, trailing=3.0)),
                     ("трейлинг 3%, макс 10 д", dict(hold_max=10, trailing=3.0)),
                     ("стоп 5%, макс 5 д", dict(hold_max=5, stop=5.0)),
                     ("тезис + стоп 5%, макс 10 д", dict(hold_max=10, thesis=True, stop=5.0)),
                     ("10 дней по времени", dict(hold_max=10)), ("3 дня по времени", dict(hold_max=3))):
        tr = run(panel, liq, **kw)
        summ(name, tr)
        portfolio("", tr, 8)
    print("\nПРОБОЙ ВВЕРХ → ЛОНГ (для сравнения)")
    for name, kw in (("5 дней по времени", dict(hold_max=5)), ("тезис (закрытие ниже уровня пробоя), макс 10 д", dict(hold_max=10, thesis=True)),
                     ("тезис + трейлинг 3%, макс 10 д", dict(hold_max=10, thesis=True, trailing=3.0))):
        tr = run(panel, liq, side=1, **kw)
        summ(name, tr)
        portfolio("", tr, 8)
    print("\nВМЕСТЕ: шорт (тезис+трейлинг) + лонг (тезис+трейлинг), ≤8 и ≤15 позиций")
    both = run(panel, liq, 10, thesis=True, trailing=3.0) + run(panel, liq, 10, thesis=True, trailing=3.0, side=1)
    summ("лонг+шорт", both)
    portfolio("", both, 8)
    portfolio("", both, 15)


if __name__ == "__main__":
    main()
