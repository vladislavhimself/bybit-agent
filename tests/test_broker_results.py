"""Результат сделки по платежам брокера (РТС, добор), плата за маржу в шорте акций, итог по брокеру в отчёте.
Сверка 21.11.2026: журнал +2 287 руб., брокер +192 руб. Запуск из корня: python tests/test_broker_results.py"""
import sys
import time

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")
from bot import fmt  # noqa: E402
from bot.executor import Executor  # noqa: E402
from bot.moex import MoexExchange, iso  # noqa: E402


def q(v):
    neg = v < 0
    v = abs(v)
    u = int(v)
    n = int(round((v - u) * 1e9))
    return {"units": str(-u if neg else u), "nano": -n if neg else n}


def op(ts, typ, pay, figi="", qty=0, price=0.0, kind="futures"):
    return {"id": f"{ts}-{typ}-{pay}", "date": iso(ts), "operationType": "OPERATION_TYPE_" + typ, "payment": q(pay), "figi": figi, "quantity": str(qty),
            "price": q(price), "instrumentType": kind}


def make(ops, mode="demo"):
    ex = MoexExchange.__new__(MoexExchange)
    ex.s, ex.account, ex.mode = None, "acc", mode
    ex._figi2sym, ex._uid2sym = {"fRI": "RIZ6", "fGK": "GKZ6", "fMT": "MTLR"}, {}
    ex.instruments = lambda: {"RIZ6": {"kind": "future", "mult": 1.6359, "lot": 1}, "GKZ6": {"kind": "future", "mult": 1.0, "lot": 1},
                              "MTLR": {"kind": "share", "mult": 1.0, "lot": 1}}
    ex.mult = lambda sym: ex.instruments()[sym]["mult"]

    def call(svc, method, body=None, retries=8):
        assert method == "GetOperations", method
        a = time.mktime(time.strptime(body["from"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
        b = time.mktime(time.strptime(body["to"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
        return {"operations": [o for o in ops if a <= MoexExchange._op_ts(o) <= b]}
    ex.call = call
    return ex


T = int(time.time()) - 86400
# ---- РТС: стоимость пункта между входом и выходом изменилась (сделка 36: журнал −1 239, брокер −1 885) ----
ops = [op(T, "SELL", 153725.0, "fRI", 1, 93970.0), op(T, "BROKER_FEE", -76.9, "fRI"),
       op(T + 3600, "BUY", -155455.5, "fRI", 1, 94630.0), op(T + 3600, "BROKER_FEE", -77.7, "fRI")]
ex = make(ops)
r = ex.closed_pnl("RIZ6", start_ms=(T - 60) * 1000, entry=93970.0, side="Sell", end_ms=(T + 4000) * 1000)[0]
assert r.get("by_payments") and abs(r["pnl"] - (153725.0 - 155455.5 - 154.6)) < 1e-6, r
assert abs(r["fees"] - 154.6) < 1e-6 and r["exit"] == 94630.0 and r["qty"] == 1
print(f"РТС: результат по платежам {r['pnl']:.1f} (по ценам × шаг было бы {(93970 - 94630) * 1.6359 - 154.6:.1f})")

# ---- добор: средняя цена в журнале округлена (сделка 41: 4 092 вместо 4 132) ----
ops = [op(T, "SELL", 91440.0, "fGK", 80, 1143.0), op(T + 60, "SELL", 44800.0, "fGK", 40, 1120.0),
       op(T + 120, "BUY", -28574.0, "fGK", 26, 1099.0), op(T + 180, "BUY", -103400.0, "fGK", 94, 1100.0), op(T + 180, "BROKER_FEE", -134.1, "fGK")]
ex = make(ops)
r = ex.closed_pnl("GKZ6", start_ms=(T - 60) * 1000, entry=1135.0, side="Sell", end_ms=(T + 400) * 1000)[0]
assert r.get("by_payments") and abs(r["pnl"] - 4131.9) < 1e-6 and r["qty"] == 120, r
print(f"добор: результат по платежам {r['pnl']:.1f}")

# ---- вход вне окна (позиция взята под управление): количества не сходятся -> расчёт по ценам, как раньше ----
r = ex.closed_pnl("GKZ6", start_ms=(T + 100) * 1000, entry=1135.0, side="Sell", end_ms=(T + 400) * 1000)[0]
assert not r.get("by_payments") and abs(r["pnl"] - ((1135 - 1099) * 26 + (1135 - 1100) * 94 - 134.1)) < 1e-6, r
# ---- боевой счёт: по платежам не считаем (у настоящего брокера платежа по фьючерсной сделке нет) ----
r = make(ops, mode="live").closed_pnl("GKZ6", start_ms=(T - 60) * 1000, entry=1135.0, side="Sell", end_ms=(T + 400) * 1000)[0]
assert not r.get("by_payments")
print("вход вне окна и боевой счёт: расчёт по ценам сохранён")


# ---- плата за маржу делится между шортами акций, открытыми в момент списания ----
class J:
    def __init__(self, rows):
        self.rows = rows

    def _run(self, sql, params=(), fetch=None):
        return [r for r in self.rows if r["opened_ts"] <= params[0]]


class ExM:
    def __init__(self, fees):
        self.fees = fees

    def instruments(self):
        return {"MTLR": {"kind": "share"}, "RUAL": {"kind": "share"}, "GKZ6": {"kind": "future"}}

    def margin_fees(self, a, b):
        return [(ts, amt) for ts, amt in self.fees if a <= ts <= b]


rows = [{"id": 1, "symbol": "MTLR", "qty": 1000.0, "qty_init": 1000.0, "entry": 40.0, "mult": 1.0, "opened_ts": T, "closed_ts": 0},
        {"id": 2, "symbol": "RUAL", "qty": 1000.0, "qty_init": 1000.0, "entry": 120.0, "mult": 1.0, "opened_ts": T + 100, "closed_ts": T + 5000},
        {"id": 3, "symbol": "GKZ6", "qty": 10.0, "qty_init": 10.0, "entry": 1100.0, "mult": 1.0, "opened_ts": T, "closed_ts": 0}]
exe = Executor.__new__(Executor)
exe.ex, exe.j = ExM([(T + 50, 40.0), (T + 1000, 80.0), (T + 9000, 30.0)]), J(rows)
t1 = dict(rows[0], side="Sell")
t2 = dict(rows[1], side="Sell")
# списание 40: открыт только Мечел; 80: Мечел 40 000 и Русал 120 000 -> 20 и 60; 30: снова только Мечел
assert exe._margin_fee_share(t1, now=T + 10000) == 40.0 + 20.0 + 30.0, exe._margin_fee_share(t1, now=T + 10000)
assert exe._margin_fee_share(t2, now=T + 5000) == 60.0
assert exe._margin_fee_share(dict(rows[2], side="Sell"), now=T + 10000) == 0.0      # фьючерс: платы за маржу нет
assert exe._margin_fee_share(dict(t1, side="Buy"), now=T + 10000) == 0.0            # лонг: тоже
print("плата за маржу: 90 на Мечел, 60 на Русал, фьючерсу и лонгу ничего")

# ---- отчёт: итог по брокеру ----
fmt.CUR = "RUB"
m = {"tradable": 301000.0, "reserve": 0.0, "drawdown_pct": 0.0, "day_pct": 0.0, "week_pct": 0.2, "peak": 301000.0, "halted": False, "halt_reason": ""}
stats = {"n": 42, "win_rate": 45.2, "pnl": 2286.7, "fees": 2392.48, "avg_r": -0.01, "profit_factor": 1.1,
         "broker_pnl": 191.9, "broker_fees": 2882.2, "margin_fees": 175.0}
txt = fmt.balance_message(301000.0, m, [], stats, 300000.0, None, None, None)
assert "по операциям брокера" in txt and "191.9" in txt.replace(",", ".") and "плата за маржу" in txt and "В журнале сделок" in txt, txt
old = dict(stats)
for k in ("broker_pnl", "broker_fees", "margin_fees"):
    old.pop(k)
txt2 = fmt.balance_message(301000.0, m, [], old, 300000.0, None, None, None)
assert "с учётом комиссий" in txt2 and "по операциям брокера" not in txt2
print("ВСЁ ОК: результат по платежам, плата за маржу, итог по брокеру")
