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
             stall_h=12, trail_after=1.0, max_h=48, pull=0.5, wait=2):
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
        if gb_r and mfe >= gb_min:
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

print("== Как в боте сейчас ==")
base = run(feats)
report(base, "стоп 2.5, тейки 0.8R/2.0R, безуб 0.7, защита 0.6 с 0.8")
for sg, g in base.groupby("sg"):
    report(g, f"   {'лонг' if sg > 0 else 'шорт'}")

print("\n== Ширина стопа ==")
for sm in (1.5, 2.0, 2.5, 3.0, 3.5):
    report(run(feats, stop_mult=sm), f"стоп {sm} ATR")

print("\n== Первый тейк (уровень, доля) ==")
for lvl, part in ((0.8, 0.4), (1.0, 0.4), (1.5, 0.4), (1.0, 0.25), (1.5, 0.25)):
    report(run(feats, tps=((lvl, part), (2.0, 0.35))), f"тейк-1 {lvl}R x{part}")
report(run(feats, tps=()), "без частичных тейков (только трейлинг)")
report(run(feats, tps=((2.0, 0.5),)), "один тейк 2.0R x0.5")

print("\n== Защита прибыли (откат от пика) и безубыток ==")
for gb, gmin in ((0.6, 0.8), (0.9, 1.0), (1.2, 1.5), (0, 0)):
    report(run(feats, gb_r=gb, gb_min=gmin), f"защита {gb}R после пика {gmin}R" if gb else "без защиты прибыли")
for be in (0.7, 1.0, 1.5, 0):
    report(run(feats, be_r=be), f"безубыток после {be}R" if be else "без безубытка")

print("\n== Застой и время в сделке ==")
for st, mh in ((12, 48), (24, 48), (0, 48), (12, 96), (0, 96)):
    report(run(feats, stall_h=st, max_h=mh), f"застой {st or 'выкл'}ч, максимум {mh}ч")

print("\n== Принятый вариант по годам (устойчивость) ==")
best = run(feats, stop_mult=2.0, tps=((1.5, 0.25), (2.5, 0.35)), gb_r=1.2, gb_min=1.5, be_r=0.7, max_h=96)
for y, g in best.groupby(best.t.dt.year):
    report(g, f"   {y}")
old_cfg = run(feats)
for y, g in old_cfg.groupby(old_cfg.t.dt.year):
    report(g, f"   {y} (как было)")

print("\n== Комбинации ==")
combos = {
    "стоп 3.0, тейк 1.5x0.3, защита 0.9/1.0, безуб 1.0": dict(stop_mult=3.0, tps=((1.5, 0.3), (3.0, 0.3)), gb_r=0.9, gb_min=1.0, be_r=1.0),
    "стоп 2.5, без тейков, защита 1.0/1.2, безуб 1.0": dict(stop_mult=2.5, tps=(), gb_r=1.0, gb_min=1.2, be_r=1.0),
    "стоп 2.0, тейк 1.0x0.4, защита 0.6/0.8": dict(stop_mult=2.0, tps=((1.0, 0.4), (2.0, 0.35))),
    "стоп 3.0, тейк 1.0x0.5, защита 0.8/1.0, застой выкл": dict(stop_mult=3.0, tps=((1.0, 0.5), (2.5, 0.3)), gb_r=0.8, gb_min=1.0, stall_h=0),
    "стоп 2.5, тейк 1.0x0.4, защита 0.9/1.0, безуб 1.0, застой 24": dict(tps=((1.0, 0.4), (2.0, 0.35)), gb_r=0.9, gb_min=1.0, be_r=1.0, stall_h=24),
}
for label, kw in combos.items():
    report(run(feats, **kw), label)
