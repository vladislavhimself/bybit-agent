"""Капитал песочницы считается по сделкам с фьючерсами, а не по вармарже клиринга (случай 20.11.2026: −12 746 руб. на ровном месте).
Запуск из корня: python tests/test_sandbox_equity.py"""
import calendar
import sys
import time

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")
from bot.moex import MoexExchange, iso  # noqa: E402


def q(v):
    u = int(v)
    return {"units": str(u), "nano": int(round((v - u) * 1e9))}


class Broker:
    """Песочница: деньги меняются только пополнением, комиссией и вармаржой клиринга; платежи по фьючерсам в деньги не идут."""

    def __init__(self):
        self.cash, self.ops, self.pos, self.price = 300000.0, [], {}, {}
        self.t = time.time() - 3 * 3600
        self.ops_visible = True
        self.n = 0

    def op(self, typ, pay, figi="", qty=0, kind=""):
        self.n += 1
        self.t += 60
        self.ops.append({"id": str(self.n), "date": iso(self.t), "operationType": "OPERATION_TYPE_" + typ, "payment": q(pay), "figi": figi,
                         "quantity": str(qty), "instrumentType": kind})

    def trade(self, figi, side, qty, price, mult=1.0):
        sign = -1 if side == "SELL" else 1
        self.op(side, -sign * qty * price * mult, figi, qty, "futures")
        self.cash -= 10.0
        self.op("BROKER_FEE", -10.0, figi)
        old_qty, old_avg = self.pos.get(figi, (0.0, 0.0))
        new_qty = old_qty + sign * qty
        avg = price if old_qty == 0 or (old_qty > 0) != (new_qty > 0) else (old_avg if abs(new_qty) < abs(old_qty) else (old_avg * abs(old_qty) + price * qty) / abs(new_qty))
        self.pos[figi] = (new_qty, avg)
        self.price[figi] = price

    def clearing(self, amount):
        self.cash += amount
        self.op("WRITING_OFF_VARMARGIN" if amount < 0 else "ACCRUING_VARMARGIN", amount)

    def call(self, svc, method, body=None, retries=8):
        if method == "GetPortfolio":
            pos = [{"instrumentType": "currency", "quantity": q(self.cash)}]
            for figi, (qty, avg) in self.pos.items():
                if abs(qty) > 0:
                    pos.append({"instrumentType": "futures", "figi": figi, "quantity": {"units": str(int(qty)), "nano": 0}, "averagePositionPrice": q(avg),
                                "currentPrice": q(self.price[figi]), "varMargin": q(0.0)})
            return {"positions": pos}
        if method == "GetOperations":
            return {"operations": list(self.ops) if self.ops_visible else list(self._old_ops)}
        raise AssertionError(method)


def make(br):
    ex = MoexExchange.__new__(MoexExchange)
    ex.s, ex.account, ex.mode = None, "acc", "demo"
    ex._figi2sym, ex._uid2sym = {"fS": "SSZ6", "fG": "GKZ6"}, {}
    ex.instruments = lambda: {"SSZ6": {"mult": 1.0}, "GKZ6": {"mult": 1.0}}
    ex.call = br.call
    return ex


# ---- сценарий 19–20.11: шорт вечером, клиринг списал, днём закрыли до клиринга ----
br = Broker()
ex = make(br)
assert abs(ex.balances()["equity"] - 300000) < 1e-6
br.trade("fS", "SELL", 164, 288.0)
br.trade("fG", "SELL", 80, 1143.0)
ex._ledger["ts"] = 0   # прошла минута
b = ex.balances()
assert abs(b["equity"] - (300000 - 20)) < 1e-6, b          # вошли: капитал = деньги минус комиссии
br.clearing(-4462.0)                                          # песочница списала вармаржу по часовым позициям
ex._ledger["ts"] = 0
b = ex.balances()
assert abs(b["equity"] - (300000 - 20)) < 1e-6, b           # списание клиринга капитал не меняет: цены те же
assert abs(b["legacy_equity"] - (300000 - 20 - 4462)) < 1e-6
br.price["fS"], br.price["fG"] = 267.0, 1100.0                # рынок упал
b = ex.balances()
want = 300000 - 20 + 164 * (288 - 267) + 80 * (1143 - 1100)
assert abs(b["equity"] - want) < 1e-6 and abs(b["unrealised"] - (164 * 21 + 80 * 43)) < 1e-6, (b, want)
br.trade("fS", "BUY", 164, 267.0)                             # закрыли до клиринга: песочница ничего не начислит
br.trade("fG", "BUY", 80, 1100.0)
b = ex.balances()                                             # позиции изменились -> хвост операций перечитан сразу, без ожидания минуты
want = 300000 - 40 + 164 * 21 + 80 * 43
assert abs(b["equity"] - want) < 1e-6, (b["equity"], want)
assert abs(b["legacy_equity"] - (300000 - 40 - 4462)) < 1e-6  # старая формула теряла и прибыль, и списание
assert abs(b["unrealised"]) < 1e-9 and abs(b["wallet"] - want) < 1e-6
print(f"шорт закрыт до клиринга: капитал {b['equity']:.0f} (по старой формуле было бы {b['legacy_equity']:.0f})")

# ---- операции отстают от позиций: капитал не прыгает на стоимость контракта ----
br = Broker()
ex = make(br)
ex.balances()
br.trade("fS", "SELL", 100, 300.0)
ex._ledger["ts"] = 0
good = ex.balances()["equity"]
br._old_ops = list(br.ops)
br.ops_visible = False
br.trade("fS", "BUY", 100, 290.0)                             # позиция закрыта, а операция в списке ещё не видна
b = ex.balances()
assert abs(b["equity"] - good) < 1e-6, (b["equity"], good)    # держим последнее согласованное значение
br.ops_visible = True
b = ex.balances()
assert abs(b["equity"] - (300000 - 20 + 100 * 10)) < 1e-6, b
print("операции отстали от позиций: капитал не прыгнул, после синхронизации +1000")

# ---- больше 100 операций в окне: окно делится, ничего не теряется ----
br = Broker()
br.t = time.time() - 3 * 86400     # старые операции: часть уйдёт в «замороженную» сумму, часть останется в хвосте
for i in range(130):
    if i == 100:
        br.t = time.time() - 6 * 3600
    br.trade("fS", "SELL", 1, 300.0)
    br.trade("fS", "BUY", 1, 299.0)
full_call = br.call


def capped(svc, method, body=None, retries=8):
    r = full_call(svc, method, body, retries)
    if method == "GetOperations":
        a = calendar.timegm(time.strptime(body["from"], "%Y-%m-%dT%H:%M:%SZ"))
        b_ = calendar.timegm(time.strptime(body["to"], "%Y-%m-%dT%H:%M:%SZ"))
        sel = [o for o in r["operations"] if a <= MoexExchange._op_ts(o) <= b_]
        return {"operations": sel[-100:]}
    return r


ex = make(br)
ex.call = capped
b = ex.balances()
assert abs(b["equity"] - (300000 - 130 * 20 + 130 * 1.0)) < 1e-6, b["equity"]
assert ex._ledger["f_fut"] != 0 and ex._ledger["frozen_ts"] > MoexExchange.OPS_START_TS
ex._ledger["ts"] = 0
assert abs(ex.balances()["equity"] - b["equity"]) < 1e-6   # повторный расчёт от замороженной суммы даёт то же
print("260 сделок при лимите 100 операций на запрос: посчитаны все")

# ---- боевой счёт: формула прежняя ----
br = Broker()
ex = make(br)
ex.mode = "live"
b = ex.balances()
assert abs(b["equity"] - 300000) < 1e-6 and "legacy_equity" not in b
print("ВСЁ ОК: капитал песочницы по сделкам")
