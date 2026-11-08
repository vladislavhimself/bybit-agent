"""Крипта (Bybit, бессрочные USDT) тем же честным методом, что Мосбиржа 07–08.11.2026 (research/price_strategies.py, breakout_check.py,
live_logic_check.py). Вопрос владельца: будет ли на крипте лучше, хуже или так же.
Данные: data/history/<SYM>USDT_60.csv (мс) с 2020 — дневные свечи агрегируются по UTC-дням. Издержки: 0.055% за сторону + 0.02% проскальзывание
= 0.15% на круг (тейкер Bybit), шорт без платы за заём (фандинг ≈ 0 в среднем). Рынок = BTCUSDT (вместо индекса Мосбиржи).
1. Ценовые стратегии: пробой 20 дн с объёмом (вниз → шорт, вверх → лонг), разворот −12% к BTC за 5 дн → лонг, возврат к среднему за день,
   недельный моментум (лидеры/аутсайдеры), краткосрочный разворот +8%/−8%. Портфель ≤8 позиций без плеча.
2. Техническая логика бота (research/moex_sim.py, крипто-параметры из config.yaml: порог 0.50, стоп 2.5 ATR, тейки 0.8/2.0, защита 0.6R после 0.8R,
   застой 12 ч, 48 ч, без дневного режима и пирамидинга; ворота по BTC выше 20-дн средней), обе стороны.
Запуск из корня: python research/crypto_strategies.py."""
import glob
import os
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research import moex_sim as ms  # noqa: E402
from research import price_strategies as ps  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
HIST = str(ROOT / "data" / "history")
COST = 0.15
SPLIT = pd.Timestamp("2024-01-01")
MAX_POS = 8


def load_daily():
    out = {}
    for p in sorted(glob.glob(HIST + "/*USDT_60.csv")):
        sym = os.path.basename(p)[:-7]
        df = pd.read_csv(p).drop_duplicates("ts").sort_values("ts")
        if len(df) < 24 * 400:
            continue
        ts = df["ts"] if df["ts"].max() > 1e11 else df["ts"] * 1000
        df["dt"] = pd.to_datetime(ts, unit="ms", utc=True).dt.tz_localize(None)
        d = df.set_index("dt").resample("1D").agg(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"), volume=("volume", "sum")).dropna()
        d.index.name = "date"
        out[sym] = d
    return out


def report(name, trades, hold, panel, cost=COST):
    if len(trades) < 10:
        print(f"  {name}: мало сделок ({len(trades)})")
        return None
    pn = np.array([p for _, p, _ in trades]) - cost
    dates = pd.to_datetime([d for d, _, _ in trades])
    cum = np.cumsum(pn)
    dd = float((np.maximum.accumulate(cum) - cum).max())
    bef, aft = pn[dates < SPLIT], pn[dates >= SPLIT]
    pool = panel["fwd"][hold]
    rng = np.random.default_rng(5)
    rs = np.array([pool[rng.integers(0, len(pool), len(pn))].sum() - cost * len(pn) for _ in range(1000)])
    p_rand = float((rs >= cum[-1]).mean())
    by_year = defaultdict(float)
    for d, p in zip(dates, pn):
        by_year[d.year] += p
    per_week = len(pn) / max(1, (dates.max() - dates.min()).days / 7)
    print(f"  {name}: n={len(pn)} (~{per_week:.1f}/нед) приб. {np.mean(pn > 0):.0%} средняя {pn.mean():+.2f}% медиана {np.median(pn):+.2f}% сумма {cum[-1]:+.0f}% просадка(сумма) {dd:.0f}% | "
          f"до 2024 {bef.sum():+.0f}% ({np.mean(bef > 0) if len(bef) else 0:.0%}) после {aft.sum():+.0f}% ({np.mean(aft > 0) if len(aft) else 0:.0%}) | случайные ≥ в {p_rand:.0%}")
    print("     по годам: " + ", ".join(f"{y}: {v:+.0f}%" for y, v in sorted(by_year.items())))
    return pn


def portfolio(trades_full, max_pos=MAX_POS, capital=100.0):
    """trades_full: list of dict(entry, exit, pnl) с издержками. Равные доли, не больше max_pos одновременно."""
    tr = sorted(trades_full, key=lambda t: t["entry"])
    open_pos, eq, curve, taken = [], capital, [], 0
    for t in tr:
        open_pos = [e for e in open_pos if e >= t["entry"]]
        if len(open_pos) >= max_pos:
            continue
        eq += eq / max_pos * t["pnl"] / 100
        open_pos.append(t["exit"])
        taken += 1
        curve.append((t["exit"], eq))
    if not curve:
        return
    s = pd.Series([e for _, e in curve], index=pd.to_datetime([d for d, _ in curve])).sort_index()
    yearly = s.groupby(s.index.year).last()
    prev, yr = capital, []
    for y, v in yearly.items():
        yr.append(f"{y}: {(v / prev - 1) * 100:+.0f}%")
        prev = v
    dd = float(((s.cummax() - s) / s.cummax()).max() * 100)
    years = max(0.5, (s.index[-1] - s.index[0]).days / 365.25)
    print(f"     ПОРТФЕЛЬ (≤{max_pos} позиций, без плеча): сделок {taken}/{len(tr)}, {capital:.0f} → {s.iloc[-1]:.0f}, среднегодовая {((s.iloc[-1] / capital) ** (1 / years) - 1) * 100:+.1f}%, "
          f"макс. просадка {dd:.1f}% | " + ", ".join(yr))


def trades_with_exit(panel, sigs, side, hold, cost):
    close, opn, dates = panel["close"], panel["open"], panel["close"].index
    out = []
    for i, sym in sigs:
        if i + hold >= len(dates):
            continue
        o, c = opn[sym].iloc[i + 1], close[sym].iloc[i + hold]
        if np.isnan(o) or np.isnan(c):
            continue
        out.append({"entry": dates[i + 1], "exit": dates[i + hold], "sym": sym, "pnl": side * (c / o - 1) * 100 - cost})
    return out


def breakout_signals(panel, side, lookback=20, vol_mult=1.5):
    close, high, vol = panel["close"], panel["high"], panel["vol"]
    out = []
    for sym in close.columns:
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


def main():
    daily = load_daily()
    if "BTCUSDT" not in daily:
        print("нет BTCUSDT")
        return
    btc = daily["BTCUSDT"][["open", "close", "high", "low"]]
    alts = {k: v for k, v in daily.items()}
    panel = ps.build_panel(alts, btc)
    print(f"пар: {len(alts)}, дней: {len(panel['close'])}, {panel['close'].index[0].date()} — {panel['close'].index[-1].date()}, издержки {COST}% на круг, рынок = BTC")

    print("\n1. ПРОБОЙ 20 ДНЕЙ С ОБЪЁМОМ ≥1.5×")
    both = []
    for side, lab in ((-1, "вниз → шорт"), (1, "вверх → лонг")):
        sigs = breakout_signals(panel, side)
        for hold in (3, 5, 10):
            tr = trades_with_exit(panel, sigs, side, hold, COST)
            report(f"{lab}, {hold} д", [(t["entry"], t["pnl"] + COST, t["sym"]) for t in tr], hold, panel)
            if hold == 5:
                portfolio(tr)
                both += tr
    print("  обе стороны вместе, 5 д:")
    portfolio(both, 8)
    portfolio(both, 15)

    print("\n2. РАЗВОРОТ: пара отстала от BTC на 12%+ за 5 дней → лонг; обогнала на 12%+ → шорт")
    rel5 = panel["rel"].rolling(5).sum()
    for thr, side, lab in ((-12, 1, "−12% → лонг"), (12, -1, "+12% → шорт"), (-20, 1, "−20% → лонг")):
        sigs = []
        for sym in rel5.columns:
            r = rel5[sym].to_numpy()
            last = -99
            for i in np.where((r <= thr) if thr < 0 else (r >= thr))[0]:
                if i - last < 5:
                    continue
                sigs.append((i, sym))
                last = i
        for hold in (3, 5):
            tr = trades_with_exit(panel, sigs, side, hold, COST)
            report(f"{lab}, {hold} д", [(t["entry"], t["pnl"] + COST, t["sym"]) for t in tr], hold, panel)
            if hold == 5:
                portfolio(tr)

    print("\n3. ВОЗВРАТ К СРЕДНЕМУ ЗА ДЕНЬ (к BTC): −5% → лонг 1 д; +5% → шорт 1 д")
    rel = panel["rel"]
    for thr, side, lab in ((-5, 1, "−5% → лонг"), (5, -1, "+5% → шорт")):
        sigs = [(i, sym) for sym in rel.columns for i in np.where((rel[sym].to_numpy() <= thr) if thr < 0 else (rel[sym].to_numpy() >= thr))[0]]
        tr = trades_with_exit(panel, sigs, side, 1, COST)
        report(lab, [(t["entry"], t["pnl"] + COST, t["sym"]) for t in tr], 1, panel)

    print("\n4. НЕДЕЛЬНЫЙ МОМЕНТУМ: каждый понедельник 5 лучших за 20 дней → лонг 5 д; 5 худших → шорт 5 д")
    ps.SPLIT = SPLIT
    mom = panel["close"].pct_change(20) * 100
    dates = panel["close"].index
    for lab, pick, side in (("лидеры → лонг", lambda row: row.nlargest(5).index, 1), ("аутсайдеры → шорт", lambda row: row.nsmallest(5).index, -1)):
        tr = []
        for i in range(21, len(dates) - 6):
            if dates[i].weekday() != 0:
                continue
            row = mom.iloc[i - 1].dropna()
            if len(row) < 10:
                continue
            for sym in pick(row):
                x = ps.fwd_ret(panel, sym, i - 1, 5, side)
                if x is not None:
                    tr.append((dates[i], x, sym))
        report(lab, tr, 5, panel)

    print("\n5. ТЕХНИЧЕСКАЯ ЛОГИКА БОТА (крипто-параметры), часовые свечи, ворота по BTC")
    syms = [s for s in alts if s != "BTCUSDT"] + ["BTCUSDT"]
    feats = ms.load(HIST, syms, days=2500, index="BTCUSDT")
    gates = {"thresh": 0.50, "ext_long": 2.0, "ext_short": 2.0, "range_long": False, "range_short": False, "high_vol": False, "counter_4h": False,
             "busy": 3, "max_per_day": 0, "index_gate": True, "long_mult": 1.0}
    exits = dict(stop_mult=2.5, tps=((0.8, 0.40), (2.0, 0.35)), be_r=0.7, tiers=(), stall_h=12, trail_after=1.0, max_h=48, gb_r=0.6, gb_min=0.8,
                 pyramid=0.0, daily_exit=False)
    ms.FEE, ms.SLIP = 0.00055, 0.0002
    t = ms.run(feats, gates=gates, **dict(ms.DEFAULT_EXITS, **exits))
    from research.live_logic_check import report as rreport
    import research.live_logic_check as llc
    llc.RISK_PCT = 0.8
    rreport("крипта, как в боте (обе стороны)", t)
    rreport("крипта, только лонги", t[t.sg > 0])
    rreport("крипта, только шорты", t[t.sg < 0])
    t2 = ms.run(feats, gates=dict(gates, index_gate=False), **dict(ms.DEFAULT_EXITS, **exits))
    rreport("крипта без ворот по BTC", t2)
    # случайные входы с теми же выходами
    rng = np.random.default_rng(3)
    base = llc.random_baseline(feats, min(2000, len(t)), rng, **dict(ms.DEFAULT_EXITS, **exits))
    print(f"  случайные входы с теми же выходами: n={len(base)} средний {base.mean():+.3f}R, приб. {np.mean(base > 0):.0%}")


if __name__ == "__main__":
    main()
