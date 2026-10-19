"""Доп. проверки: глубина отката, ширина стопа, кластеры одновременных сигналов. Использует функции из bt2.py."""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bt_entries as bt2  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
H, UNI = sys.argv[1], sys.argv[2]


def simulate_p(f, i, sg, pull=0.25, stop_mult=2.0, tp1=0.8, wait=2):
    o, hi, lo, cl = f.open.values, f.high.values, f.low.values, f.close.values
    a = f.atr.values[i]
    n = len(cl)
    lim = cl[i] - sg * pull * a
    j0 = None
    for j in range(i + 1, i + 1 + wait):
        if j < n and ((sg > 0 and lo[j] <= lim) or (sg < 0 and hi[j] >= lim)):
            j0 = j
            break
    if j0 is None:
        return None
    entry, start = lim, j0
    R = stop_mult * a
    stop = entry - sg * R
    qty, realized, mfe, mfe_bar = 1.0, 0.0, 0.0, start
    tps = [[tp1, 0.40, False], [2.0, 0.35, False]]
    end = min(n, start + 96)
    F_T, F_M, SL = bt2.FEE_TAKER, bt2.FEE_MAKER, bt2.SLIP
    for j in range(start, end):
        if (sg > 0 and lo[j] <= stop) or (sg < 0 and hi[j] >= stop):
            ex = stop * (1 - sg * SL)
            realized += qty * sg * (ex - entry) / R - qty * F_T * ex / R
            qty = 0
            break
        fav = hi[j] if sg > 0 else lo[j]
        for tp in tps:
            if not tp[2] and sg * (fav - entry) >= tp[0] * R:
                realized += tp[1] * tp[0] - tp[1] * F_M * entry / R
                qty -= tp[1]
                tp[2] = True
        cf = sg * (fav - entry) / R
        if cf > mfe:
            mfe, mfe_bar = cf, j
        r_now = sg * (cl[j] - entry) / R
        hours = j - start + 1
        if (hours >= 48 and r_now < 0.5) or (0.1 <= r_now < 1.0 and hours >= 12 and j - mfe_bar >= 12):
            ex = cl[j] * (1 - sg * SL)
            realized += qty * sg * (ex - entry) / R - qty * F_T * ex / R
            qty = 0
            break
        if mfe >= 0.7:
            be = entry * (1 + sg * 0.0015)
            stop = max(stop, be) if sg > 0 else min(stop, be)
        if mfe >= 0.8:
            gb = entry + sg * (mfe - 0.6) * R
            stop = max(stop, gb) if sg > 0 else min(stop, gb)
        if r_now >= 1.0:
            lad = entry + sg * (int(r_now) - 1 + 0.2) * R
            stop = max(stop, lad) if sg > 0 else min(stop, lad)
    if qty > 0:
        ex = cl[end - 1]
        realized += qty * sg * (ex - entry) / R - qty * F_T * ex / R
    return realized - F_M * entry / R, start


def run_p(feats, rule, **kw):
    res = []
    for sym, f in feats.items():
        sc, rg, t4 = f.score.values, f.regime.values, f.trend_4h.values
        r24, ap = f.ret24.values, f.atr_pct.values
        busy = -1
        for i in range(len(f) - 6):
            if i <= busy or abs(0.6 * sc[i]) < 0.55:
                continue
            sg = 1 if sc[i] > 0 else -1
            if rg[i] != "trend" or (t4[i] != 0 and (t4[i] > 0) != (sg > 0)) or sg * r24[i] > 15:
                continue
            if not rule(sg * r24[i] / ap[i]):
                continue
            out = simulate_p(f, i, sg, **kw)
            if out is None:
                continue
            res.append((f.index[i], sym, sg, out[0]))
            busy = out[1] + 12
    return pd.DataFrame(res, columns=["t", "sym", "sg", "r"]).sort_values("t").reset_index(drop=True)


feats = {}
for sym in open(UNI).read().split():
    p = os.path.join(H, f"{sym}_60.csv")
    if not os.path.exists(p):
        continue
    df = pd.read_csv(p)
    df = df[df.ts >= df.ts.max() - bt2.DAYS * 86400000]
    df = df[df.volume.cumsum() > 0].reset_index(drop=True)
    if len(df) >= 2000:
        feats[sym] = bt2.features(df)

flt = lambda e: e < 2  # noqa: E731
print("== Глубина отката (с фильтром растяжения <2 ATR), стоп 2 ATR ==")
for pull in (0.0, 0.15, 0.25, 0.4, 0.6):
    bt2.report(run_p(feats, flt, pull=pull), f"откат {pull} ATR")
print("\n== Ширина стопа (откат 0.25, фильтр <2 ATR) ==")
for sm in (1.5, 2.0, 2.5, 3.0):
    bt2.report(run_p(feats, flt, stop_mult=sm), f"стоп {sm} ATR")
print("\n== Первый тейк ==")
for tp in (0.6, 0.8, 1.0):
    bt2.report(run_p(feats, flt, tp1=tp), f"тейк-1 на {tp}R")

print("\n== Одновременные сигналы в одну сторону (вариант без фильтра) ==")
b = run_p(feats, lambda e: True)
b["hour"] = b.t.dt.floor("h")
b["cl"] = b.groupby(["hour", "sg"]).sym.transform("count")
for k, g in b.groupby(pd.cut(b.cl, [0, 1, 2, 3, 99]), observed=True):
    print(f"  в тот же час в ту же сторону сигналов: {str(k):8s} сделок {len(g):4d}  средн. {g.r.mean():+.3f}R  "
          f"в плюс {(g.r > 0).mean() * 100:4.1f}%")
print("\n== Лонг и шорт отдельно (с фильтром <2 ATR) ==")
t = run_p(feats, flt)
for sg, g in t.groupby("sg"):
    print(f"  {'лонг' if sg > 0 else 'шорт'}: сделок {len(g)}, средн. {g.r.mean():+.3f}R, в плюс {(g.r > 0).mean() * 100:.1f}%")
print("\n== По месяцам (с фильтром <2 ATR) ==")
for m, g in t.groupby(t.t.dt.to_period("M")):
    print(f"  {m}: сделок {len(g):3d}, итог {g.r.sum():+6.2f}R, средн. {g.r.mean():+.3f}R")
