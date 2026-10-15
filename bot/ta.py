"""Технический анализ: индикаторы и сводка по инструменту.
Тренд (EMA 1ч/4ч), сила тренда (ADX), пробой канала, RSI и дивергенция, MACD, Bollinger (сжатие/расширение),
уровни поддержки/сопротивления по свингам, объём, тайминг входа по 15м."""
import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = atr(df, 1)
    atr_n = tr.ewm(alpha=1 / n, adjust=False).mean().replace(0, np.nan)
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr_n
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr_n
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean().fillna(0)


def macd(close: pd.Series, fast=12, slow=26, sig=9):
    line = ema(close, fast) - ema(close, slow)
    signal = ema(line, sig)
    return line, signal, line - signal


def bollinger(close: pd.Series, n=20, k=2.0):
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std()
    upper, lower = mid + k * sd, mid - k * sd
    pct_b = (close - lower) / (upper - lower).replace(0, np.nan)
    width = (upper - lower) / mid
    return pct_b, width


def swing_levels(df: pd.DataFrame, lookback=120, w=5):
    """Локальные максимумы/минимумы (пивоты) за lookback баров."""
    d = df.iloc[-lookback:]
    highs, lows = d["high"].values, d["low"].values
    res, sup = [], []
    for i in range(w, len(d) - w):
        if highs[i] == highs[i - w:i + w + 1].max():
            res.append(float(highs[i]))
        if lows[i] == lows[i - w:i + w + 1].min():
            sup.append(float(lows[i]))
    return res, sup


def _trend(close, fast, slow):
    if close > fast > slow:
        return 1
    if close < fast < slow:
        return -1
    return 0


def snapshot(df1h: pd.DataFrame, df4h: pd.DataFrame | None = None, df15: pd.DataFrame | None = None,
             dfd: pd.DataFrame | None = None) -> dict | None:
    """Сводка по инструменту. Нужно минимум 120 часовых свечей. dfd = дневные свечи (режим старшего таймфрейма)."""
    if df1h is None or len(df1h) < 120:
        return None
    # Дневной режим: по 15 годам BTC импульс на дневках (цена выше SMA200, RSI>70, пробой максимума) даёт
    # +9..+15% преимущества за 20 дней, а покупка просадок преимущества не даёт (research/ta_rules_50y.py)
    trend_d, rsi_d, breakout_d = 0, 50.0, 0
    if dfd is not None and len(dfd) >= 60:
        cd = dfd["close"]
        n200 = 200 if len(cd) >= 200 else len(cd) - 1
        sma50, sma200 = float(cd.rolling(min(50, len(cd) - 1)).mean().iloc[-1]), float(cd.rolling(n200).mean().iloc[-1])
        cl = float(cd.iloc[-1])
        trend_d = 1 if (cl > sma200 and sma50 > sma200) else (-1 if (cl < sma200 and sma50 < sma200) else 0)
        rsi_d = float(rsi(cd, 14).iloc[-1])
        hi20d, lo20d = float(dfd["high"].iloc[-21:-1].max()), float(dfd["low"].iloc[-21:-1].min())
        breakout_d = 1 if cl > hi20d else (-1 if cl < lo20d else 0)
    c = df1h["close"]
    e20, e50 = ema(c, 20), ema(c, 50)
    e200 = ema(c, 200 if len(c) >= 200 else len(c) - 1)
    a = atr(df1h, 14)
    r = rsi(c, 14)
    ax = adx(df1h, 14)
    m_line, m_sig, m_hist = macd(c)
    pct_b, bb_w = bollinger(c)

    close = float(c.iloc[-1])
    atr_v = float(a.iloc[-1])
    atr_pct = atr_v / close * 100
    atr_pct_hist = (a / c * 100).iloc[-100:]
    atr_rank = float((atr_pct_hist < atr_pct).mean())

    hi20 = float(df1h["high"].iloc[-21:-1].max())
    lo20 = float(df1h["low"].iloc[-21:-1].min())
    breakout = 1 if close > hi20 else (-1 if close < lo20 else 0)

    vol_ratio = float(df1h["volume"].iloc[-1] / max(df1h["volume"].iloc[-21:-1].mean(), 1e-9))
    ret_24h = float((close / df1h["close"].iloc[-25] - 1) * 100) if len(df1h) > 25 else 0.0
    ret_4h = float((close / df1h["close"].iloc[-5] - 1) * 100)

    trend_1h = _trend(close, float(e50.iloc[-1]), float(e200.iloc[-1]))
    trend_4h = 0
    if df4h is not None and len(df4h) >= 60:
        c4 = df4h["close"]
        trend_4h = _trend(float(c4.iloc[-1]), float(ema(c4, 20).iloc[-1]), float(ema(c4, 50).iloc[-1]))
    timing_15m, pull_15m = 0, 0.0
    if df15 is not None and len(df15) >= 50:
        c15 = df15["close"]
        e21_15 = float(ema(c15, 21).iloc[-1])
        timing_15m = _trend(float(c15.iloc[-1]), float(ema(c15, 9).iloc[-1]), e21_15)
        # Насколько цена оторвалась от 15-минутной средней, в ATR(1ч): >0 выше средней, <0 ниже
        pull_15m = (close - e21_15) / atr_v if atr_v > 0 else 0.0

    # MACD: знак гистограммы и её направление
    mh, mh_prev = float(m_hist.iloc[-1]), float(m_hist.iloc[-4])
    macd_sign = 1 if mh > 0 else -1
    macd_rising = mh > mh_prev

    # Bollinger: сжатие (ширина в нижних 20% за 100 баров) и выход из сжатия
    bw_now = float(bb_w.iloc[-1])
    bw_rank = float((bb_w.iloc[-100:] < bw_now).mean())
    squeeze = bw_rank < 0.2
    squeeze_release = (not squeeze) and float((bb_w.iloc[-12:-2] < np.nanpercentile(bb_w.iloc[-100:], 20)).mean()) > 0.5
    pb = float(pct_b.iloc[-1]) if not np.isnan(pct_b.iloc[-1]) else 0.5

    # Дивергенция RSI (20 баров): цена выше, RSI ниже = медвежья; цена ниже, RSI выше = бычья
    div = 0
    seg_c, seg_r = c.iloc[-20:], r.iloc[-20:]
    i_hi, i_lo = int(seg_c.values.argmax()), int(seg_c.values.argmin())
    if i_hi >= 15 and float(seg_c.iloc[i_hi]) >= float(c.iloc[-40:-20].max()) and float(seg_r.iloc[i_hi]) < float(r.iloc[-40:-20].max()) - 3:
        div = -1
    if i_lo >= 15 and float(seg_c.iloc[i_lo]) <= float(c.iloc[-40:-20].min()) and float(seg_r.iloc[i_lo]) > float(r.iloc[-40:-20].min()) + 3:
        div = 1

    # Уровни: ближайшее сопротивление сверху и поддержка снизу (в ATR)
    res, sup = swing_levels(df1h)
    above = [x for x in res if x > close]
    below = [x for x in sup if x < close]
    res_dist = (min(above) - close) / atr_v if above and atr_v > 0 else 99.0
    sup_dist = (close - max(below)) / atr_v if below and atr_v > 0 else 99.0

    adx_v = float(ax.iloc[-1])
    if atr_rank >= 0.92:
        regime = "high_vol"
    elif adx_v >= 22 and trend_1h != 0:
        regime = "trend_up" if trend_1h > 0 else "trend_down"
    else:
        regime = "range"

    rsi_v = float(r.iloc[-1])
    score = 0.4 * trend_1h + 0.25 * trend_4h + 0.25 * breakout + 0.10 * max(-1.0, min(1.0, ret_24h / 5))
    score += 0.08 * macd_sign * (1.0 if macd_rising == (macd_sign > 0) else 0.5)
    if breakout != 0 and vol_ratio > 1.5:
        score += 0.1 * breakout
    if squeeze_release and breakout != 0:
        score += 0.1 * breakout
    # Дневной импульс: за тренд на дневках +0.15, импульсный режим (RSI_d>70 в тренде или пробой дневного максимума) +0.1;
    # лонг против дневного нисходящего тренда и шорт против дневного восходящего ослабляются
    score += 0.15 * trend_d
    if trend_d > 0 and (rsi_d > 70 or breakout_d > 0):
        score += 0.1
    if trend_d < 0 and breakout_d < 0:
        score -= 0.1
    if (rsi_v > 75 and score > 0) or (rsi_v < 25 and score < 0):
        score *= 0.5
    if div != 0 and (div > 0) != (score > 0):
        score *= 0.7
    # Нет места до уровня: лонг под сопротивлением / шорт над поддержкой ближе 1 ATR
    if score > 0 and res_dist < 1.0:
        score *= 0.7
    if score < 0 and sup_dist < 1.0:
        score *= 0.7
    if (trend_d < 0 and score > 0) or (trend_d > 0 and score < 0):
        score *= 0.75
    score = max(-1.0, min(1.0, score))

    return {
        "close": close, "atr": atr_v, "atr_pct": round(atr_pct, 3), "atr_rank": round(atr_rank, 2),
        "rsi": round(rsi_v, 1), "adx": round(adx_v, 1), "trend_1h": trend_1h, "trend_4h": trend_4h,
        "timing_15m": timing_15m, "pull_15m": round(pull_15m, 2), "breakout": breakout, "vol_ratio": round(vol_ratio, 2),
        "ret_24h": round(ret_24h, 2), "ret_4h": round(ret_4h, 2), "macd_hist": round(mh, 6),
        "macd_sign": macd_sign, "bb_pct_b": round(pb, 2), "bb_squeeze": bool(squeeze),
        "bb_release": bool(squeeze_release), "rsi_divergence": div, "res_dist_atr": round(res_dist, 2),
        "sup_dist_atr": round(sup_dist, 2), "trend_d": trend_d, "rsi_d": round(rsi_d, 1), "breakout_d": breakout_d,
        "ema20": float(e20.iloc[-1]), "ema50": float(e50.iloc[-1]),
        "ema200": float(e200.iloc[-1]), "regime": regime, "score": round(score, 3),
    }
