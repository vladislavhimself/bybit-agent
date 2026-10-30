"""Чистый симулятор технических входов и выходов бота на часовой истории Мосбиржи (без побочных эффектов,
используется ботом для еженедельного перерасчёта параметров и исследованиями).

Входы — как в боте: сила ТА ≥ порога (0.6·score), тренд/флэт по правилам ворот, вход лимиткой на откате 0.5 ATR,
фильтр растяжения. Выходы: стоп в ATR, частичные тейки, безубыток, ступенчатая защита прибыли, лестница трейлинга,
застой, тайм-стоп. Комиссия 0.04% за сторону + проскальзывание 0.05%. Результат сделки в R (риск = стоп)."""
import os
import sys

import numpy as np
import pandas as pd

FEE, SLIP = 0.0004, 0.0005


# ---- признаки (копия research/live_rules/bt_entries.py, без зависимости от argv) ----
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(c, n=14):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return (100 - 100 / (1 + up / dn.replace(0, np.nan))).fillna(50)


def atr(df, n=14):
    pc = df.close.shift(1)
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def adx(df, n=14):
    up = df.high.diff()
    dn = -df.low.diff()
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    a = atr(df, 1).ewm(alpha=1 / n, adjust=False).mean().replace(0, np.nan)
    pdi = 100 * pd.Series(pdm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / a
    mdi = 100 * pd.Series(mdm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / a
    return (100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)).ewm(alpha=1 / n, adjust=False).mean().fillna(0)


def features(df):
    df = df.copy()
    df["t"] = pd.to_datetime(df.ts, unit="ms")
    df = df.set_index("t")
    c = df.close
    f = pd.DataFrame(index=df.index)
    e50, e200 = ema(c, 50), ema(c, 200)
    f["trend_1h"] = np.where((c > e50) & (e50 > e200), 1, np.where((c < e50) & (e50 < e200), -1, 0))
    h4 = df.resample("4h", label="right", closed="right").agg({"close": "last"}).dropna()
    e20_4, e50_4 = ema(h4.close, 20), ema(h4.close, 50)
    t4 = np.where((h4.close > e20_4) & (e20_4 > e50_4), 1, np.where((h4.close < e20_4) & (e20_4 < e50_4), -1, 0))
    f["trend_4h"] = pd.Series(t4, index=h4.index).reindex(df.index, method="ffill").fillna(0)
    hi20 = df.high.shift(1).rolling(20).max()
    lo20 = df.low.shift(1).rolling(20).min()
    f["breakout"] = np.where(c > hi20, 1, np.where(c < lo20, -1, 0))
    f["ret24"] = (c / c.shift(24) - 1) * 100
    f["ret4"] = (c / c.shift(4) - 1) * 100
    ml = ema(c, 12) - ema(c, 26)
    mh = ml - ema(ml, 9)
    f["macd_sign"] = np.where(mh > 0, 1, -1)
    f["macd_rising"] = mh > mh.shift(3)
    f["vol_ratio"] = df.volume / df.volume.shift(1).rolling(20).mean()
    a = atr(df)
    f["atr"] = a
    f["atr_pct"] = a / c * 100
    f["atr_rank"] = (a / c).rolling(100).rank(pct=True)
    f["adx"] = adx(df)
    f["rsi"] = rsi(c)
    d = df.resample("1D", label="right", closed="right").agg({"high": "max", "low": "min", "close": "last"}).dropna()
    sma50, sma200 = d.close.rolling(50).mean(), d.close.rolling(200, min_periods=100).mean()
    td = np.where((d.close > sma200) & (sma50 > sma200), 1, np.where((d.close < sma200) & (sma50 < sma200), -1, 0))
    rd = rsi(d.close)
    hi20d = d.high.shift(1).rolling(20).max()
    lo20d = d.low.shift(1).rolling(20).min()
    bd = np.where(d.close > hi20d, 1, np.where(d.close < lo20d, -1, 0))
    dd = pd.DataFrame({"trend_d": td, "rsi_d": rd, "breakout_d": bd}, index=d.index)
    f = f.join(dd.reindex(df.index, method="ffill"))
    # оценка как в bot/ta.py (без уровней, дивергенции и сжатия Боллинджера)
    s = 0.4 * f.trend_1h + 0.25 * f.trend_4h + 0.25 * f.breakout + 0.10 * (f.ret24 / 5).clip(-1, 1)
    s = s + 0.08 * f.macd_sign * np.where(f.macd_rising == (f.macd_sign > 0), 1.0, 0.5)
    s = s + 0.15 * f.trend_d.fillna(0)
    s = s + np.where((f.trend_d > 0) & ((f.rsi_d > 70) | (f.breakout_d > 0)), 0.1, 0)
    s = s - np.where((f.trend_d < 0) & (f.breakout_d < 0), 0.1, 0)
    s = s + np.where((f.breakout != 0) & (f.vol_ratio > 1.5), 0.1 * f.breakout, 0)
    s = np.where(((f.rsi > 75) & (s > 0)) | ((f.rsi < 25) & (s < 0)), s * 0.5, s)
    s = np.where(((f.trend_d < 0) & (s > 0)) | ((f.trend_d > 0) & (s < 0)), s * 0.75, s)
    f["score"] = np.clip(s, -1, 1)
    f["regime"] = np.where(f.atr_rank >= 0.92, "high_vol",
                           np.where((f.adx >= 22) & (f.trend_1h != 0), "trend", "range"))
    f["open"], f["high"], f["low"], f["close"] = df.open, df.high, df.low, df.close
    f["dow"], f["hour"] = df.index.dayofweek, df.index.hour
    return f.iloc[250:]



DEFAULT_GATES = {"thresh": 0.55, "ext_long": 3.0, "ext_short": 3.0, "range_long": False, "range_short": True,
                 "high_vol": False, "counter_4h": False, "busy": 3}
DEFAULT_EXITS = dict(stop_mult=2.0, tps=((1.5, 0.25), (2.5, 0.35)), be_r=0.7, tiers=((1.5, 1.5), (3.0, 1.0), (5.0, 0.6)),
                     stall_h=12, trail_after=1.0, max_h=96)


def load(hist_dir, symbols, days):
    """Признаки по бумагам из data/history/<T>_60.csv за последние days дней."""
    feats = {}
    for sym in symbols:
        p = os.path.join(hist_dir, f"{sym}_60.csv")
        if not os.path.exists(p):
            continue
        df = pd.read_csv(p)
        if df.empty:
            continue
        if df.ts.max() < 1e11:   # история Мосбиржи в секундах, bt_entries ждёт миллисекунды
            df["ts"] = df["ts"] * 1000
        df = df[df.ts >= df.ts.max() - days * 86400000]
        df = df[df.volume.cumsum() > 0].reset_index(drop=True)
        if len(df) >= 1500:
            feats[sym] = features(df)
    return feats


def simulate(f, i, sg, stop_mult=2.0, tps=((1.5, 0.25), (2.5, 0.35)), be_r=0.7, gb_r=0.0, gb_min=0.0,
             stall_h=12, trail_after=1.0, max_h=96, pull=0.5, wait=2, tiers=None):
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
    return realized - FEE * entry / R, start, end


def run(feats, gates=None, **exit_kw):
    """Все сделки по правилам: DataFrame(t, sym, sg, r, t_exit)."""
    G = dict(DEFAULT_GATES, **(gates or {}))
    kw = dict(DEFAULT_EXITS, **exit_kw)
    res = []
    for sym, f in feats.items():
        sc, rg, t4 = f.score.values, f.regime.values, f.trend_4h.values
        r24, ap = f.ret24.values, f.atr_pct.values
        idx = f.index
        busy = -1
        for i in range(len(f) - 6):
            if i <= busy or abs(0.6 * sc[i]) < G["thresh"]:
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
            out = simulate(f, i, sg, **kw)
            if out is None:
                continue
            t = idx[i]
            t_exit = idx[min(out[2] - 1, len(idx) - 1)]
            res.append((t, sym, sg, out[0], t_exit))
            busy = out[1] + G["busy"]
    return pd.DataFrame(res, columns=["t", "sym", "sg", "r", "t_exit"])


def stats(t):
    if t is None or not len(t):
        return {"n": 0, "avg_r": 0.0, "pf": 0.0, "win": 0.0, "sum_r": 0.0, "score": -9.0}
    w, l = t.r[t.r > 0], t.r[t.r <= 0]
    pf = float(w.sum() / -l.sum()) if len(l) and l.sum() < 0 else 9.0
    avg = float(t.r.mean())
    return {"n": int(len(t)), "avg_r": round(avg, 4), "pf": round(pf, 3), "win": round(float((t.r > 0).mean()), 3),
            "sum_r": round(float(t.r.sum()), 1), "score": round(avg * np.sqrt(len(t)), 3)}   # ≈ t-статистика


def fmt_stats(s):
    return f"сделок {s['n']}, средний {s['avg_r']:+.3f}R, PF {s['pf']:.2f}, в плюс {s['win'] * 100:.0f}%, итог {s['sum_r']:+.1f}R"
