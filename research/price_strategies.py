"""Ценовые стратегии как сделки на истории: откуда взять частоту (пользователь: минимум 4 сделки в неделю с перевесом).

Данные: дневные свечи data/history/<SYM>_D.csv (~85 бумаг Мосбиржи с 2019–2020), часовые для внутридневных; индекс — из IMOEX_60.
Издержки: 0.1% на круг по акциям (0.2% для пары — две ноги). Без плеча, без реинвестирования: результат = сумма % по сделкам.
Каждая стратегия: n, доля прибыльных, средняя, сумма, просадка, до/после 2024, по годам, и сравнение со случайными входами
той же длины и в том же количестве (доля случаев, когда случайный набор дал сумму не хуже нашей).
Стратегии: 1 возврат к среднему против индекса; 2 пары; 3 пробой 20 дней с объёмом; 4 ночь/понедельник/конец месяца;
5 недельный моментум (лидеры/аутсайдеры месяца); 6 краткосрочный разворот (−8% за 5 дней); 7 первый час (эйфория/паника) внутри дня.
Запуск из корня: python research/price_strategies.py. Модель не вызывается."""
import glob
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
MSK = ZoneInfo("Europe/Moscow")
COST = 0.1
SPLIT = pd.Timestamp("2024-01-01")
EXCLUDE = {"IMOEX", "IMOEXF", "RGBI", "RTSI", "GAZPF", "SBERF", "BTC", "GOLD", "DXY", "NASDAQ", "SPX", "VIX"}
RNG = np.random.default_rng(11)


def load_daily():
    out = {}
    for p in sorted(glob.glob(str(ROOT / "data" / "history" / "*_D.csv"))):
        s = os.path.basename(p)[:-6]
        if s in EXCLUDE or "USDT" in s:
            continue
        df = pd.read_csv(p).drop_duplicates("ts").sort_values("ts")
        if len(df) < 300:
            continue
        df["date"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert(MSK).dt.normalize().dt.tz_localize(None)
        df = df.drop_duplicates("date").set_index("date")[["open", "high", "low", "close", "volume"]]
        out[s] = df
    return out


def load_hourly(sym):
    df = pd.read_csv(ROOT / "data" / "history" / f"{sym}_60.csv").drop_duplicates("ts").sort_values("ts")
    df["dt"] = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert(MSK).dt.tz_localize(None)
    return df


def index_daily():
    h = load_hourly("IMOEX")
    h["date"] = h["dt"].dt.normalize()
    g = h.groupby("date").agg(open=("open", "first"), close=("close", "last"), high=("high", "max"), low=("low", "min"))
    return g


def report(name, trades, hold_days, panel, cost=COST):
    """trades: list of (date_entry, pnl% до издержек, sym). Случайный фон: те же hold_days по случайным (бумага, день)."""
    if len(trades) < 10:
        print(f"  {name}: мало сделок ({len(trades)})")
        return None
    pn = np.array([p for _, p, _ in trades]) - cost
    dates = pd.to_datetime([d for d, _, _ in trades])
    cum = np.cumsum(pn)
    dd = float((np.maximum.accumulate(cum) - cum).max())
    t = pn.mean() / (pn.std(ddof=1) / np.sqrt(len(pn))) if pn.std() > 0 else 0
    bef, aft = pn[dates < SPLIT], pn[dates >= SPLIT]
    # случайный фон: средняя доходность лонга за hold_days по всем бумагам и дням (знак нашей сделки неизвестен => сравниваем |сумма| с распределением случайных сумм лонгов)
    pool = panel["fwd"][hold_days]
    rs = np.array([pool[RNG.integers(0, len(pool), len(pn))].sum() - cost * len(pn) for _ in range(1000)])
    p_rand = float((rs >= cum[-1]).mean())
    by_year = defaultdict(float)
    for d, p in zip(dates, pn):
        by_year[d.year] += p
    per_week = len(pn) / max(1, (dates.max() - dates.min()).days / 7)
    print(f"  {name}")
    print(f"    сделок {len(pn)} (~{per_week:.1f}/нед), приб. {np.mean(pn > 0):.0%}, средняя {pn.mean():+.2f}%, сумма {cum[-1]:+.1f}%, t={t:.1f}, просадка {dd:.1f}%")
    print(f"    до 2024: {bef.sum():+.1f}% ({len(bef)}, приб. {np.mean(bef > 0) if len(bef) else 0:.0%}) | после 2024: {aft.sum():+.1f}% ({len(aft)}, приб. {np.mean(aft > 0) if len(aft) else 0:.0%}) | "
          f"случайные входы ≥ нашей суммы в {p_rand:.0%}")
    print("    по годам: " + ", ".join(f"{y}: {v:+.0f}%" for y, v in sorted(by_year.items())))
    return {"n": len(pn), "win": float(np.mean(pn > 0)), "mean": float(pn.mean()), "sum": float(cum[-1]), "dd": dd, "before": float(bef.sum()), "after": float(aft.sum()), "p_rand": p_rand}


def build_panel(daily, idx):
    """Общие таблицы: закрытия, открытия, доходности, относительная к индексу доходность, форвардные доходности для фона."""
    close = pd.DataFrame({s: d["close"] for s, d in daily.items()}).sort_index()
    opn = pd.DataFrame({s: d["open"] for s, d in daily.items()}).reindex(close.index)
    high = pd.DataFrame({s: d["high"] for s, d in daily.items()}).reindex(close.index)
    vol = pd.DataFrame({s: d["volume"] for s, d in daily.items()}).reindex(close.index)
    ret = close.pct_change() * 100
    ic = idx["close"].reindex(close.index).ffill()
    iret = ic.pct_change() * 100
    rel = ret.sub(iret, axis=0)
    fwd = {}
    for h in (1, 3, 5, 10, 15):
        # доходность лонга: вход по открытию следующего дня, выход по закрытию через h дней
        f = (close.shift(-h) / opn.shift(-1) - 1) * 100
        fwd[h] = f.to_numpy().ravel()
        fwd[h] = fwd[h][~np.isnan(fwd[h])]
    return {"close": close, "open": opn, "high": high, "vol": vol, "ret": ret, "rel": rel, "iret": iret, "fwd": fwd}


def fwd_ret(panel, sym, i, h, side=1):
    """Вход по открытию дня i+1, выход по закрытию дня i+h (в %), side=±1."""
    c, o = panel["close"][sym], panel["open"][sym]
    if i + h >= len(c) or np.isnan(o.iloc[i + 1]) or np.isnan(c.iloc[i + h]):
        return None
    return side * (c.iloc[i + h] / o.iloc[i + 1] - 1) * 100


def s1_mean_reversion(panel):
    print("\n1. ВОЗВРАТ К СРЕДНЕМУ ПРОТИВ ИНДЕКСА: бумага за день отстала/обогнала индекс на 3%+ → обратная сделка со следующего открытия")
    rel, dates = panel["rel"], panel["rel"].index
    res = {}
    for thr, side, lab in ((-3, 1, "упала на 3%+ к индексу → лонг"), (3, -1, "выросла на 3%+ к индексу → шорт"), (-5, 1, "упала на 5%+ → лонг"), (5, -1, "выросла на 5%+ → шорт")):
        for h in (1, 3):
            trades = []
            for sym in rel.columns:
                r = rel[sym].to_numpy()
                idxs = np.where((r <= thr) if thr < 0 else (r >= thr))[0]
                for i in idxs:
                    x = fwd_ret(panel, sym, i, h, side)
                    if x is not None:
                        trades.append((dates[i], x, sym))
            res[f"{lab}|{h}д"] = report(f"{lab}, удержание {h} д", trades, h, panel)
    return res


PAIRS = [("SBER", "SBERP"), ("TATN", "TATNP"), ("SNGS", "SNGSP"), ("MTLR", "MTLRP"), ("LKOH", "ROSN"), ("NLMK", "CHMF"), ("MAGN", "NLMK"), ("GAZP", "NVTK"),
         ("SBER", "VTBR"), ("GMKN", "RUAL"), ("MTSS", "RTKM"), ("PIKK", "LSRG"), ("X5", "MGNT"), ("ROSN", "SIBN"), ("TATN", "LKOH"), ("CHMF", "MAGN")]


def s2_pairs(panel):
    print("\n2. ПАРЫ: z-оценка лог-отношения за 60 дней; вход при |z| ≥ 2 со следующего открытия, выход при |z| ≤ 0.5 или через 15 дней (две ноги, издержки 0.2%)")
    close, opn = panel["close"], panel["open"]
    trades = []
    for a, b in PAIRS:
        if a not in close or b not in close:
            continue
        lr = np.log(close[a] / close[b])
        z = (lr - lr.rolling(60).mean()) / lr.rolling(60).std()
        i = 60
        n = len(z)
        while i < n - 2:
            zi = z.iloc[i]
            if np.isnan(zi) or abs(zi) < 2:
                i += 1
                continue
            side = -1 if zi > 0 else 1  # z>0: A дорог к B → шорт A, лонг B
            j = i + 1
            while j < min(n - 1, i + 16) and abs(z.iloc[j]) > 0.5:
                j += 1
            if j >= n:
                break
            oa, ob = opn[a].iloc[i + 1], opn[b].iloc[i + 1]
            ca, cb = close[a].iloc[j], close[b].iloc[j]
            if any(np.isnan(v) for v in (oa, ob, ca, cb)):
                i = j + 1
                continue
            pnl = side * ((ca / oa - 1) - (cb / ob - 1)) * 100
            trades.append((close.index[i], pnl, f"{a}/{b}"))
            i = j + 1
    return report("все пары", trades, 5, panel, cost=0.2)


def s3_breakout(panel):
    print("\n3. ПРОБОЙ: закрытие выше максимума 20 дней при объёме ≥1.5× среднего → лонг со следующего открытия, 5 дней; зеркально вниз → шорт")
    close, high, vol, dates = panel["close"], panel["high"], panel["vol"], panel["close"].index
    res = {}
    for side, lab in ((1, "пробой вверх → лонг"), (-1, "пробой вниз → шорт")):
        trades = []
        for sym in close.columns:
            c, hi, v = close[sym], high[sym], vol[sym]
            ref = hi.rolling(20).max().shift(1) if side == 1 else panel["close"][sym].rolling(20).min().shift(1)
            va = v.rolling(20).mean().shift(1)
            cond = ((c > ref) if side == 1 else (c < ref)) & (v >= 1.5 * va)
            last = -99
            for i in np.where(cond.to_numpy())[0]:
                if i - last < 5:
                    continue
                x = fwd_ret(panel, sym, i, 5, side)
                if x is not None:
                    trades.append((dates[i], x, sym))
                    last = i
        res[lab] = report(lab, trades, 5, panel)
    return res


def s4_calendar(panel, daily):
    print("\n4. КАЛЕНДАРЬ: ночь (закрытие → открытие), понедельник, конец месяца — по всем бумагам, средняя доходность за «сделку»")
    close, opn, dates = panel["close"], panel["open"], panel["close"].index
    res = {}
    # ночь: купить по закрытию, продать по открытию следующего дня
    on = (opn.shift(-1) / close - 1) * 100
    day = (close / opn - 1) * 100
    print(f"  ночь (закрытие→открытие): средняя {np.nanmean(on.to_numpy()):+.3f}% за ночь, день (открытие→закрытие): {np.nanmean(day.to_numpy()):+.3f}% | "
          f"до 2024 ночь {np.nanmean(on[on.index < SPLIT].to_numpy()):+.3f}% день {np.nanmean(day[day.index < SPLIT].to_numpy()):+.3f}% | "
          f"после 2024 ночь {np.nanmean(on[on.index >= SPLIT].to_numpy()):+.3f}% день {np.nanmean(day[day.index >= SPLIT].to_numpy()):+.3f}%  (издержки 0.1% на круг)")
    # понедельник / пятница: доходность индекса по дням недели
    iret = panel["iret"]
    print("  индекс по дням недели (средний % за день): " + ", ".join(f"{['пн', 'вт', 'ср', 'чт', 'пт'][k]} {iret[iret.index.weekday == k].mean():+.3f}" for k in range(5)))
    # конец месяца: индекс последние 2 дня месяца и первые 2 дня
    g = iret.groupby([iret.index.year, iret.index.month])
    last2, first2, mid = [], [], []
    for _, s in g:
        if len(s) < 10:
            continue
        last2.append(s.iloc[-2:].sum())
        first2.append(s.iloc[:2].sum())
        mid.append(s.iloc[2:-2].mean() * 2)
    print(f"  конец месяца (последние 2 дня): {np.mean(last2):+.2f}% (вверх {np.mean(np.array(last2) > 0):.0%}, n={len(last2)}), первые 2 дня: {np.mean(first2):+.2f}% (вверх {np.mean(np.array(first2) > 0):.0%}), "
          f"обычные 2 дня: {np.mean(mid):+.2f}%")
    res["overnight_mean"] = float(np.nanmean(on.to_numpy()))
    return res


def s5_weekly_momentum(panel):
    print("\n5. НЕДЕЛЬНЫЙ МОМЕНТУМ: каждый понедельник 5 лучших бумаг за 20 дней → лонг на 5 дней; 5 худших → шорт; лучшие минус худшие")
    close, opn, dates = panel["close"], panel["open"], panel["close"].index
    mom = close.pct_change(20) * 100
    res = {}
    for lab, pick, side in (("лидеры → лонг", lambda row: row.nlargest(5).index, 1), ("аутсайдеры → шорт", lambda row: row.nsmallest(5).index, -1),
                            ("аутсайдеры → лонг (разворот)", lambda row: row.nsmallest(5).index, 1)):
        trades = []
        for i in range(21, len(dates) - 6):
            if dates[i].weekday() != 0:
                continue
            row = mom.iloc[i - 1].dropna()
            if len(row) < 20:
                continue
            for sym in pick(row):
                x = fwd_ret(panel, sym, i - 1, 5, side)
                if x is not None:
                    trades.append((dates[i], x, sym))
        res[lab] = report(lab, trades, 5, panel)
    return res


def s6_reversal(panel):
    print("\n6. КРАТКОСРОЧНЫЙ РАЗВОРОТ: бумага упала на 8%+ за 5 дней (к индексу) → лонг 5 дней; выросла на 8%+ → шорт")
    rel5 = panel["rel"].rolling(5).sum()
    dates = rel5.index
    res = {}
    for thr, side, lab in ((-8, 1, "−8% за 5 дней → лонг"), (8, -1, "+8% за 5 дней → шорт"), (-12, 1, "−12% за 5 дней → лонг")):
        trades = []
        for sym in rel5.columns:
            r = rel5[sym].to_numpy()
            last = -99
            for i in np.where((r <= thr) if thr < 0 else (r >= thr))[0]:
                if i - last < 5:
                    continue
                x = fwd_ret(panel, sym, i, 5, side)
                if x is not None:
                    trades.append((dates[i], x, sym))
                    last = i
        res[lab] = report(lab, trades, 5, panel)
    return res


def s7_first_hour(daily_syms):
    print("\n7. ПЕРВЫЙ ЧАС (часовые свечи): бумага за первый час +2% → шорт до закрытия дня; −2% → лонг до закрытия (издержки 0.1%)")
    res = {}
    for thr, side, lab in ((2, -1, "эйфория +2% → шорт до закрытия"), (-2, 1, "паника −2% → лонг до закрытия"), (3, -1, "эйфория +3% → шорт"), (-3, 1, "паника −3% → лонг")):
        trades = []
        for sym in daily_syms:
            p = ROOT / "data" / "history" / f"{sym}_60.csv"
            if not p.exists():
                continue
            h = load_hourly(sym)
            h["date"] = h["dt"].dt.normalize()
            h["hour"] = h["dt"].dt.hour
            for d, g in h.groupby("date"):
                g = g[(g["hour"] >= 10) & (g["hour"] <= 18)]
                if len(g) < 5:
                    continue
                first = g.iloc[0]
                r1 = (first["close"] / first["open"] - 1) * 100
                if (thr > 0 and r1 >= thr) or (thr < 0 and r1 <= thr):
                    entry = g.iloc[1]["open"]
                    exit_ = g.iloc[-1]["close"]
                    trades.append((d, side * (exit_ / entry - 1) * 100, sym))
        pn = np.array([p for _, p, _ in trades]) - COST
        if len(pn) < 10:
            print(f"  {lab}: мало ({len(pn)})")
            continue
        dates = pd.to_datetime([d for d, _, _ in trades])
        bef, aft = pn[dates < SPLIT], pn[dates >= SPLIT]
        cum = np.cumsum(pn)
        dd = float((np.maximum.accumulate(cum) - cum).max())
        print(f"  {lab}: сделок {len(pn)} (~{len(pn) / max(1, (dates.max() - dates.min()).days / 7):.1f}/нед), приб. {np.mean(pn > 0):.0%}, средняя {pn.mean():+.2f}%, сумма {cum[-1]:+.0f}%, просадка {dd:.0f}% | "
              f"до 2024 {bef.sum():+.0f}% ({len(bef)}, {np.mean(bef > 0) if len(bef) else 0:.0%}) | после 2024 {aft.sum():+.0f}% ({len(aft)}, {np.mean(aft > 0) if len(aft) else 0:.0%})")
        res[lab] = {"n": len(pn), "win": float(np.mean(pn > 0)), "sum": float(cum[-1]), "before": float(bef.sum()), "after": float(aft.sum())}
    return res


def main():
    daily = load_daily()
    idx = index_daily()
    panel = build_panel(daily, idx)
    print(f"бумаг: {len(daily)}, дней: {len(panel['close'])}, {panel['close'].index[0].date()} — {panel['close'].index[-1].date()}, издержки {COST}% на круг")
    out = {}
    out["mean_reversion"] = s1_mean_reversion(panel)
    out["pairs"] = s2_pairs(panel)
    out["breakout"] = s3_breakout(panel)
    out["calendar"] = s4_calendar(panel, daily)
    out["weekly_momentum"] = s5_weekly_momentum(panel)
    out["reversal"] = s6_reversal(panel)
    liquid = [s for s in ("SBER", "GAZP", "LKOH", "ROSN", "NVTK", "GMKN", "PLZL", "YDEX", "TATN", "SNGS", "MGNT", "VTBR", "T", "ALRS", "CHMF", "NLMK", "MAGN", "RUAL", "AFLT", "MTSS",
                          "PHOR", "MOEX", "SMLT", "PIKK", "SVCB", "POSI", "OZON", "VKCO", "SIBN", "IRAO", "HYDR", "MTLR", "TRNFP", "FLOT", "FEES", "AFKS", "X5", "SBERP", "TATNP", "SNGSP") if s in daily]
    out["first_hour"] = s7_first_hour(liquid)
    import json
    json.dump(out, open(ROOT / "data" / "price_strategies.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    print("\nсохранено: data/price_strategies.json")


if __name__ == "__main__":
    main()
