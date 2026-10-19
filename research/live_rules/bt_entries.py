"""Бэктест TA-входов бота на часовых свечах с выходами как в боте (стоп 2 ATR, безубыток 0.7R, тейки 0.8R/2R,
защита от отката 0.6R, застрявшая 12ч, тайм-стоп 48ч) и комиссиями. Варианты фильтров сравниваются в R."""
import os
import sys

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
H = sys.argv[1]
SYMS = open(sys.argv[2]).read().split()
DAYS = 400


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


FEE_TAKER, FEE_MAKER, SLIP = 0.00055, 0.0002, 0.0002


def simulate(f, i, sg, entry_mode="pullback"):
    """Сделка по сигналу на закрытии бара i. Возвращает (результат в R после комиссий, бар входа) или None."""
    o, hi, lo, cl = f.open.values, f.high.values, f.low.values, f.close.values
    a = f.atr.values[i]
    px = cl[i]
    n = len(cl)
    if entry_mode == "pullback":
        lim = px - sg * 0.25 * a
        j0 = None
        for j in (i + 1, i + 2):
            if j < n and ((sg > 0 and lo[j] <= lim) or (sg < 0 and hi[j] >= lim)):
                j0 = j
                break
        if j0 is None:
            return None
        entry, fee_in, start = lim, FEE_MAKER, j0
    else:
        if i + 1 >= n:
            return None
        entry, fee_in, start = o[i + 1] * (1 + sg * SLIP), FEE_TAKER, i + 1
    R = 2.0 * a
    stop = entry - sg * R
    qty, realized, mfe, mfe_bar = 1.0, 0.0, 0.0, start
    tps = [[0.8, 0.40, False], [2.0, 0.35, False]]
    end = min(n, start + 96)
    for j in range(start, end):
        # 1) стоп раньше тейков (консервативно)
        if (sg > 0 and lo[j] <= stop) or (sg < 0 and hi[j] >= stop):
            ex = stop * (1 - sg * SLIP)
            realized += qty * sg * (ex - entry) / R - qty * FEE_TAKER * ex / R
            qty = 0
            break
        # 2) частичные тейки
        fav = hi[j] if sg > 0 else lo[j]
        for tp in tps:
            if not tp[2] and sg * (fav - entry) >= tp[0] * R:
                realized += tp[1] * tp[0] - tp[1] * FEE_MAKER * entry / R
                qty -= tp[1]
                tp[2] = True
        cur_fav = sg * (fav - entry) / R
        if cur_fav > mfe:
            mfe, mfe_bar = cur_fav, j
        r_now = sg * (cl[j] - entry) / R
        hours = j - start + 1
        # 3) выходы по времени на закрытии бара
        if (hours >= 48 and r_now < 0.5) or (0.1 <= r_now < 1.0 and hours >= 12 and j - mfe_bar >= 12):
            ex = cl[j] * (1 - sg * SLIP)
            realized += qty * sg * (ex - entry) / R - qty * FEE_TAKER * ex / R
            qty = 0
            break
        # 4) перенос стопа к следующему бару
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
        realized += qty * sg * (ex - entry) / R - qty * FEE_TAKER * ex / R
    return realized - fee_in * entry / R, start


def run(feats, rule, entry_mode="pullback"):
    res = []
    for sym, f in feats.items():
        sc, rg, t4 = f.score.values, f.regime.values, f.trend_4h.values
        r24, r4, ap = f.ret24.values, f.ret4.values, f.atr_pct.values
        dow, rs = f.dow.values, f.rsi.values
        busy_until = -1
        for i in range(len(f) - 3):
            if i <= busy_until:
                continue
            s = sc[i]
            if abs(0.6 * s) < 0.55:
                continue
            sg = 1 if s > 0 else -1
            if rg[i] != "trend":            # флэт и экстремальная волатильность без новости не торгуются
                continue
            if t4[i] != 0 and (t4[i] > 0) != (sg > 0):   # против 4ч тренда без новости нет
                continue
            if sg * r24[i] > 15:            # погоня за движением >15%
                continue
            ctx = dict(ext24=sg * r24[i] / ap[i], ext4=sg * r4[i] / ap[i], dow=dow[i], rsi=rs[i], sg=sg)
            if not rule(ctx):
                continue
            out = simulate(f, i, sg, entry_mode)
            if out is None:
                continue
            r, start = out
            res.append((f.index[i], sym, sg, r, ctx["ext24"]))
            busy_until = start + 12
    return pd.DataFrame(res, columns=["t", "sym", "sg", "r", "ext24"]).sort_values("t").reset_index(drop=True)


def report(t, label):
    if t.empty:
        print(f"{label:60s} сделок 0")
        return
    w = t[t.r > 0]
    gl = -t[t.r <= 0].r.sum()
    pf = w.r.sum() / gl if gl > 0 else float("inf")
    eq = t.r.cumsum()
    dd = (eq.cummax() - eq).max()
    half = t.t.iloc[len(t) // 2]
    a1, a2 = t[t.t < half].r.mean(), t[t.t >= half].r.mean()
    print(f"{label:60s} сделок {len(t):4d}  в плюс {len(w) / len(t) * 100:4.1f}%  средн. {t.r.mean():+.3f}R  "
          f"PF {pf:4.2f}  итог {t.r.sum():+6.1f}R  просадка {dd:5.1f}R | половины {a1:+.3f} / {a2:+.3f}")


if __name__ == "__main__":
    feats = {}
    for sym in SYMS:
        p = os.path.join(H, f"{sym}_60.csv")
        if not os.path.exists(p):
            continue
        df = pd.read_csv(p)
        df = df[df.ts >= df.ts.max() - DAYS * 86400000]
        df = df[df.volume.cumsum() > 0].reset_index(drop=True)
        if len(df) < 2000:
            continue
        feats[sym] = features(df)
    print("пар:", len(feats), "| часов истории:", sum(len(f) for f in feats.values()))
    base = run(feats, lambda c: True)
    report(base, "A. как сейчас в боте (только теханализ)")
    for k in (2, 3, 4):
        report(run(feats, lambda c, k=k: c["ext24"] < k), f"B. A + пропуск, если за 24ч уже прошло >={k} ATR")
    report(run(feats, lambda c: c["ext24"] < 3 and c["ext4"] < 2), "B2. A + за 24ч <3 ATR и за 4ч <2 ATR")
    report(run(feats, lambda c: c["dow"] < 5), "C. A + без входов в выходные")
    report(run(feats, lambda c: c["ext24"] < 3 and c["dow"] < 5), "D. 24ч <3 ATR + без выходных")
    report(run(feats, lambda c: True, entry_mode="market"), "E. A, вход по рынку вместо отката")
    print("\nКорзины по растяжению (вариант A): сколько ATR цена уже прошла за 24ч в сторону сделки")
    b = base.copy()
    b["bucket"] = pd.cut(b.ext24, [-99, 0, 1, 2, 3, 4, 6, 99])
    for k, g in b.groupby("bucket", observed=True):
        print(f"  {str(k):14s} сделок {len(g):4d}  средн. {g.r.mean():+.3f}R  в плюс {(g.r > 0).mean() * 100:4.1f}%")
    base.to_csv(os.path.join(os.path.dirname(sys.argv[2]), "bt2_base.csv"), index=False)
