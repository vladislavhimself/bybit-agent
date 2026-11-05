"""Офлайн-проверка на сценарии Газпрома 04–05.11: общий безубыток после добора, защита половины пика, тейк по целому лоту,
перевыставление сгоревшего тейка, подтяжка стопа по вердикту наблюдателя."""
import sys
import time

import pandas as pd

sys.path.insert(0, ".")
from bot.executor import Executor  # noqa: E402
from bot.journal import Journal  # noqa: E402


class S:
    def __init__(self, d): self.d = d
    def get(self, k, d=None):
        cur = self.d
        for part in k.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return d
            cur = cur[part]
        return cur


RISK = {"daily_trend_exit": True, "max_hours_daily_exit": 720, "pyramid_part": 0.5, "pyramid_r": 1.5, "soft_stops": True,
        "max_hours_in_trade": 96, "breakeven_r": 0.7, "giveback_r": 1.2, "giveback_min_peak_r": 1.5, "stall_hours": 24, "trail_after_r": 1.0,
        "tp_levels": [{"r": 3.0, "part": 0.33}], "pyramid_stop_breakeven": True, "daily_peak_lock": {"min_peak_r": 3.0, "keep": 0.5},
        "driver_check": {"mode": "tighten", "min_peak_r": 2.0, "every_min": 30, "min_confidence": 0.6, "keep": 0.5}}


class Ex:
    def __init__(self):
        self.mark = 111.45; self.qty = 3.0; self.entry = 111.45; self.orders = []; self.stops = []; self.limits = []; self.active = []
    def instruments(self): return {"GAZPF": {"base": "GAZP", "mult": 100.0, "min_qty": 1, "tick": 0.01, "lot": 1}}
    def positions(self): return [{"symbol": "GAZPF", "side": "Sell", "qty": self.qty, "entry": self.entry, "mark": self.mark, "upnl": 0, "mult": 100.0, "leverage": 0.3, "sl": 0, "tp": 0, "liq": 0}]
    def klines(self, sym, iv, n):
        now_ms = int(time.time() * 1000)
        if iv == "240":
            return pd.DataFrame({"ts": range(80), "close": [130 - i * 0.25 for i in range(80)]})   # тренд 4ч вниз — шорту по пути
        if iv == "D":
            return pd.DataFrame({"ts": [now_ms - (30 - i) * 86400000 for i in range(30)], "close": [113.0] * 30})
        return pd.DataFrame({"ts": [now_ms - 7200000, now_ms - 3600000, now_ms - 600000], "close": [self.mark] * 3})
    def fmt_qty(self, sym, q): return str(int(q))
    def place_market(self, sym, side, qty, **k):
        self.orders.append((side, qty)); self.entry = (self.entry * self.qty + self.mark * qty) / (self.qty + qty); self.qty += qty
    def set_stop_loss(self, sym, p, side_closing=None): self.stops.append(round(p, 2))
    def open_orders(self, sym): return [{"orderId": x} for x in self.active]
    def place_limit(self, sym, side, qty, price, **k):
        oid = f"tp{len(self.limits) + 1}"; self.limits.append((side, qty, round(price, 2))); self.active.append(oid); return oid
    def price_limits(self, sym): return (100.0, 120.0)
    def cancel_all(self, sym): pass
    def close_position(self, sym): self.qty = 0


class T:
    def __init__(self): self.msgs = []
    def send(self, text, html=False): self.msgs.append(text)


class Verdict:
    def __init__(self, v, c): self.verdict, self.confidence, self.reasoning = v, c, "тест"


j = Journal(":memory:"); ex = Ex(); tg = T(); e = Executor(S({"risk": RISK}), ex, None, j, tg)
e.risk = type("R", (), {"tradable": lambda s, x: 96000.0})()
# план тейков для 3 контрактов: треть = 1 контракт (раньше округлялось в ноль)
plan = e._tp_plan("GAZPF", 3.0, 111.45, 1.131, long=False)
print("план тейков на 3 контракта:", plan); assert len(plan) == 1 and plan[0]["qty"] == 1.0 and abs(plan[0]["price"] - (111.45 - 3 * 1.131)) < 1e-6
now = int(time.time())
tid = j.open_trade({"symbol": "GAZPF", "side": "Sell", "qty": 3.0, "entry": 111.45, "stop": 112.581, "tp": 0, "stop_init": 112.581, "tier": "low",
                    "score": -0.6, "confidence": 0.7, "reasons": [], "regime": "trend_down", "atr": 0.566, "leverage": 1, "opened_ts": now,
                    "status": "open", "order_id": "", "news_driven": 0, "qty_init": 3.0, "tp_plan": [], "tp_hits": 0, "event_key": "", "mult": 100, "fees": 0})
j._run("UPDATE trades SET entry_init=111.45, opened_ts=?, stop=111.283 WHERE id=?", (now - 12 * 3600, tid))
j.update_tp_plan(tid, [dict(plan[0], order_id="old-expired")])

# 1) цена 109.66 (+1.58R): добор 1 контракта и стоп на общий безубыток
ex.mark = 109.66; e.manage(); e.manage()
t = j.open_trades()[0]
print("после добора: объём", t["qty"], "средний вход", round(t["entry"], 3), "стоп", round(t["stop"], 3), "| тейки у брокера:", ex.limits)
assert ex.orders == [("Sell", 1.0)] and t["qty"] == 4.0
assert abs(t["stop"] - 111.0025 * (1 - 0.0015)) < 0.01, t["stop"]          # общий безубыток чуть ниже среднего входа 111.00
assert ex.limits and ex.limits[0][1] == 1.0, "сгоревший тейк должен быть перевыставлен"

# 2) цена 107.55 (пик +3.45R): стоп держит половину пика = +1.72R = 109.50
ex.mark = 107.55; e.manage(); e.manage()
t = j.open_trades()[0]
print("после пика 3.45R: стоп", round(t["stop"], 2), "| mfe", round(t["mfe_r"], 2))
assert abs(t["stop"] - (111.45 - 0.5 * t["mfe_r"] * 1.131)) < 0.02 and t["stop"] < 109.6

# 3) наблюдатель: «подтянуть» с уверенностью 0.7 при +2.3R → стоп на половину текущей прибыли (если лучше)
j2 = Journal(":memory:"); ex2 = Ex(); e2 = Executor(S({"risk": dict(RISK, pyramid_part=0, daily_peak_lock={})}), ex2, None, j2, T()); e2.risk = e.risk
tid2 = j2.open_trade({"symbol": "GAZPF", "side": "Sell", "qty": 3.0, "entry": 111.45, "stop": 111.283, "tp": 0, "stop_init": 112.581, "tier": "low",
                      "score": -0.6, "confidence": 0.7, "reasons": [], "regime": "trend_down", "atr": 0.566, "leverage": 1, "opened_ts": now,
                      "status": "open", "order_id": "", "news_driven": 0, "qty_init": 3.0, "tp_plan": [], "tp_hits": 0, "event_key": "", "mult": 100, "fees": 0})
j2._run("UPDATE trades SET entry_init=111.45, opened_ts=?, stop=111.283 WHERE id=?", (now - 12 * 3600, tid2))
e2.driver = type("D", (), {"maybe_check": lambda self, *a, **k: Verdict("tighten", 0.7)})()
ex2.mark = 108.85; e2.manage()
t2 = j2.open_trades()[0]
print("наблюдатель «подтянуть» при +2.30R: стоп", round(t2["stop"], 2))
assert abs(t2["stop"] - (111.45 - 0.5 * 2.2988 * 1.131)) < 0.03
print("ВСЁ ОК")
