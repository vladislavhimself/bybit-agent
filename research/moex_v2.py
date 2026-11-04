"""Проверка на 7 годах четырёх доработок (без модели, только свечи):
 3) пирамидинг: добор +50% размера при +1.5R, если тренд 4ч в нашу сторону;
 7) выход по дневному тренду: пока цена выше/ниже 20-дневной средней — держим, тайм-стоп не считаем (макс. 30 дней);
 2) вход на откате к 20-дневной средней в дневном тренде (новый тип входа);
 1) дневной сканер: те же правила по дневным свечам (data/history/<T>_D.csv).
Запуск: DAYS=2500 python research/moex_v2.py data/history <файл со списком тикеров>"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import moex_sim as ms  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
H, UNI = sys.argv[1], open(sys.argv[2], encoding="utf-8").read().split()
DAYS = int(os.environ.get("DAYS", "2500"))
FEE, SLIP = ms.FEE, ms.SLIP


def add_daily_sma(feats):
    """20-дневная средняя дневных закрытий, известная на каждом часе (сдвиг на день назад — без заглядывания)."""
    for sym, f in feats.items():
        d = f.close.resample("1D").last().dropna()
        sma = d.rolling(20).mean().shift(1)
        f["sma20d"] = sma.reindex(f.index, method="ffill")
        f["lo_d"] = d.rolling(10).min().shift(1).reindex(f.index, method="ffill")
        f["hi_d"] = d.rolling(10).max().shift(1).reindex(f.index, method="ffill")
    return feats


def simulate2(f, i, sg, stop_mult=2.0, tps=((1.5, 0.25), (2.5, 0.35)), be_r=0.7, tiers=((1.5, 1.5), (3.0, 1.0), (5.0, 0.6)),
              stall_h=24, trail_after=1.0, max_h=96, pull=0.5, wait=2, pyramid=0.0, pyr_r=1.5, daily_exit=False, max_h_daily=720):
    """Как ms.simulate, плюс пирамидинг и выход по дневному тренду. Результат в R от первоначального риска."""
    o, hi, lo, cl = f.open.values, f.high.values, f.low.values, f.close.values
    sma = f.sma20d.values if "sma20d" in f.columns else None
    t4 = f.trend_4h.values
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
    added = False
    tp_state = [[lvl, part, False] for lvl, part in tps]
    horizon = max_h_daily if daily_exit else max_h
    end = min(n, start + horizon)
    exit_bar = None
    for j in range(start, end):
        if (sg > 0 and lo[j] <= stop) or (sg < 0 and hi[j] >= stop):
            ex = stop * (1 - sg * SLIP)
            realized += qty * sg * (ex - entry) / R - qty * FEE * ex / R
            qty = 0
            exit_bar = j
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
        # пирамидинг: добор при +pyr_r R, тренд 4ч в нашу сторону; добор по текущей цене, учитывается как доля позиции
        if pyramid and not added and r_now >= pyr_r and (t4[j] > 0) == (sg > 0) and t4[j] != 0 and qty > 0:
            add_px = cl[j] * (1 + sg * SLIP)
            # добранная часть: её результат = pyramid * sg * (exit - add_px) / R; учитываем через смещение "среднего входа"
            # проще: храним добор отдельно
            added = True
            add_qty, add_entry = pyramid, add_px
            realized -= add_qty * FEE * add_px / R
        if daily_exit and sma is not None and not np.isnan(sma[j]):
            # дневной тренд сломан: закрытие часа по другую сторону 20-дневной средней после того, как сделка была в плюсе
            if hours >= 24 and ((sg > 0 and cl[j] < sma[j]) or (sg < 0 and cl[j] > sma[j])):
                ex = cl[j] * (1 - sg * SLIP)
                realized += qty * sg * (ex - entry) / R - qty * FEE * ex / R
                if added:
                    realized += add_qty * sg * (ex - add_entry) / R - add_qty * FEE * ex / R
                    added = False
                qty = 0
                exit_bar = j
                break
        if not daily_exit and qty > 0 and ((hours >= max_h and r_now < 0.5) or (stall_h and 0.1 <= r_now < 1.0 and hours >= stall_h and j - mfe_bar >= stall_h)):
            ex = cl[j] * (1 - sg * SLIP)
            realized += qty * sg * (ex - entry) / R - qty * FEE * ex / R
            if added:
                realized += add_qty * sg * (ex - add_entry) / R - add_qty * FEE * ex / R
                added = False
            qty = 0
            exit_bar = j
            break
        if be_r and mfe >= be_r:
            be = entry * (1 + sg * 0.0015)
            stop = max(stop, be) if sg > 0 else min(stop, be)
        if tiers and not daily_exit:
            gb_now = None
            for pk, gb in tiers:
                if mfe >= pk:
                    gb_now = gb
            if gb_now is not None:
                gb = entry + sg * (mfe - gb_now) * R
                stop = max(stop, gb) if sg > 0 else min(stop, gb)
        if trail_after and r_now >= trail_after and not daily_exit:
            lad = entry + sg * (int(r_now) - 1 + 0.2) * R
            stop = max(stop, lad) if sg > 0 else min(stop, lad)
        if qty <= 1e-9:
            exit_bar = j
            break
    if exit_bar is None:
        exit_bar = end - 1
    if qty > 1e-9:
        ex = cl[exit_bar]
        realized += qty * sg * (ex - entry) / R - qty * FEE * ex / R
    if added:
        ex = (stop * (1 - sg * SLIP)) if (sg > 0 and lo[exit_bar] <= stop) or (sg < 0 and hi[exit_bar] >= stop) else cl[exit_bar]
        realized += add_qty * sg * (ex - add_entry) / R - add_qty * FEE * ex / R
    return realized - FEE * entry / R, start, exit_bar + 1


def run2(feats, gates=None, entry="trend", **kw):
    """entry='trend' — как в боте; entry='pullback' — откат к 20-дневной средней в дневном тренде (лонг: тренд дня вверх,
    low часа коснулся sma20d, закрытие выше; шорт зеркально)."""
    G = dict(ms.DEFAULT_GATES, **(gates or {}))
    res = []
    for sym, f in feats.items():
        sc, rg, t4, td = f.score.values, f.regime.values, f.trend_4h.values, f.trend_d.values
        r24, ap = f.ret24.values, f.atr_pct.values
        sma, lo, hi, cl = f.sma20d.values, f.low.values, f.high.values, f.close.values
        idx = f.index
        busy = -1
        for i in range(30, len(f) - 6):
            if i <= busy:
                continue
            if entry == "trend":
                if abs(0.6 * sc[i]) < G["thresh"]:
                    continue
                sg = 1 if sc[i] > 0 else -1
                if rg[i] == "range" and not (G["range_long"] if sg > 0 else G["range_short"]):
                    continue
                if rg[i] == "high_vol" and not G["high_vol"]:
                    continue
                if t4[i] != 0 and (t4[i] > 0) != (sg > 0) and not G["counter_4h"]:
                    continue
                if sg * r24[i] > 15:
                    continue
                ext = G["ext_long"] if sg > 0 else G["ext_short"]
                if sg * r24[i] / ap[i] >= ext:
                    continue
            else:
                if np.isnan(sma[i]) or td[i] == 0:
                    continue
                sg = 1 if td[i] > 0 else -1
                touched = (lo[i] <= sma[i] <= cl[i]) if sg > 0 else (hi[i] >= sma[i] >= cl[i])
                if not touched:
                    continue
                if rg[i] == "high_vol":
                    continue
            out = simulate2(f, i, sg, **kw)
            if out is None:
                continue
            res.append((idx[i], sym, sg, out[0], idx[min(out[2] - 1, len(idx) - 1)]))
            busy = out[1] + G["busy"]
    return pd.DataFrame(res, columns=["t", "sym", "sg", "r", "t_exit"])


def rep(label, t):
    s = ms.stats(t)
    print(f"{label:52s} {ms.fmt_stats(s)}")
    return s


feats = add_daily_sma(ms.load(H, UNI, DAYS))
print(f"бумаг {len(feats)}, дней {DAYS}\n")
print("== База (как в боте сейчас, застряла 24) ==")
base = run2(feats)
rep("база", base)

print("\n== 3) Пирамидинг (добор при +1.5R, тренд 4ч за) ==")
for p in (0.5, 1.0):
    t = run2(feats, pyramid=p)
    rep(f"добор +{p * 100:.0f}% размера", t)
    for y, g in t.groupby(t.t.dt.year):
        b = base[base.t.dt.year == y]
        print(f"      {y}: с добором {ms.stats(g)['sum_r']:+7.1f}R   база {ms.stats(b)['sum_r']:+7.1f}R")
    if p == 0.5:
        rep("   добор при +1.0R", run2(feats, pyramid=0.5, pyr_r=1.0))
        rep("   добор при +2.0R", run2(feats, pyramid=0.5, pyr_r=2.0))

print("\n== 7) Выход по дневному тренду (вместо тайм-стопа и защиты прибыли; стоп и безубыток остаются) ==")
t7 = run2(feats, daily_exit=True)
rep("выход: закрытие за 20-дневной средней", t7)
for y, g in t7.groupby(t7.t.dt.year):
    b = base[base.t.dt.year == y]
    print(f"      {y}: дневной выход {ms.stats(g)['sum_r']:+7.1f}R   база {ms.stats(b)['sum_r']:+7.1f}R")
print("   среднее удержание, ч: база", round(((base.t_exit - base.t).dt.total_seconds() / 3600).mean()), "дневной выход", round(((t7.t_exit - t7.t).dt.total_seconds() / 3600).mean()))
rep("выход по дневному + пирамидинг 50%", run2(feats, daily_exit=True, pyramid=0.5))

print("\n== 2) Вход на откате к 20-дневной средней в дневном тренде ==")
t2 = run2(feats, entry="pullback")
rep("откат к SMA20d, выходы как в боте", t2)
for sg, g in t2.groupby("sg"):
    rep(f"   {'лонг' if sg > 0 else 'шорт'}", g)
for y, g in t2.groupby(t2.t.dt.year):
    print(f"      {y}", ms.fmt_stats(ms.stats(g)))
rep("откат к SMA20d, выход по дневному тренду", run2(feats, entry="pullback", daily_exit=True))

print("\n== 1) Дневной сканер: те же правила на дневных свечах ==")
dfeats = {}
for sym in UNI:
    p = os.path.join(H, f"{sym}_D.csv")
    if not os.path.exists(p):
        continue
    df = pd.read_csv(p)
    if df.ts.max() < 1e11:
        df["ts"] = df["ts"] * 1000
    df = df[df.ts >= df.ts.max() - DAYS * 86400000].reset_index(drop=True)
    if len(df) < 400:
        continue
    f = ms.features(df)          # признаки те же, но каждая «свеча» — день: тренд1ч = тренд 50/200 дней и т.д.
    dfeats[sym] = f
print(f"бумаг с дневной историей: {len(dfeats)}")
if dfeats:
    for sym, f in dfeats.items():
        d = f.close
        f["sma20d"] = d.rolling(20).mean().shift(1)
    for lab, kw in (("дневки: стоп 2 ATR(д), тейки 1.5/2.5, макс 30 дней", dict(max_h=30, stall_h=10)),
                    ("дневки: без тейков, выход по тренду 20 дн.", dict(tps=(), daily_exit=True, max_h_daily=120))):
        t = run2(dfeats, {"busy": 2, "ext_long": 3.0, "ext_short": 99}, **kw)
        rep(lab, t)
        for y, g in t.groupby(t.t.dt.year):
            print(f"      {y}", ms.fmt_stats(ms.stats(g)))
