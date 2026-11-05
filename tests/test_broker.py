"""Сценарные тесты на эмуляторе брокера. Эмулятор ведёт себя как T-Invest там, где бот ошибался:
объём стоп-заявки фиксирован в момент постановки; заявка больше позиции открывает встречную позицию; лимитные заявки
сгорают в конце дня; когда торги закрыты, рыночная заявка отклоняется; инструмент может пропасть из справочника.
Запуск из корня проекта: python test_broker.py"""
import sys
import time

import pandas as pd

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")
import bot.executor as executor_mod  # noqa: E402
from bot.executor import Executor  # noqa: E402
from bot.journal import Journal  # noqa: E402

executor_mod.time.sleep = lambda s: None   # паузы ожидания брокера в тестах не нужны
NOW = time.time()


class S:
    def __init__(self, d): self.d = d
    def get(self, k, d=None):
        cur = self.d
        for part in k.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return d
            cur = cur[part]
        return cur


def risk_cfg(**over):
    r = {"daily_trend_exit": True, "max_hours_daily_exit": 720, "pyramid_part": 0.5, "pyramid_r": 1.5, "soft_stops": True,
         "max_hours_in_trade": 96, "breakeven_r": 0.7, "giveback_r": 1.2, "giveback_min_peak_r": 1.5, "stall_hours": 24, "trail_after_r": 1.0,
         "giveback_tiers": [[1.5, 1.5], [3.0, 1.0], [5.0, 0.6]], "tp_levels": [{"r": 3.0, "part": 0.33}], "pyramid_stop_breakeven": True,
         "daily_peak_lock": {"min_peak_r": 3.0, "keep": 0.5}, "breakeven_after_tp1": True, "close_days_before_expiry": 3,
         "news_exit_min_importance": 7, "sectors": {}, "news_exit_max_names": 4, "news_wide_tighten": True,
         "driver_check": {"mode": "tighten", "min_peak_r": 2.0, "every_min": 30, "min_confidence": 0.6, "keep": 0.5, "take_part": 0.5, "take_min_confidence": 0.8}}
    r.update(over)
    return r


class Broker:
    """Эмулятор брокера: позиции со знаком, стоп-заявки с фиксированным объёмом, дневные лимитные заявки."""
    def __init__(self):
        self.inst = {}
        self.pos = {}          # sym -> [лоты со знаком, средний вход]
        self.price = {}
        self.stops = []        # {id, sym, side, qty, price}
        self.limits = []       # {id, sym, side, qty, price, status, filled}
        self.dead = {}         # id -> статус снятых/исполненных заявок
        self.open_now = True
        self.fail = set()      # имена методов, которые сейчас падают
        self.sma, self.daily_len, self.trend4h, self.hour_close = {}, {}, {}, {}
        self.log = []
        self._n = 0

    def add(self, sym, price, base=None, expiration=None, kind="future"):
        self.inst[sym] = {"base": base or sym, "mult": 100.0, "min_qty": 1, "tick": 0.01, "lot": 1, "kind": kind, "expiration": expiration}
        self.price[sym] = price

    def _id(self, p):
        self._n += 1
        return f"{p}{self._n}"

    def _chk(self, name):
        if name in self.fail:
            raise RuntimeError(f"relay timeout ({name})")

    # --- интерфейс биржи ---
    def instruments(self): return self.inst
    def trading_open(self, sym): return self.open_now
    def last_price(self, sym): return self.price[sym]
    def price_limits(self, sym): return (self.price[sym] * 0.8, self.price[sym] * 1.2)
    def fmt_qty(self, sym, q): return str(int(q))
    def equity(self): return 100000.0, 100000.0
    def closed_pnl(self, sym, start_ms=None, **k): return []

    def positions(self):
        self._chk("positions")
        out = []
        for sym, (q, e) in self.pos.items():
            if abs(q) > 1e-9 and sym in self.inst:
                out.append({"symbol": sym, "side": "Buy" if q > 0 else "Sell", "qty": abs(q), "entry": e, "mark": self.price[sym],
                            "mark_ok": True, "upnl": 0, "mult": 100.0, "leverage": 0.3, "sl": 0, "tp": 0, "liq": 0})
        return out

    def klines(self, sym, iv, n):
        self._chk("klines")
        now_ms = int(time.time() * 1000)
        if iv == "D":
            n = min(n, self.daily_len.get(sym, n))
            return pd.DataFrame({"ts": [now_ms - (n - i) * 86400000 for i in range(n)], "close": [self.sma.get(sym, self.price[sym])] * n})
        if iv == "240":
            up = self.trend4h.get(sym, 0)
            return pd.DataFrame({"ts": range(80), "close": [100 + up * i * 0.25 for i in range(80)]})
        c = self.hour_close.get(sym, self.price[sym])
        return pd.DataFrame({"ts": [now_ms - 7200000, now_ms - 3600000, now_ms - 600000], "close": [c] * 3})

    def _trade(self, sym, side, qty, price):
        q, e = self.pos.get(sym, [0.0, 0.0])
        d = qty if side == "Buy" else -qty
        if q == 0 or (q > 0) == (d > 0):
            e = (abs(q) * e + qty * price) / (abs(q) + qty)
        elif abs(d) > abs(q):
            e = price   # позиция перевернулась
        self.pos[sym] = [q + d, e]
        self.log.append((sym, side, qty, price))

    def place_market(self, sym, side, qty, sl=None, tp=None, reduce_only=False):
        self._chk("place_market")
        if not self.open_now:
            raise RuntimeError("30079 instrument is not available for trading")
        self._trade(sym, side, float(qty), self.price[sym])
        return self._id("m")

    def place_limit(self, sym, side, qty, price, reduce_only=True):
        self._chk("place_limit")
        oid = self._id("l")
        self.limits.append({"id": oid, "sym": sym, "side": side, "qty": float(qty), "price": price})
        return oid

    def set_stop_loss(self, sym, sl, side_closing=None):
        self._chk("set_stop_loss")
        q = self.pos.get(sym, [0, 0])[0]
        if not q:
            return
        new = {"id": self._id("s"), "sym": sym, "side": "Sell" if q > 0 else "Buy", "qty": abs(q), "price": sl}
        self.stops = [s for s in self.stops if s["sym"] != sym] + [new]

    def open_orders(self, sym):
        try:
            return self.open_orders_strict(sym)
        except Exception:  # noqa: BLE001
            return []     # как у настоящего клиента: при ошибке пустой список

    def open_orders_strict(self, sym):
        self._chk("open_orders")
        return [{"orderId": o["id"]} for o in self.limits if o["sym"] == sym]

    def order_status(self, sym, oid):
        self._chk("order_status")
        if any(o["id"] == oid for o in self.limits):
            return {"status": "New", "filled": 0.0}
        return {"status": self.dead.get(oid, "Cancelled"), "filled": 0.0}

    def cancel_order(self, sym, oid):
        self.dead.update({o["id"]: "Cancelled" for o in self.limits if o["id"] == oid})
        self.limits = [o for o in self.limits if o["id"] != oid]

    def cancel_stop(self, sid):
        self.stops = [s for s in self.stops if s["id"] != sid]

    def cancel_all(self, sym):
        self._chk("cancel_all")
        if sym not in self.inst:
            raise KeyError(sym)
        for o in [o for o in self.limits if o["sym"] == sym]:
            self.cancel_order(sym, o["id"])
        self.stops = [s for s in self.stops if s["sym"] != sym]

    def close_position(self, sym):
        q = self.pos.get(sym, [0, 0])[0]
        if q:
            return self.place_market(sym, "Sell" if q > 0 else "Buy", abs(q), reduce_only=True)

    def orders_view(self):
        self._chk("orders_view")
        return {"stops": [{"symbol": s["sym"], "id": s["id"], "qty": s["qty"], "price": s["price"], "side": s["side"]} for s in self.stops],
                "limits": [{"symbol": o["sym"], "id": o["id"], "qty": o["qty"], "price": o["price"], "side": o["side"]} for o in self.limits]}

    # --- рынок ---
    def move(self, sym, price):
        """Цена пришла в точку: исполняются лимитные заявки и стоп-заявки (своим фиксированным объёмом)."""
        self.price[sym] = price
        for o in [o for o in self.limits if o["sym"] == sym]:
            if (o["side"] == "Sell" and price >= o["price"]) or (o["side"] == "Buy" and price <= o["price"]):
                self._trade(sym, o["side"], o["qty"], o["price"])
                self.dead[o["id"]] = "Filled"
                self.limits.remove(o)
        for s in [s for s in self.stops if s["sym"] == sym]:
            if (s["side"] == "Sell" and price <= s["price"]) or (s["side"] == "Buy" and price >= s["price"]):
                self._trade(sym, s["side"], s["qty"], price)
                self.stops.remove(s)

    def end_of_day(self):
        for o in self.limits:
            self.dead[o["id"]] = "Cancelled"
        self.limits = []

    def qty(self, sym): return self.pos.get(sym, [0, 0])[0]
    def stop(self, sym): return next((s for s in self.stops if s["sym"] == sym), None)


class T:
    def __init__(self): self.msgs = []
    def send(self, text, html=False): self.msgs.append(text)
    def has(self, s): return any(s in m for m in self.msgs)


class Risk:
    def tradable(self, eq): return 96000.0
    def _alias(self, b): return {"SBERP": "SBER"}.get(b, b)


def bot(**over):
    b = Broker()
    j, tg = Journal(":memory:"), T()
    e = Executor(S({"risk": risk_cfg(**over)}), b, Risk(), j, tg)
    return b, j, tg, e


def trade(b, j, e, sym, side, qty, entry, stop_init, hours=30, stop=None, tps=True, **cols):
    """Позиция у брокера + запись в журнале + стоп-заявка + тейк по плану, как после _finish_entry."""
    long = side == "Buy"
    b.pos[sym] = [qty if long else -qty, entry]
    dist = abs(entry - stop_init)
    plan = e._tp_plan(sym, float(qty), entry, dist, long) if tps else []
    for lvl in plan:
        lvl["order_id"] = b.place_limit(sym, "Sell" if long else "Buy", lvl["qty"], lvl["price"])
    tid = j.open_trade({"symbol": sym, "side": side, "qty": float(qty), "entry": entry, "stop": stop_init, "tp": 0, "tier": "low", "score": 0.6,
                        "confidence": 0.7, "reasons": [], "regime": "trend", "atr": dist / 2, "leverage": 1, "order_id": "", "news_driven": 0,
                        "tp_plan": plan, "event_key": "", "mult": 100, "fees": 0})
    cols = dict({"entry_init": entry, "opened_ts": int(NOW - hours * 3600), "stop": stop if stop is not None else stop_init}, **cols)
    j._run("UPDATE trades SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?", (*cols.values(), tid))
    b.set_stop_loss(sym, cols["stop"])
    return tid


def row(j, tid): return j._run("SELECT * FROM trades WHERE id=?", (tid,), "one")
def title(s): print("\n— " + s)


# 1) Выход по дневному тренду ночью: биржа закрыта — стоп-заявка и тейк остаются; утром сделка закрывается
title("1. закрытие при закрытой бирже не снимает стоп-заявку")
b, j, tg, e = bot(pyramid_part=0)
b.add("GAZPF", 111.0); b.sma["GAZPF"] = 112.0; b.hour_close["GAZPF"] = 111.0     # лонг, час закрылся ниже средней
tid = trade(b, j, e, "GAZPF", "Buy", 3, 110.0, 108.0)
b.open_now = False
for _ in range(3):
    assert e.manage() == []
assert b.stop("GAZPF") and b.stop("GAZPF")["qty"] == 3 and len(b.limits) == 1 and b.qty("GAZPF") == 3, "ночью ничего не снято"
assert row(j, tid)["status"] == "open"
b.open_now = True
assert len(e.manage()) == 1 and b.qty("GAZPF") == 0 and not b.stops and not b.limits
print("   ночью: стоп и тейк на месте, позиция цела; утром закрыта:", row(j, tid)["close_reason"][:40])

# 2) Контр-новость ночью: закрытие отложено и выполнено при открытии
title("2. контр-новость при закрытой бирже: закрытие отложено, а не потеряно")
b, j, tg, e = bot(pyramid_part=0)
b.add("LKOH", 7000.0)
tid = trade(b, j, e, "LKOH", "Buy", 2, 7000.0, 6900.0, hours=5)
b.open_now = False
ev = {"importance": 8, "confidence": 0.9, "direction": "bearish", "market_wide": False, "event_type": "regulation", "title": "Лукойл: санкции", "affected": ["LKOH"]}
assert e.react_to_events([ev]) == [] and b.stop("LKOH") and b.qty("LKOH") == 2
assert tg.has("ЗАКРЫТИЕ ОТЛОЖЕНО") and str(tid) in e._pc_all()
e.manage(); e.manage()
assert sum("ЗАКРЫТИЕ ОТЛОЖЕНО" in m for m in tg.msgs) == 1, "одно сообщение, без повторов"
b.open_now = True
assert len(e.manage()) == 1 and b.qty("LKOH") == 0 and "контр-новость" in row(j, tid)["close_reason"] and not e._pc_all()
print("   отложено одним сообщением, закрыто при открытии с причиной:", row(j, tid)["close_reason"][:36])

# 3) Тейк исполнился: стоп-заявка переставлена на остаток, пик засчитан, защита половины пика включилась
title("3. после тейка стоп-заявка на остаток, пик не меньше уровня тейка")
b, j, tg, e = bot(pyramid_part=0)
b.add("GAZPF", 111.45); b.sma["GAZPF"] = 113.0
tid = trade(b, j, e, "GAZPF", "Sell", 4, 111.45, 112.64, stop=111.28, mfe_r=2.96)   # R = 1.19, стоп уже в безубытке
b.move("GAZPF", 107.88)      # тейк 3R = 107.88 исполнился (1 контракт)
b.move("GAZPF", 107.95)      # опрос застал цену чуть хуже: 2.94R
e.manage()
t = row(j, tid)
assert b.qty("GAZPF") == -3 and t["qty"] == 3 and t["tp_hits"] == 1
assert b.stop("GAZPF")["qty"] == 3, b.stop("GAZPF")
assert t["mfe_r"] >= 3.0 and abs(t["stop"] - (111.45 - 0.5 * t["mfe_r"] * 1.19)) < 0.02, (t["mfe_r"], t["stop"])
b.move("GAZPF", t["stop"] + 0.01)        # цена вернулась к стопу: закрывается ровно остаток, встречной позиции нет
assert b.qty("GAZPF") == 0
print(f"   стоп-заявка на 3, пик {t['mfe_r']:.2f}R, стоп {t['stop']:.2f} (половина пика); возврат к стопу закрыл ровно остаток")

# 4) Защита половины пика, когда цена уже ниже уровня: закрытие по рынку, а не стоп «за ценой»
title("4. уровень защиты уже пройден: закрываю по рынку")
b, j, tg, e = bot(pyramid_part=0)
b.add("ROSN", 460.0); b.sma["ROSN"] = 440.0
tid = trade(b, j, e, "ROSN", "Buy", 5, 450.0, 440.0, stop=450.7, mfe_r=3.2, tps=False)   # пик 3.2R, сейчас 1.0R: половина пика = 466
closed = e.manage()
assert len(closed) == 1 and b.qty("ROSN") == 0 and "уже пройден" in row(j, tid)["close_reason"]
print("   закрыто:", row(j, tid)["close_reason"][:70])

# 5) Перестановка стопа не удалась: старая стоп-заявка остаётся
title("5. сбой при перестановке стопа: старая заявка на месте")
b, j, tg, e = bot(pyramid_part=0)
b.add("SBERF", 310.0); b.sma["SBERF"] = 300.0
tid = trade(b, j, e, "SBERF", "Buy", 2, 305.0, 300.0, tps=False)     # +1R: должен переехать в безубыток
b.fail.add("set_stop_loss")
e.manage()
assert b.stop("SBERF")["price"] == 300.0 and row(j, tid)["stop"] == 300.0
b.fail.clear(); e.manage()
assert abs(b.stop("SBERF")["price"] - 305.0 * 1.0015) < 1e-6 and abs(row(j, tid)["stop"] - 305.0 * 1.0015) < 1e-6
print("   при сбое стоп и журнал прежние; после восстановления связи стоп в безубытке")

# 6) Инструмент пропал из справочника: сделка закрывается записью, остальные ведутся
title("6. пропавший инструмент не останавливает ведение остальных сделок")
b, j, tg, e = bot(pyramid_part=0)
b.add("SRZ6", 31000.0, base="SBER", expiration=time.strftime("%Y-%m-%d", time.gmtime(NOW + 40 * 86400)))
b.add("SBERF", 310.0); b.sma["SBERF"] = 300.0; b.sma["SRZ6"] = 30000.0
t1 = trade(b, j, e, "SRZ6", "Buy", 1, 31000.0, 30500.0, tps=False)
t2 = trade(b, j, e, "SBERF", "Buy", 2, 305.0, 300.0, tps=False)
del b.inst["SRZ6"]
closed = e.manage()
assert [c["id"] for c in closed] == [t1] and "пропал из справочника" in row(j, t1)["close_reason"]
assert abs(row(j, t2)["stop"] - 305.0 * 1.0015) < 1e-6, "вторая сделка ведётся"
print("   запись закрыта с причиной «инструмент пропал», вторая сделка переведена в безубыток")
# и любая ошибка по одной сделке не мешает другой
b, j, tg, e = bot(pyramid_part=0)
b.add("AAA", 100.0); b.add("SBERF", 310.0); b.sma["SBERF"] = 300.0; b.sma["AAA"] = 90.0
t1 = trade(b, j, e, "AAA", "Buy", 1, 100.0, 98.0, tps=False)
t2 = trade(b, j, e, "SBERF", "Buy", 2, 305.0, 300.0, tps=False)
j._run("UPDATE trades SET stop_init=NULL WHERE id=?", (t1,))      # испорченная запись: ошибка внутри ведения
e.manage()
assert abs(row(j, t2)["stop"] - 305.0 * 1.0015) < 1e-6 and tg.has("СДЕЛКА НЕ ВЕДЁТСЯ")
print("   ошибка по одной сделке: сообщение владельцу, вторая сделка ведётся")

# 7) Фьючерс за 3 дня до экспирации закрывается
title("7. выход перед экспирацией")
b, j, tg, e = bot(pyramid_part=0)
b.add("GZZ6", 11200.0, base="GAZP", expiration=time.strftime("%Y-%m-%d", time.gmtime(NOW + 2 * 86400)))
tid = trade(b, j, e, "GZZ6", "Buy", 1, 11200.0, 11000.0, tps=False)
assert len(e.manage()) == 1 and "экспирации" in row(j, tid)["close_reason"]
print("   закрыто:", row(j, tid)["close_reason"])

# 8) Нет 20-дневной средней (новый контракт): работают обычные выходы
title("8. дневной режим без средней: защита прибыли обычным правилом")
b, j, tg, e = bot(pyramid_part=0)
b.add("NEW", 103.0); b.daily_len["NEW"] = 10
tid = trade(b, j, e, "NEW", "Buy", 2, 100.0, 98.0, stop=100.15, mfe_r=2.0, tps=False)   # пик 2R, сейчас 1.5R... откат 0.5R
b.price["NEW"] = 100.8      # 0.4R: откат от пика 1.6R > 1.5R
assert len(e.manage()) == 1 and "защита прибыли" in row(j, tid)["close_reason"]
print("   закрыто:", row(j, tid)["close_reason"])
# а при короткой недоступности данных дневной режим сохраняется (обычные выходы не срабатывают от разового сбоя)
b, j, tg, e = bot(pyramid_part=0)
b.add("GAZP", 100.8)
tid = trade(b, j, e, "GAZP", "Buy", 2, 100.0, 98.0, stop=100.15, mfe_r=2.0, tps=False)
b.fail.add("klines")
assert e.manage() == [] and row(j, tid)["status"] == "open"
print("   разовый сбой данных: сделка не закрыта обычным правилом")

# 9) Тейки не дублируются, когда брокер не отдал список заявок или заявка жива
title("9. тейк не выставляется дублем")
b, j, tg, e = bot(pyramid_part=0)
b.add("GAZPF", 111.0); b.sma["GAZPF"] = 113.0
tid = trade(b, j, e, "GAZPF", "Sell", 3, 111.45, 112.64, hours=2)
b.fail.add("open_orders"); e.manage()
assert len(b.limits) == 1, "список заявок не получен: ничего не выставляем"
b.fail.clear(); e._tp_checked.clear(); e.manage()
assert len(b.limits) == 1, "заявка жива: не дублируем"
b.end_of_day(); e._tp_checked.clear(); e.manage()
assert len(b.limits) == 1 and b.limits[0]["qty"] == 1.0, "сгоревший тейк перевыставлен один раз"
e._tp_checked.clear(); e.manage()
assert len(b.limits) == 1
print("   сбой списка — без дубля; живая заявка — без дубля; сгоревшая — перевыставлена один раз")

# 10) Добор: не после тейка, не после разгрузки; исполнился при сбое ответа — журнал и стоп приведены к факту
title("10. добор")
b, j, tg, e = bot()
b.add("GAZPF", 109.5); b.sma["GAZPF"] = 113.0; b.trend4h["GAZPF"] = -1
tid = trade(b, j, e, "GAZPF", "Sell", 4, 111.45, 112.64, stop=111.28, tp_hits=1, tps=False)
e.manage()
assert b.qty("GAZPF") == -4 and not row(j, tid)["add_qty"], "после тейка не добираем"
j._run("UPDATE trades SET tp_hits=0, cut_qty=2 WHERE id=?", (tid,)); e._pyr_checked.clear(); e.manage()
assert b.qty("GAZPF") == -4, "после разгрузки не добираем"
b, j, tg, e = bot()
b.add("GAZPF", 109.5); b.sma["GAZPF"] = 113.0; b.trend4h["GAZPF"] = -1
tid = trade(b, j, e, "GAZPF", "Sell", 4, 111.45, 112.64, stop=111.28, tps=False)
orig_positions, calls = b.positions, {"n": 0}


def flaky():
    calls["n"] += 1
    if calls["n"] == 2:      # первый вызов — в manage, второй — сразу после заявки на добор
        raise RuntimeError("relay timeout")
    return orig_positions()


b.positions = flaky
e.manage()
t = row(j, tid)
assert b.qty("GAZPF") == -6 and t["qty"] == 6 and t["add_qty"] == 2, (b.qty("GAZPF"), t["qty"], t["add_qty"])
assert b.stop("GAZPF")["qty"] == 6 and b.stop("GAZPF")["price"] < 111.28, b.stop("GAZPF")
print(f"   добор исполнен при сбое ответа: журнал {t['qty']:g}, стоп-заявка на {b.stop('GAZPF')['qty']:g} по {b.stop('GAZPF')['price']:.2f} (общий безубыток)")

# 11) Встречная позиция (заявка закрыла больше остатка): закрывается, владельцу сообщение
title("11. встречная позиция")
b, j, tg, e = bot(pyramid_part=0)
b.add("GAZPF", 110.0); b.sma["GAZPF"] = 113.0
tid = trade(b, j, e, "GAZPF", "Sell", 3, 111.45, 112.64, tps=False)
b.pos["GAZPF"] = [1, 110.0]
assert len(e.manage()) == 1 and b.qty("GAZPF") == 0 and tg.has("ПОЗИЦИЯ ПЕРЕВЕРНУЛАСЬ")
print("   закрыта, сообщение отправлено")

# 12) Позиция пропала из одного ответа брокера: не закрываем запись сразу
title("12. один пропуск позиции в ответе — не закрытие")
b, j, tg, e = bot(pyramid_part=0)
b.add("GAZPF", 111.0); b.sma["GAZPF"] = 113.0
tid = trade(b, j, e, "GAZPF", "Sell", 3, 111.45, 112.64, tps=False)
keep, b.pos = b.pos, {}
assert e.manage() == [] and row(j, tid)["status"] == "open" and b.stop("GAZPF"), "стоп-заявка не снята"
b.pos = keep
assert e.manage() == [] and row(j, tid)["status"] == "open"
b.pos = {}
e.manage()
assert len(e.manage()) == 1 and row(j, tid)["status"] == "closed"
print("   один пропуск — сделка и стоп на месте; два подряд — закрыта")

# 13) Сверка заявок
title("13. сверка заявок с журналом")
b, j, tg, e = bot(pyramid_part=0)
b.add("GAZPF", 111.0); b.add("OLD", 50.0); b.add("SBERF", 310.0); b.sma["GAZPF"] = 113.0
tid = trade(b, j, e, "GAZPF", "Sell", 3, 111.45, 112.64, hours=2)
b.stops.append({"id": "s-old", "sym": "OLD", "side": "Sell", "qty": 5.0, "price": 48.0})          # забытая стоп-заявка
b.limits.append({"id": "l-old", "sym": "SBERF", "side": "Sell", "qty": 1.0, "price": 330.0})     # забытый тейк
b.stop("GAZPF")["qty"] = 5.0                                                                      # стоп на чужой объём
b.limits.append({"id": "l-dup", "sym": "GAZPF", "side": "Buy", "qty": 3.0, "price": 107.88})    # дубль: тейков 4 при позиции 3
e.reconcile()
assert not [s for s in b.stops if s["sym"] == "OLD"] and not [o for o in b.limits if o["sym"] == "SBERF"]
assert b.stop("GAZPF")["qty"] == 3 and abs(b.stop("GAZPF")["price"] - 112.64) < 1e-9
assert not [o for o in b.limits if o["sym"] == "GAZPF"], "лишние тейки сняты"
e.manage()
assert [o["qty"] for o in b.limits if o["sym"] == "GAZPF"] == [1.0], "тейк выставлен заново по плану"
b.fail.add("orders_view"); e._audit_ts = 0; e.reconcile(); b.fail.clear()
assert b.stop("GAZPF")["qty"] == 3 and len(b.limits) == 1, "списки не получены: ничего не трогаем"
# отложенный вход не трогаем
j.add_pending({"symbol": "SBERF", "side": "Buy", "qty": 1, "price": 305.0, "stop": 300.0, "order_id": "l-pend", "expires_ts": int(NOW) + 3600, "event_key": "", "payload": {}})
b.limits.append({"id": "l-pend", "sym": "SBERF", "side": "Buy", "qty": 1.0, "price": 305.0})
e._audit_ts = 0; e._audit_orders()
assert [o for o in b.limits if o["id"] == "l-pend"]
print("   забытые заявки сняты, стоп приведён к позиции, дубль тейка снят и выставлен заново, отложенный вход не тронут")

# 14) Результат сделки: окно не захватывает соседние сделки, неполный ответ не принимается за итог
title("14. расчёт результата")
b, j, tg, e = bot(pyramid_part=0)
b.add("GZM7", 12600.0, base="GAZP"); b.price_based_pnl = True
seen = {}


def cp(sym, start_ms=None, entry=None, side=None, end_ms=None):
    seen.update(start=start_ms, end=end_ms)
    return [{"pnl": 100.0, "exit": 12650.0, "fees": 5.0, "ts": start_ms + 1000, "qty": seen.get("qty", 4.0)}]


b.closed_pnl = cp
a = j.open_trade({"symbol": "GZM7", "side": "Buy", "qty": 2.0, "entry": 12600.0, "stop": 12500.0, "tp": 0, "tier": "low", "score": 0, "confidence": 0, "reasons": [],
                  "regime": "", "atr": 50, "leverage": 1, "tp_plan": [], "mult": 1})
j._run("UPDATE trades SET status='closed', opened_ts=?, closed_ts=? WHERE id=?", (int(NOW) - 7200, int(NOW) - 600, a))
bid = j.open_trade({"symbol": "GZM7", "side": "Buy", "qty": 4.0, "entry": 12620.0, "stop": 12520.0, "tp": 0, "tier": "low", "score": 0, "confidence": 0, "reasons": [],
                    "regime": "", "atr": 50, "leverage": 1, "tp_plan": [], "mult": 1})
j._run("UPDATE trades SET opened_ts=?, entry_init=entry WHERE id=?", (int(NOW) - 300, bid))
res = e._exchange_result(row(j, bid))
assert res and seen["start"] == (int(NOW) - 600 + 1) * 1000 and seen["end"] is None, seen
seen["qty"] = 1.0      # брокер отдал только тейк на 1 контракт из 4
assert e._exchange_result(row(j, bid)) is None
j._run("UPDATE trades SET closed_ts=? WHERE id=?", (int(NOW) - 100, bid)); seen["qty"] = 4.0
e._exchange_result(row(j, bid))
assert seen["end"] == (int(NOW) - 100 + 300) * 1000
print("   окно начинается после закрытия предыдущей сделки и ограничено сверху при уточнении; неполный ответ отклонён")

# 15) Наблюдатель: сбой ответа после частичного закрытия не превращает его в «тейк»
title("15. частичное закрытие при сбое ответа брокера")
b, j, tg, e = bot(pyramid_part=0)
b.add("GAZPF", 108.85); b.sma["GAZPF"] = 113.0
tid = trade(b, j, e, "GAZPF", "Sell", 4, 111.45, 112.581, stop=111.283, mfe_r=2.3, hours=12)
e.driver = type("D", (), {"maybe_check": lambda self, *a, **k: type("V", (), {"verdict": "take", "confidence": 0.9, "reasoning": "тест"})()})()
orig_positions, calls = b.positions, {"n": 0}
b.positions = flaky
e.manage()
t = row(j, tid)
assert b.qty("GAZPF") == -2 and t["qty"] == 2 and t["cut_qty"] == 2 and (t["tp_hits"] or 0) == 0 and b.stop("GAZPF")["qty"] == 2
assert not tg.has("ЧАСТИЧНЫЙ ТЕЙК")
print("   журнал 2, закрыто наблюдателем 2, тейков 0, стоп-заявка на 2")

# 16) Контр-новость по обычной акции закрывает позицию в привилегированной
title("16. новость про «Сбер» и позиция в «Сбер-п»")
b, j, tg, e = bot(pyramid_part=0)
b.add("SBERP", 300.0, base="SBERP", kind="share")
tid = trade(b, j, e, "SBERP", "Buy", 10, 300.0, 295.0, hours=5, tps=False)
ev = {"importance": 8, "confidence": 0.9, "direction": "bearish", "market_wide": False, "event_type": "regulation", "title": "Сбер: штраф", "affected": ["SBER"]}
assert len(e.react_to_events([ev])) == 1 and b.qty("SBERP") == 0
print("   закрыта")

# 17) /panic ночью: честный отчёт, стоп-заявки на месте
title("17. закрыть всё при закрытой бирже")
b, j, tg, e = bot(pyramid_part=0)
b.add("GAZPF", 111.0)
tid = trade(b, j, e, "GAZPF", "Sell", 3, 111.45, 112.64, tps=False)
b.open_now = False
n = e.close_all("ручное закрытие (/panic)")
assert n == 0 and e.last_deferred == 1 and b.stop("GAZPF") and b.qty("GAZPF") == -3
b.open_now = True
assert len(e.manage()) == 1 and b.qty("GAZPF") == 0
print("   ночью: закрыто 0, отложено 1, стоп на месте; при открытии закрыто")
print("\nВСЁ ОК")
