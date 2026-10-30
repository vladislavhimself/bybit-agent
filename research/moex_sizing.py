"""Пункт 5: размер по волатильности портфеля против фиксированного риска на сделку.
Пункт 1: фильтр растяжения выключить только для шортов.
Запуск: DAYS=2500 python research/moex_sizing.py data/history <файл со списком тикеров>"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import moex_sim as ms  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
H, UNI = sys.argv[1], open(sys.argv[2], encoding="utf-8").read().split()
DAYS = int(os.environ.get("DAYS", "2500"))
feats = ms.load(H, UNI, DAYS)
print(f"бумаг {len(feats)}, дней {DAYS}")

base = ms.run(feats)
print("база (как в боте):", ms.fmt_stats(ms.stats(base)))


def equity_curve(trades, risk_pct=0.8, target_vol=None, lookback=20, clamp=(0.5, 2.0)):
    """Дневная кривая капитала: каждая сделка рискует risk_pct% капитала на момент входа (× масштаб по волатильности).
    Масштаб = целевая дневная волатильность / реализованная за lookback дней (по дневным доходностям кривой)."""
    t = trades.sort_values("t_exit").copy()
    days = pd.date_range(t.t.min().normalize(), t.t_exit.max().normalize(), freq="D")
    eq = pd.Series(1.0, index=days)
    daily_pnl = pd.Series(0.0, index=days)
    open_by_entry = t.sort_values("t")
    # масштаб вычисляем по состоянию кривой на день входа; результат зачисляется в день выхода
    scale_at = {}
    cur = 1.0
    hist = []
    rets_by_day = {}
    for day in days:
        # зачисляем закрытия этого дня
        closed = t[(t.t_exit.dt.normalize() == day)]
        pnl = 0.0
        for _, row in closed.iterrows():
            pnl += row.r * row.risk_abs
        prev = cur
        cur += pnl
        eq[day] = cur
        rets_by_day[day] = cur / prev - 1 if prev > 0 else 0.0
        hist.append(rets_by_day[day])
        # размер для сделок, открывающихся завтра/сегодня: по волатильности последних lookback дней
        if target_vol and len(hist) >= lookback:
            rv = float(np.std(hist[-lookback:]))
            sc = float(np.clip(target_vol / rv, *clamp)) if rv > 0 else 1.0
        else:
            sc = 1.0
        scale_at[day] = (cur, sc)
    return eq


def simulate_sizing(trades, risk_pct=0.8, target_vol=None, lookback=20, clamp=(0.5, 2.0)):
    t = trades.sort_values("t").reset_index(drop=True).copy()
    days = pd.date_range(t.t.min().normalize(), t.t_exit.max().normalize(), freq="D")
    cur = 1.0
    eq, hist = [], []
    pending = []   # (t_exit_day, pnl_abs)
    day_scale = {}
    ti = 0
    for day in days:
        # 1) зачисляем закрытия
        still = []
        for d_exit, pnl in pending:
            if d_exit <= day:
                cur += pnl
            else:
                still.append((d_exit, pnl))
        pending = still
        eq.append(cur)
        if len(eq) >= 2:
            hist.append(eq[-1] / eq[-2] - 1 if eq[-2] > 0 else 0.0)
        if target_vol and len(hist) >= lookback:
            rv = float(np.std(hist[-lookback:]))
            sc = float(np.clip(target_vol / rv, *clamp)) if rv > 0 else 1.0
        else:
            sc = 1.0
        day_scale[day] = sc
        # 2) открываем сделки этого дня
        while ti < len(t) and t.t.iloc[ti].normalize() == day:
            row = t.iloc[ti]
            risk_abs = cur * risk_pct / 100 * sc
            pending.append((row.t_exit.normalize(), row.r * risk_abs))
            ti += 1
    for _, pnl in pending:
        cur += pnl
    eq = pd.Series(eq, index=days)
    dd = (eq / eq.cummax() - 1).min()
    years = (days[-1] - days[0]).days / 365.25
    cagr = (eq.iloc[-1]) ** (1 / years) - 1 if years > 0 and eq.iloc[-1] > 0 else -1
    return {"итог": round((eq.iloc[-1] - 1) * 100, 1), "годовых": round(cagr * 100, 1), "просадка": round(dd * 100, 1),
            "год/просадка": round(cagr / -dd, 2) if dd < 0 else 0, "ср.масштаб": round(float(np.mean(list(day_scale.values()))), 2)}, eq


print("\n== Пункт 5: размер по волатильности (риск 0.8% на сделку × масштаб) ==")
res0, eq0 = simulate_sizing(base, 0.8, None)
print(f"{'фиксированный риск 0.8%':44s}", res0)
daily0 = eq0.pct_change().dropna()
med_vol = float(daily0.rolling(20).std().median())
print(f"медианная дневная волатильность кривой: {med_vol * 100:.2f}%")
for tv_mult, label in ((1.0, "цель = медиана"), (1.3, "цель = 1.3×медианы"), (0.8, "цель = 0.8×медианы")):
    res, _ = simulate_sizing(base, 0.8, med_vol * tv_mult)
    print(f"{label:44s}", res)
for lb in (10, 40):
    res, _ = simulate_sizing(base, 0.8, med_vol, lookback=lb)
    print(f"{'цель = медиана, окно ' + str(lb) + ' дн.':44s}", res)
res, _ = simulate_sizing(base, 0.8, med_vol, clamp=(0.5, 1.5))
print(f"{'цель = медиана, масштаб 0.5–1.5':44s}", res)
print("по годам (цель = медиана):")
res_v, eq_v = simulate_sizing(base, 0.8, med_vol)
for y in sorted(set(eq0.index.year)):
    a0 = eq0[eq0.index.year == y]; av = eq_v[eq_v.index.year == y]
    print(f"   {y}: фикс {((a0.iloc[-1] / a0.iloc[0]) - 1) * 100:+6.1f}%  вола {((av.iloc[-1] / av.iloc[0]) - 1) * 100:+6.1f}%")

print("\n== Пункт 1: фильтр растяжения для шортов выключить ==")
for label, g in (("лонг 3.0 / шорт 3.0 (сейчас)", {}), ("лонг 3.0 / шорт выкл", {"ext_short": 99}), ("лонг 3.0 / шорт 5.0", {"ext_short": 5.0}),
                 ("лонг 4.0 / шорт выкл", {"ext_long": 4.0, "ext_short": 99})):
    t = ms.run(feats, g)
    print(f"{label:34s}", ms.fmt_stats(ms.stats(t)))
    if "шорт выкл" in label and "лонг 3.0" in label:
        for y, gg in t.groupby(t.t.dt.year):
            print(f"      {y}", ms.fmt_stats(ms.stats(gg)))
        sh = t[t.sg < 0]
        print("      только шорты:", ms.fmt_stats(ms.stats(sh)))
print("\n== Пункт 5, вариант «только уменьшать» (масштаб 0.5–1.0) и по ATR индекса ==")
for cl in ((0.5, 1.0), (0.7, 1.0)):
    res, _ = simulate_sizing(base, 0.8, med_vol, clamp=cl)
    print(f"{'цель = медиана, масштаб ' + str(cl):44s}", res)
