"""Офлайн-проверка: исполнившийся отложенный ордер при отмене оформляется сделкой; позиция без записи подхватывается."""
import json
import sys
import time

import pandas as pd

sys.path.insert(0, ".")
from bot.executor import Executor  # noqa: E402
from bot.journal import Journal  # noqa: E402


class S:
    def get(self, k, d=None): return {"risk.stop_atr_mult": 2.0, "risk": {"tp_levels": []}}.get(k, d)


class Ex:
    def __init__(self): self.cancelled = []; self.stops = []; self.pos = []
    def instruments(self): return {"YDEX": {"base": "YDEX", "mult": 1.0, "min_qty": 1, "tick": 0.5, "lot": 1}}
    def order_status(self, sym, oid): return {"status": "Filled", "filled": 9.0, "avg": 3390.0, "qty": 9.0}
    def cancel_order(self, sym, oid): self.cancelled.append(oid)
    def balances(self): return {"equity": 96000.0}
    def positions(self): return self.pos
    def klines(self, sym, iv, n):
        c = [3400 + (i % 5) * 4 for i in range(n)]
        return pd.DataFrame({"ts": range(n), "open": c, "high": [x + 15 for x in c], "low": [x - 15 for x in c], "close": c, "volume": 1, "turnover": 1})
    def set_stop_loss(self, sym, p, side_closing=None): self.stops.append((sym, round(p, 1)))
    def cancel_all(self, sym): pass
    def fmt_qty(self, sym, q): return str(int(q))


class T:
    def __init__(self): self.msgs = []
    def send(self, text, html=False): self.msgs.append(text)


# --- 1. отложенный ордер уже исполнился, бот хочет его снять по контр-новости ---
j = Journal(":memory:"); ex = Ex(); tg = T()
e = Executor(S(), ex, None, j, tg)
registered = []
e._finish_entry = lambda sig, atr, dist, lev, oid, equity, price, qty: registered.append((sig.symbol, sig.direction, price, qty))
e._sig_from_payload = lambda sym, pay: type("Sig", (), {"symbol": sym, "direction": pay["direction"]})()
j.add_pending({"symbol": "YDEX", "side": "Sell", "qty": 9, "price": 3390.0, "stop": 3432.0, "order_id": "ord1", "created_ts": int(time.time()) - 120,
               "expires_ts": int(time.time()) + 3600, "event_key": "", "payload": {"direction": "short", "atr": 21.2, "dist": 42.4, "lev": 1.0}})
n = e.cancel_pendings("контр-новость", symbols={"YDEX"})
print("снято:", n, "оформлено:", registered, "pending осталось:", len(j.pendings()))
assert n == 0 and registered and registered[0][0] == "YDEX" and registered[0][3] == 9.0 and not j.pendings()

# --- 2. позиция у брокера без записи ---
j2 = Journal(":memory:"); ex2 = Ex(); tg2 = T()
ex2.pos = [{"symbol": "YDEX", "side": "Sell", "qty": 9.0, "entry": 3390.0, "mark": 3415.0, "upnl": -225.0, "mult": 1.0, "leverage": 0.3, "sl": 0.0, "tp": 0.0, "liq": 0.0}]
e2 = Executor(S(), ex2, None, j2, tg2)
adopted = e2.adopt_orphans()
ot = j2.open_trades()
print("подхвачено:", adopted, "| в журнале:", [(t["symbol"], t["side"], t["qty"], t["entry"], round(t["stop"], 1)) for t in ot], "| стоп у брокера:", ex2.stops)
assert adopted == ["YDEX"] and len(ot) == 1 and ot[0]["stop"] > 3390 and ex2.stops and "ПОД УПРАВЛЕНИЕ" in tg2.msgs[0]
assert e2.adopt_orphans() == [], "второй раз не подхватывает"
print("ВСЁ ОК")
