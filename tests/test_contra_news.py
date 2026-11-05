"""Офлайн-проверка трёх правок: контр-новость только по бумаге/сектору, пауза после неё, лимит входов в сутки."""
import sys
import time

sys.path.insert(0, ".")
from bot.executor import Executor  # noqa: E402
from bot.risk import RiskManager  # noqa: E402
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


SECT = {"oil_gas": ["GAZP", "LKOH", "NVTK", "ROSN"], "banks": ["SBER"]}
RISK = {"news_exit_min_importance": 7, "sectors": SECT, "news_exit_max_names": 4, "news_wide_tighten": True,
        "max_open_positions": 5, "risk_per_trade_pct": {"low": 0.5, "medium": 0.8, "high": 1.2}, "max_direction_risk_pct": 2.5,
        "max_total_exposure_x": 3, "max_same_direction": 3, "news_exit_cooldown_hours": 12, "max_entries_per_day": 2, "symbol_cooldown_hours": 3}


class Ex:
    def instruments(self): return {"GZM7": {"base": "GAZP"}, "GAZPF": {"base": "GAZP"}, "SBERF": {"base": "SBER"}}
    def cancel_all(self, s): pass
    def set_stop_loss(self, s, p): pass
    def close_position(self, s): pass


j = Journal(":memory:")
now = int(time.time())
tid = j.open_trade({"symbol": "GZM7", "side": "Buy", "qty": 4, "entry": 12660.0, "stop": 12520.0, "tp": 0, "stop_init": 12520.0, "tier": "medium",
                    "score": 0.6, "confidence": 0.7, "reasons": [], "regime": "trend_up", "atr": 70, "leverage": 1, "opened_ts": now - 3600,
                    "status": "open", "order_id": "", "news_driven": 0, "qty_init": 4, "tp_plan": [], "tp_hits": 0, "event_key": "", "mult": 1, "fees": 0})
ex = Executor(S({"risk": RISK}), Ex(), None, j, None)
closed_log = []
ex._close_now = lambda t, reason, **k: (closed_log.append((t["symbol"], reason)), True)[1]
moved = []
ex._move_stop = lambda t, p, why: moved.append((round(p), why))

wide = {"importance": 8, "confidence": 0.9, "direction": "bearish", "market_wide": False, "event_type": "macro", "title": "США предупредили союзников",
        "affected": ["SBER", "GAZP", "LKOH", "ROSN", "NVTK", "VTBR", "AFLT", "MOEX"]}
ex.react_to_events([wide])
print("общерыночная (8 тикеров):", "закрытий", len(closed_log), "стоп подтянут:", moved)
assert not closed_log and moved and moved[0][0] == 12590

specific = {"importance": 7, "confidence": 0.9, "direction": "bearish", "market_wide": False, "event_type": "regulation",
            "title": "Газпром: отмена дивидендов", "affected": ["GAZP"]}
ex.react_to_events([specific])
print("новость по бумаге:", closed_log)
assert closed_log and closed_log[0][0] == "GZM7"

closed_log.clear()
sector = {"importance": 7, "confidence": 0.9, "direction": "bearish", "market_wide": False, "event_type": "regulation",
          "title": "Новые санкции на нефтегаз", "affected": ["LKOH", "ROSN", "NVTK"]}
ex.react_to_events([sector])
print("секторная (3 тикера нефтегаза, Газпром не назван):", closed_log)
assert closed_log, "секторная новость должна закрывать"

# ---- пауза после контр-новости и лимит входов в сутки ----
j2 = Journal(":memory:")
for i, (ts_open, ts_close, reason) in enumerate([(now - 6 * 3600, now - 3 * 3600, "контр-новость [macro, imp 7]: США..."), (now - 2 * 3600, now - 600, "стоп/тейк на бирже")]):
    t = j2.open_trade({"symbol": "GZM7", "side": "Buy", "qty": 4, "entry": 12660.0, "stop": 12520.0, "tp": 0, "stop_init": 12520.0, "tier": "low",
                       "score": 0.6, "confidence": 0.7, "reasons": [], "regime": "trend_up", "atr": 70, "leverage": 1, "opened_ts": ts_open,
                       "status": "open", "order_id": "", "news_driven": 0, "qty_init": 4, "tp_plan": [], "tp_hits": 0, "event_key": "", "mult": 1, "fees": 0})
    j2._run("UPDATE trades SET status='closed', closed_ts=?, exit=12636, pnl=-73, pnl_r=-0.24, close_reason=? WHERE id=?", (ts_close, reason, t))
rm = RiskManager(S({"risk": RISK}), Ex(), j2)
rm.tradable = lambda eq: 100000.0
ok, why = rm.can_open("GAZPF", [], [], 100000, "long", "", "low")
print("вход после контр-новости 3 ч назад:", ok, why); assert not ok and rm.last_reject == "news_exit_cooldown"
ok, why = rm.can_open("GZM7", [], [], 100000, "short", "", "low")
print("шорт (другая сторона):", ok, why); assert ok
RISK["news_exit_cooldown_hours"] = 0
ok, why = rm.can_open("GAZPF", [], [], 100000, "long", "", "low")
print("без паузы, но 2 входа за сутки:", ok, why); assert not ok and rm.last_reject == "daily_entries"
ok, why = rm.can_open("SBERF", [], [], 100000, "long", "", "low")
print("другая компания:", ok); assert ok
print("ВСЁ ОК")
