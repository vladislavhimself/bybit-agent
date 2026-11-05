"""Офлайн-проверка: вердикт наблюдателя «забрать» с уверенностью от 0.8 закрывает половину позиции один раз,
стоп-заявка перевыставляется на новый объём, тейк после этого считается правильно; один лот не делится; лимиты 8/3/6."""
import sys
import time

import pandas as pd

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")
from bot.executor import Executor  # noqa: E402
from bot.journal import Journal  # noqa: E402
from bot.risk import RiskManager  # noqa: E402


class S:
    def __init__(self, d): self.d = d
    def get(self, k, d=None):
        cur = self.d
        for part in k.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return d
            cur = cur[part]
        return cur


RISK = {"daily_trend_exit": True, "max_hours_daily_exit": 720, "pyramid_part": 0, "pyramid_r": 1.5, "soft_stops": True,
        "max_hours_in_trade": 96, "breakeven_r": 0.7, "giveback_r": 1.2, "giveback_min_peak_r": 1.5, "stall_hours": 24, "trail_after_r": 1.0,
        "tp_levels": [{"r": 3.0, "part": 0.33}], "pyramid_stop_breakeven": True, "daily_peak_lock": {},
        "driver_check": {"mode": "tighten", "min_peak_r": 2.0, "every_min": 30, "min_confidence": 0.6, "keep": 0.5,
                         "take_part": 0.5, "take_min_confidence": 0.8}}


class Ex:
    def __init__(self, qty):
        self.mark = 108.85; self.qty = float(qty); self.entry = 111.45; self.orders = []; self.stops = []; self.limits = []; self.active = []
    def instruments(self): return {"GAZPF": {"base": "GAZP", "mult": 100.0, "min_qty": 1, "tick": 0.01, "lot": 1}}
    def positions(self):
        if self.qty <= 0:
            return []
        return [{"symbol": "GAZPF", "side": "Sell", "qty": self.qty, "entry": self.entry, "mark": self.mark, "upnl": 0, "mult": 100.0, "leverage": 0.3, "sl": 0, "tp": 0, "liq": 0}]
    def klines(self, sym, iv, n):
        now_ms = int(time.time() * 1000)
        if iv == "D":
            return pd.DataFrame({"ts": [now_ms - (30 - i) * 86400000 for i in range(30)], "close": [113.0] * 30})
        return pd.DataFrame({"ts": [now_ms - 7200000, now_ms - 3600000, now_ms - 600000], "close": [self.mark] * 3})
    def fmt_qty(self, sym, q): return str(int(q))
    def place_market(self, sym, side, qty, **k):
        self.orders.append((side, qty)); self.qty -= qty
    def set_stop_loss(self, sym, p, side_closing=None): self.stops.append((round(p, 2), self.qty))
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
    def __init__(self, v, c): self.verdict, self.confidence, self.reasoning = v, c, "важная противоположная новость <тест>"


def setup(qty, verdict):
    j = Journal(":memory:"); ex = Ex(qty); tg = T(); e = Executor(S({"risk": RISK}), ex, None, j, tg)
    e.risk = type("R", (), {"tradable": lambda s, x: 96000.0})()
    plan = e._tp_plan("GAZPF", float(qty), 111.45, 1.131, long=False)
    now = int(time.time())
    tid = j.open_trade({"symbol": "GAZPF", "side": "Sell", "qty": float(qty), "entry": 111.45, "stop": 111.283, "tp": 0, "stop_init": 112.581, "tier": "low",
                        "score": -0.6, "confidence": 0.7, "reasons": [], "regime": "trend_down", "atr": 0.566, "leverage": 1, "opened_ts": now,
                        "status": "open", "order_id": "", "news_driven": 0, "qty_init": float(qty), "tp_plan": plan, "tp_hits": 0, "event_key": "", "mult": 100, "fees": 0})
    j._run("UPDATE trades SET entry_init=111.45, opened_ts=?, stop=111.283, stop_init=112.581, mfe_r=2.3 WHERE id=?", (now - 12 * 3600, tid))
    for lvl in plan:
        lvl["order_id"] = "tp-live"
    j.update_tp_plan(tid, plan); ex.active.append("tp-live")
    e.driver = type("D", (), {"maybe_check": lambda self, *a, **k: verdict})()
    return j, ex, tg, e


# 1) 4 контракта, «забрать» 0.85 при +2.3R: закрыто 2, стоп перевыставлен на 2, второй раз не режем
j, ex, tg, e = setup(4, Verdict("take", 0.85))
e.manage()
t = j.open_trades()[0]
print("после вердикта: заявки", ex.orders, "| объём в журнале", t["qty"], "| закрыто наблюдателем", t["cut_qty"], "| стопы (цена, объём)", ex.stops)
assert ex.orders == [("Buy", 2.0)] and t["qty"] == 2.0 and t["cut_qty"] == 2.0 and (t["tp_hits"] or 0) == 0
assert ex.stops and all(q == 2.0 for _, q in ex.stops), "стоп-заявка должна стоять на остаток"
assert abs(t["stop"] - (111.45 - 0.5 * 2.2988 * 1.131)) < 0.03, t["stop"]
assert any("ЧАСТЬ ПРИБЫЛИ ЗАБРАНА" in m and "&lt;тест&gt;" in m for m in tg.msgs)
e.manage()
assert ex.orders == [("Buy", 2.0)], "второй раз по той же сделке не режем"
# исполнился лимитный тейк на 1 контракт: это первый тейк, а не «нулевой» и не «второй»
ex.qty = 1.0
e.manage()
t = j.open_trades()[0]
print("после тейка: объём", t["qty"], "тейков", t["tp_hits"])
assert t["qty"] == 1.0 and t["tp_hits"] == 1

# 2) уверенность 0.7: только стоп, без закрытия
j, ex, tg, e = setup(4, Verdict("take", 0.7)); e.manage()
assert ex.orders == [] and j.open_trades()[0]["qty"] == 4.0 and ex.stops
print("уверенность 0.7: заявок на закрытие нет, стоп подтянут", ex.stops[-1])

# 3) «подтянуть» 0.9: закрытия нет
j, ex, tg, e = setup(4, Verdict("tighten", 0.9)); e.manage()
assert ex.orders == []

# 4) один контракт: не делим
j, ex, tg, e = setup(1, Verdict("take", 0.9)); e.manage()
assert ex.orders == [] and j.open_trades()[0]["qty"] == 1.0
print("один контракт: не делится")

# 5) два контракта, тейк на 1 стоит: режем 1, остаётся 1 под тейк
j, ex, tg, e = setup(2, Verdict("take", 0.9)); e.manage()
assert ex.orders == [("Buy", 1.0)] and j.open_trades()[0]["qty"] == 1.0
print("два контракта: закрыт 1, остаток 1")

# 6) лимиты 8/3/6
SECT = {"oil_gas": ["A1", "A2", "A3", "A4"], "banks": ["B1"]}
R2 = {"max_open_positions": 8, "risk_per_trade_pct": {"low": 0.4, "medium": 0.8, "high": 1.0}, "max_direction_risk_pct": 5.0, "max_total_exposure_x": 3,
      "max_same_direction": 6, "max_per_base": 1, "max_per_sector": 3, "sectors": SECT}
names = ["A1", "A2", "A3", "A4", "B1"] + [f"X{i}" for i in range(8)]


class Ex2:
    def instruments(self): return {n: {"base": n, "mult": 1.0} for n in names}


rm = RiskManager(S({"risk": R2}), Ex2(), Journal(":memory:"))
rm.tradable = lambda eq: 100000.0
mk = lambda s, side="Buy": {"symbol": s, "side": side, "entry": 100.0, "stop_init": 99.6, "qty": 1000, "qty_init": 1000, "event_key": "", "opened_ts": time.time()}  # noqa: E731
held = [mk("A1"), mk("A2"), mk("A3")]
ok, why = rm.can_open("A4", held, [], 100000, "long", "", "low"); assert not ok and rm.last_reject == "sector", why
held = [mk("A1"), mk("A2"), mk("X0"), mk("X1"), mk("X2"), mk("X3")]
ok, why = rm.can_open("X4", held, [], 100000, "long", "", "low"); assert not ok and rm.last_reject == "same_direction", why
ok, why = rm.can_open("X4", held, [], 100000, "short", "", "low"); assert ok, why
held = [mk("A1"), mk("A2"), mk("X0"), mk("X1"), mk("X2"), mk("X3", "Sell"), mk("X5", "Sell"), mk("X6", "Sell")]
ok, why = rm.can_open("X4", held, [], 100000, "short", "", "low"); assert not ok and rm.last_reject == "max_positions", why
print("лимиты 8/3/6: сектор, сторона и общий лимит работают")
print("ВСЁ ОК")
