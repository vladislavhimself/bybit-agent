"""Геометрия выходов на Мосбирже: стоп, тейки, безубыток, защита прибыли, трейлинг — на часовой истории
(data/history/<TICKER>_60.csv, см. scripts/fetch_moex_history.py). Входы — технические, как в боте (сила ≥ порога,
трендовый режим, вход на откате 0.5 ATR, фильтр растяжения <2 ATR). Комиссии как у T-Invest на фьючерсах (0.04%
за сторону) плюс проскальзывание 0.05%. Печатает сетку вариантов: сделок, средний R, PF, доля плюсов, средний плюс/минус.
Запуск: python research/moex_exits.py data/history "SBER GAZP LKOH ..." """
import itertools
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_rules"))
import bt_entries as bt2  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
H, UNI = sys.argv[1], open(sys.argv[2], encoding="utf-8").read()
FEE, SLIP = 0.0004, 0.0005
DAYS = int(os.environ.get("DAYS", "500"))
THRESH = float(os.environ.get("THRESH", "0.55"))


def simulate(f, i, sg, stop_mult=2.5, tps=((0.8, 0.40), (2.0, 0.35)), be_r=0.7, gb_r=0.6, gb_min=0.8,
             stall_h=12, trail_after=1.0, max_h=48, pull=0.5, wait=2, tiers=None):
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
    tp_state = [[lvl, part, False] for lvl, part in tps]
    end = min(n, start + max_h)
    for j in range(start, end):
        if (sg > 0 and lo[j] <= stop) or (sg < 0 and hi[j] >= stop):
            ex = stop * (1 - sg * SLIP)
            realized += qty * sg * (ex - entry) / R - qty * FEE * ex / R
            qty = 0
            break
        fav = hi[j] if sg > 0 else lo[j]
        for tp in tp_state:
            if not tp[2] and sg * (fav - entry) >= tp[0] * R and qty > 0:
                take = min(tp[1], qty)
                realized += take * tp[0] - take * FEE * entry / R
                qty -= take
                tp[2] = True
        cf = sg * (fav - entry) / R
        if cf > mfe:
            mfe, mfe_bar = cf, j
        r_now = sg * (cl[j] - entry) / R
        hours = j - start + 1
        if qty > 0 and ((hours >= max_h and r_now < 0.5) or (stall_h and 0.1 <= r_now < 1.0 and hours >= stall_h and j - mfe_bar >= stall_h)):
            ex = cl[j] * (1 - sg * SLIP)
            realized += qty * sg * (ex - entry) / R - qty * FEE * ex / R
            qty = 0
            break
        if be_r and mfe >= be_r:
            be = entry * (1 + sg * 0.0015)
            stop = max(stop, be) if sg > 0 else min(stop, be)
        if tiers:
            gb_now = None
            for pk, gb in tiers:
                if mfe >= pk:
                    gb_now = gb
            if gb_now is not None:
                gb = entry + sg * (mfe - gb_now) * R
                stop = max(stop, gb) if sg > 0 else min(stop, gb)
        elif gb_r and mfe >= gb_min:
            gb = entry + sg * (mfe - gb_r) * R
            stop = max(stop, gb) if sg > 0 else min(stop, gb)
        if trail_after and r_now >= trail_after:
            lad = entry + sg * (int(r_now) - 1 + 0.2) * R
            stop = max(stop, lad) if sg > 0 else min(stop, lad)
        if qty <= 1e-9:
            break
    if qty > 1e-9:
        ex = cl[end - 1]
        realized += qty * sg * (ex - entry) / R - qty * FEE * ex / R
    return realized - FEE * entry / R, start


def run(feats, **kw):
    res = []
    for sym, f in feats.items():
        sc, rg, t4 = f.score.values, f.regime.values, f.trend_4h.values
        r24, ap = f.ret24.values, f.atr_pct.values
        busy = -1
        for i in range(len(f) - 6):
            if i <= busy or abs(0.6 * sc[i]) < THRESH:
                continue
            sg = 1 if sc[i] > 0 else -1
            if rg[i] != "trend" or (t4[i] != 0 and (t4[i] > 0) != (sg > 0)) or sg * r24[i] > 15:
                continue
            if sg * r24[i] / ap[i] >= 2:
                continue
            out = simulate(f, i, sg, **kw)
            if out is None:
                continue
            # в CSV Мосбиржи ts в секундах, а features считает его миллисекундами: дату берём из исходного ts
            t = pd.to_datetime(f["ts"].values[i], unit="s") if "ts" in f.columns else f.index[i]
            res.append((t, sym, sg, out[0]))
            busy = out[1] + 12
    return pd.DataFrame(res, columns=["t", "sym", "sg", "r"])


def report(t, label):
    if not len(t):
        print(f"{label:46s} сделок 0")
        return
    w, l = t.r[t.r > 0], t.r[t.r <= 0]
    pf = w.sum() / -l.sum() if len(l) and l.sum() < 0 else float("inf")
    print(f"{label:46s} сделок {len(t):4d}  средн {t.r.mean():+.3f}R  PF {pf:4.2f}  в плюс {(t.r > 0).mean() * 100:4.1f}%  "
          f"ср.плюс {w.mean() if len(w) else 0:+.2f}R  ср.минус {l.mean() if len(l) else 0:+.2f}R  итог {t.r.sum():+6.1f}R")


feats = {}
for sym in UNI.split():
    p = os.path.join(H, f"{sym}_60.csv")
    if not os.path.exists(p):
        continue
    df = pd.read_csv(p)
    if df.ts.max() < 1e11:   # история Мосбиржи сохранена в секундах, бэктестер ждёт миллисекунды
        df["ts"] = df["ts"] * 1000
    df = df[df.ts >= df.ts.max() - DAYS * 86400000]
    df = df[df.volume.cumsum() > 0].reset_index(drop=True)
    if len(df) >= 1500:
        feats[sym] = bt2.features(df)
print(f"бумаг: {len(feats)}, дней: {DAYS}, порог: {THRESH}, комиссия {FEE * 100:.2f}% + проскальзывание {SLIP * 100:.2f}%\n")


BASE = dict(stop_mult=2.0, tps=((1.5, 0.25), (2.5, 0.35)), be_r=0.7, max_h=96)
print("== Защита прибыли: как в боте и ступенчатые варианты (стоп 2.0, тейки 1.5/2.5, безуб 0.7, 96ч) ==")
variants = {
    "как в боте: откат 1.2R после пика 1.5R": dict(gb_r=1.2, gb_min=1.5),
    "ступени: 1.2R до 3R, 0.8R с 3R, 0.5R с 5R": dict(tiers=[(1.5, 1.2), (3.0, 0.8), (5.0, 0.5)]),
    "ступени: 1.2R до 2R, 0.7R с 2R, 0.4R с 4R": dict(tiers=[(1.5, 1.2), (2.0, 0.7), (4.0, 0.4)]),
    "ступени: 1.5R до 3R, 1.0R с 3R, 0.6R с 5R": dict(tiers=[(1.5, 1.5), (3.0, 1.0), (5.0, 0.6)]),
    "жёстко: 0.6R после пика 0.8R": dict(gb_r=0.6, gb_min=0.8),
    "держать до конца: без защиты, только лестница": dict(gb_r=0, gb_min=0),
    "держать до конца: без защиты и без лестницы": dict(gb_r=0, gb_min=0, trail_after=0),
}
res = {}
for label, kw in variants.items():
    res[label] = run(feats, **{**BASE, **kw})
    report(res[label], label)
print("\n== Только сделки, где пик был >= 3R (где выбор и стоит) ==")
# сравниваем по тем же входам: берём сделки из варианта «держать до конца» с большим итогом как прокси пика
for label, t in res.items():
    big = t.merge(res["держать до конца: без защиты, только лестница"][["t","sym"]], on=["t","sym"])
    report(big, label)
print("\n== По годам: как в боте vs ступени 1.2/0.8/0.5 ==")
for label in ("как в боте: откат 1.2R после пика 1.5R", "ступени: 1.2R до 3R, 0.8R с 3R, 0.5R с 5R"):
    for y, g in res[label].groupby(res[label].t.dt.year):
        report(g, f"   {y} {label[:12]}")
