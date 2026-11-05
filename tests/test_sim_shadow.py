"""Симулятор и тени без сети: время в часах, фильтр индекса, дневной режим теней, потолок риска по направлению.
Запуск из корня проекта: python tests/test_sim_shadow.py"""
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, ".")
sys.path.insert(0, os.path.join(".", "research"))
sys.stdout.reconfigure(encoding="utf-8")
import moex_sim as ms  # noqa: E402
from bot import shadow  # noqa: E402
from bot.journal import Journal  # noqa: E402
from bot.risk import RiskManager  # noqa: E402


# ---------- 1. симулятор: «24 часа» — это сутки по часам, а не 24 свечи ----------
def frame(n_days, price_fn, bars_per_day=15):
    """Часовые свечи: 15 в торговом дне (10:00–24:00), выходные пропущены."""
    idx, px = [], []
    day = pd.Timestamp("2026-06-01 07:00")
    k = 0
    while len(idx) < n_days * bars_per_day:
        if day.dayofweek < 5:
            for h in range(bars_per_day):
                idx.append(day + pd.Timedelta(hours=h))
                px.append(price_fn(k))
                k += 1
        day += pd.Timedelta(days=1)
    c = np.array(px, dtype=float)
    f = pd.DataFrame({"open": c, "high": c + 0.05, "low": c - 0.05, "close": c, "atr": 1.0, "trend_4h": 1.0}, index=pd.DatetimeIndex(idx))
    f["th"] = f.index.values.astype("datetime64[s]").astype("int64") / 3600.0
    return f


# лонг от 100, стоп 2 ATR = 98; цена держится на 100.5, средняя 20 дней = 101 (выше цены): дневной выход должен
# сработать через сутки по часам (на 16-й свече), а не через 24 свечи
f = frame(10, lambda k: 100.0 if k == 0 else 100.5)
f["sma20d"] = 101.0
f.iloc[1, f.columns.get_loc("low")] = 99.4      # лимитка на откате 0.5 ATR исполняется на второй свече
out = ms.simulate(f, 0, 1, daily_exit=True, tps=(), pyramid=0.0)
bars_in_trade = out[2] - out[1]
print("дневной выход по времени: свечей в сделке", bars_in_trade)
assert 14 <= bars_in_trade <= 17, bars_in_trade
g = f.drop(columns=["th"])                       # без времени — старое поведение по свечам
out_old = ms.simulate(g, 0, 1, daily_exit=True, tps=(), pyramid=0.0)
assert out_old[2] - out_old[1] == 24, out_old

# ---------- 2. симулятор: фильтр индекса и множитель лонга ----------
n = 60
idx = pd.date_range("2026-06-01 07:00", periods=n, freq="h")
base = pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "atr": 1.0, "trend_4h": 1.0, "atr_pct": 1.0, "ret24": 0.0,
                     "regime": "trend", "trend_d": 0.0, "sma20d": 90.0}, index=idx)
base["th"] = base.index.values.astype("datetime64[s]").astype("int64") / 3600.0
base["score"] = 0.0
base.iloc[5, base.columns.get_loc("score")] = 1.0          # сильный лонг: 0.6 * 1.0 = 0.60


def n_trades(mkt_up, trend_d=0.0, **gates):
    f2 = base.copy()
    f2["mkt_up"], f2["trend_d"] = mkt_up, trend_d
    return len(ms.run({"X": f2}, dict({"thresh": 0.55}, **gates), tps=()))


assert n_trades(1.0) == 1 and n_trades(0.0) == 1, "без ворот индекс не влияет"
assert n_trades(0.0, index_gate=True) == 0, "индекс падает: лонг закрыт"
assert n_trades(1.0, -1.0, index_gate=True) == 0, "бумага в дневном падении: лонг закрыт"
assert n_trades(1.0, index_gate=True) == 1 and n_trades(np.nan, index_gate=True) == 1, "нет данных по индексу — не блокируем"
assert n_trades(1.0, long_mult=0.85) == 0, "0.60 * 0.85 = 0.51 < 0.55"
base["score"] = -base["score"]; base["trend_4h"] = -1.0
assert n_trades(1.0, 1.0, index_gate=True) == 0 and n_trades(1.0, 0.0, index_gate=True) == 1 and n_trades(0.0, 1.0, index_gate=True) == 1
print("фильтр индекса и множитель лонга: как в боте")

# ---------- 3. тени: дневной режим ----------
t0 = 1_800_000_000_000 - 1_800_000_000_000 % 3_600_000


def bars(prices):
    return pd.DataFrame({"ts": [t0 + i * 300_000 for i in range(len(prices))], "open": prices, "high": [p + 0.01 for p in prices],
                         "low": [p - 0.01 for p in prices], "close": prices})


risk_daily = {"stop_atr_mult": 2.0, "tp_levels": [], "breakeven_r": 0.7, "giveback_r": 1.2, "giveback_min_peak_r": 1.5, "stall_hours": 24,
              "trail_after_r": 1.0, "max_hours_in_trade": 96, "daily_trend_exit": True, "daily_peak_lock": {"min_peak_r": 3.0, "keep": 0.5}}
# лонг от 100, R = 2: рост до +2R (104), откат до +0.4R (100.8). По старым правилам — «защита прибыли»; в дневном режиме её нет
up = list(np.linspace(100, 104, 40)) + list(np.linspace(104, 100.8, 40)) + [100.8] * 30
old = shadow.simulate(bars(up), "long", 100.0, 1.0, dict(risk_daily, daily_trend_exit=False), "market")
new = shadow.simulate(bars(up), "long", 100.0, 1.0, risk_daily, "market", sma_at=lambda ts: 95.0)
print("тень: старые правила —", old["exit"], "| дневной режим —", new["exit"] or "ещё открыта")
assert old["done"] and old["exit"] in ("защита прибыли", "трейлинг") and not new["done"]
# средняя выше цены и прошли сутки: «дневной тренд сломан» на закрытии часа
long_flat = [100.5] * (12 * 25 + 5)
r = shadow.simulate(bars(long_flat), "long", 100.0, 1.0, risk_daily, "market", sma_at=lambda ts: 101.0)
assert r["exit"] == "дневной тренд сломан", r
# пик 3.5R: стоп держит половину пика
peak = list(np.linspace(100, 107, 50)) + list(np.linspace(107, 100, 60))
r = shadow.simulate(bars(peak), "long", 100.0, 1.0, risk_daily, "market", sma_at=lambda ts: 90.0)
assert r["done"] and 1.5 < r["r"] < 1.8, r
# комиссия рынка
a = shadow.simulate(bars(peak), "long", 100.0, 1.0, risk_daily, "market", sma_at=lambda ts: 90.0, fees=(0.00052, 0.00052, 0.0005))
assert a["r"] < r["r"]
print(f"тень в дневном режиме: выход по средней, защита половины пика ({r['r']:+.2f}R), комиссия Мосбиржи учитывается ({a['r']:+.2f}R)")

# ---------- 4. потолок риска по направлению с учётом входа одним лотом ----------
class S:
    def __init__(self, d): self.d = d
    def get(self, k, d=None):
        cur = self.d
        for part in k.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return d
            cur = cur[part]
        return cur


class Ex:
    def instruments(self): return {"A": {"mult": 1.0}, "B": {"mult": 1.0}}


rm = RiskManager(S({"risk": {"max_direction_risk_pct": 5.0}}), Ex(), Journal(":memory:"))
rm.tradable = lambda eq: 100000.0
held = [{"symbol": "A", "side": "Buy", "entry": 100.0, "stop_init": 95.4, "qty": 1000, "qty_init": 1000}]     # риск 4 600
assert abs(rm.direction_headroom(held, 100000, "long") - 400.0) < 1e-6 and abs(rm.direction_headroom(held, 100000, "short") - 5000.0) < 1e-6
print("остаток потолка риска по направлению: 400 при занятых 4 600 из 5 000 — один лот с риском 1 500 не пройдёт")
print("ВСЁ ОК")
