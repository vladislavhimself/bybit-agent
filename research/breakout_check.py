"""Проверка на прочность стратегии «пробой 20 дней с объёмом» (и разворота −12%) из price_strategies.py.
1. Только ликвидные бумаги; издержки 0.1% и 0.3%; удержание 3/5/10 дней.
2. Медиана, топ-10 и худшие 10 сделок — не держится ли всё на выбросах; результат без 2% лучших сделок.
3. Портфель: не больше MAX_POS позиций одновременно, равные доли, капитал 100 — годовая доходность и просадка портфеля, а не сумма %.
4. По годам и по бумагам (сколько бумаг в плюсе).
Запуск из корня: python research/breakout_check.py."""
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research.price_strategies import SPLIT, build_panel, index_daily, load_daily  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
LIQUID = ["SBER", "GAZP", "LKOH", "ROSN", "NVTK", "GMKN", "PLZL", "YDEX", "TATN", "SNGS", "MGNT", "VTBR", "T", "ALRS", "CHMF", "NLMK", "MAGN", "RUAL", "AFLT", "MTSS",
          "PHOR", "MOEX", "SMLT", "PIKK", "SVCB", "POSI", "OZON", "VKCO", "SIBN", "IRAO", "HYDR", "MTLR", "TRNFP", "FLOT", "FEES", "AFKS", "X5", "SBERP", "TATNP", "SNGSP",
          "RTKM", "BSPB", "ENPG", "UPRO", "SGZH", "SELG", "UGLD", "HEAD", "ASTR", "CBOM", "BELU", "LENT", "MVID", "ETLN", "LSRG", "NMTP", "RENI"]
MAX_POS = 8


def signals(panel, syms, side, lookback=20, vol_mult=1.5):
    close, high, vol = panel["close"], panel["high"], panel["vol"]
    out = []  # (i, sym)
    for sym in syms:
        if sym not in close:
            continue
        c, hi, v = close[sym], high[sym], vol[sym]
        ref = hi.rolling(lookback).max().shift(1) if side == 1 else c.rolling(lookback).min().shift(1)
        va = v.rolling(lookback).mean().shift(1)
        cond = ((c > ref) if side == 1 else (c < ref)) & (v >= vol_mult * va)
        last = -99
        for i in np.where(cond.to_numpy())[0]:
            if i - last < 5:
                continue
            out.append((i, sym))
            last = i
    return out


def reversal_signals(panel, syms, thr=-12):
    rel5 = panel["rel"].rolling(5).sum()
    out = []
    for sym in syms:
        if sym not in rel5:
            continue
        r = rel5[sym].to_numpy()
        last = -99
        for i in np.where(r <= thr)[0]:
            if i - last < 5:
                continue
            out.append((i, sym))
            last = i
    return out


def trades_from(panel, sigs, side, hold, cost):
    close, opn, dates = panel["close"], panel["open"], panel["close"].index
    tr = []
    for i, sym in sigs:
        if i + hold >= len(dates):
            continue
        o, c = opn[sym].iloc[i + 1], close[sym].iloc[i + hold]
        if np.isnan(o) or np.isnan(c):
            continue
        tr.append({"entry": dates[i + 1], "exit": dates[i + hold], "sym": sym, "pnl": side * (c / o - 1) * 100 - cost})
    return tr


def summarize(name, tr):
    if len(tr) < 20:
        print(f"  {name}: мало ({len(tr)})")
        return
    pn = np.array([t["pnl"] for t in tr])
    d = pd.to_datetime([t["entry"] for t in tr])
    bef, aft = pn[d < SPLIT], pn[d >= SPLIT]
    cum = np.cumsum(pn)
    dd = float((np.maximum.accumulate(cum) - cum).max())
    k = max(1, int(len(pn) * 0.02))
    trimmed = np.sort(pn)[:-k]
    by_sym = defaultdict(list)
    for t in tr:
        by_sym[t["sym"]].append(t["pnl"])
    pos_syms = sum(1 for v in by_sym.values() if sum(v) > 0)
    print(f"  {name}: n={len(pn)} (~{len(pn) / max(1, (d.max() - d.min()).days / 7):.1f}/нед) приб. {np.mean(pn > 0):.0%} средняя {pn.mean():+.2f}% медиана {np.median(pn):+.2f}% "
          f"сумма {cum[-1]:+.0f}% просадка(сумма) {dd:.0f}% | без 2% лучших: средняя {trimmed.mean():+.2f}% | до 2024 {bef.sum():+.0f}% ({np.mean(bef > 0) if len(bef) else 0:.0%}) "
          f"после {aft.sum():+.0f}% ({np.mean(aft > 0) if len(aft) else 0:.0%}) | бумаг в плюсе {pos_syms}/{len(by_sym)}")
    by_year = defaultdict(list)
    for t in tr:
        by_year[t["entry"].year].append(t["pnl"])
    print("     по годам: " + ", ".join(f"{y}: {sum(v):+.0f}% ({len(v)}, {np.mean(np.array(v) > 0):.0%})" for y, v in sorted(by_year.items())))
    return pn


def portfolio(name, tr, max_pos=MAX_POS, capital=100.0):
    """Равные доли capital/max_pos на сделку, не больше max_pos одновременно (лишние сигналы пропускаются), без плеча."""
    tr = sorted(tr, key=lambda t: t["entry"])
    open_pos = []  # exit dates
    eq = capital
    curve = []
    taken = 0
    for t in tr:
        open_pos = [e for e in open_pos if e >= t["entry"]]
        if len(open_pos) >= max_pos:
            continue
        size = eq / max_pos
        eq += size * t["pnl"] / 100
        open_pos.append(t["exit"])
        taken += 1
        curve.append((t["exit"], eq))
    if not curve:
        return
    s = pd.Series([e for _, e in curve], index=pd.to_datetime([d for d, _ in curve])).sort_index()
    yearly = s.groupby(s.index.year).last()
    prev = capital
    yr = []
    for y, v in yearly.items():
        yr.append(f"{y}: {(v / prev - 1) * 100:+.0f}%")
        prev = v
    dd = float(((s.cummax() - s) / s.cummax()).max() * 100)
    years = (s.index[-1] - s.index[0]).days / 365.25
    cagr = ((s.iloc[-1] / capital) ** (1 / years) - 1) * 100 if years > 0 else 0
    print(f"     ПОРТФЕЛЬ (≤{max_pos} позиций, равные доли, без плеча): взято сделок {taken}/{len(tr)}, итог {capital:.0f} → {s.iloc[-1]:.0f}, "
          f"среднегодовая {cagr:+.1f}%, макс. просадка {dd:.1f}% | " + ", ".join(yr))


def main():
    daily = load_daily()
    panel = build_panel(daily, index_daily())
    syms_all = list(panel["close"].columns)
    liq = [s for s in LIQUID if s in panel["close"]]
    print(f"все бумаги: {len(syms_all)}, ликвидные: {len(liq)}")
    for side, lab in ((1, "ПРОБОЙ ВВЕРХ → лонг"), (-1, "ПРОБОЙ ВНИЗ → шорт")):
        print(f"\n{lab}")
        for universe, ulab in ((syms_all, "все"), (liq, "ликвидные")):
            sigs = signals(panel, universe, side)
            for hold in (3, 5, 10):
                for cost in (0.1, 0.3):
                    tr = trades_from(panel, sigs, side, hold, cost)
                    pn = summarize(f"{ulab}, {hold} д, издержки {cost}%", tr)
                    if ulab == "ликвидные" and hold == 5:
                        portfolio("", tr)
        # выбросы
        sigs = signals(panel, liq, side)
        tr = trades_from(panel, sigs, side, 5, 0.3)
        best = sorted(tr, key=lambda t: -t["pnl"])[:8]
        worst = sorted(tr, key=lambda t: t["pnl"])[:5]
        print("     топ-8: " + "; ".join(f"{t['sym']} {t['entry'].date()} {t['pnl']:+.0f}%" for t in best))
        print("     худшие: " + "; ".join(f"{t['sym']} {t['entry'].date()} {t['pnl']:+.0f}%" for t in worst))
        # без объёма — важен ли фильтр объёма
        sigs_nv = signals(panel, liq, side, vol_mult=0.0)
        summarize("ликвидные, 5 д, 0.3%, БЕЗ фильтра объёма", trades_from(panel, sigs_nv, side, 5, 0.3))
        sigs_v2 = signals(panel, liq, side, vol_mult=2.0)
        summarize("ликвидные, 5 д, 0.3%, объём ≥2×", trades_from(panel, sigs_v2, side, 5, 0.3))
        sigs_55 = signals(panel, liq, side, lookback=55)
        summarize("ликвидные, 5 д, 0.3%, пробой 55 дней", trades_from(panel, sigs_55, side, 5, 0.3))

    print("\nРАЗВОРОТ −12% за 5 дней к индексу → лонг")
    for universe, ulab in ((syms_all, "все"), (liq, "ликвидные")):
        sigs = reversal_signals(panel, universe)
        for hold in (3, 5, 10):
            tr = trades_from(panel, sigs, 1, hold, 0.3)
            summarize(f"{ulab}, {hold} д, 0.3%", tr)
            if ulab == "ликвидные" and hold == 5:
                portfolio("", tr)

    print("\nОБЕ СТОРОНЫ ПРОБОЯ ВМЕСТЕ, ликвидные, 5 д, 0.3% — портфель")
    tr = trades_from(panel, signals(panel, liq, 1), 1, 5, 0.3) + trades_from(panel, signals(panel, liq, -1), -1, 5, 0.3)
    summarize("лонг+шорт", tr)
    for mp in (4, 8, 15):
        portfolio("", tr, max_pos=mp)


if __name__ == "__main__":
    main()
