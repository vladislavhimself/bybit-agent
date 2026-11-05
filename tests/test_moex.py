"""Модуль брокера без сети: порядок перестановки стоп-заявки и виртуальное обеспечение песочницы."""
import sys
from decimal import Decimal
from pathlib import Path
import tempfile

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")
from bot.moex import MoexExchange, f2q  # noqa: E402


class S:
    def __init__(self, d): self.d = d
    def get(self, k, d=None):
        cur = self.d
        for part in k.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return d
            cur = cur[part]
        return cur


def make(stops, fail_post=0, fail_list=False):
    ex = MoexExchange.__new__(MoexExchange)
    ex.s, ex.account, ex.mode, ex._buffer = S({}), "acc", "demo", 0.0
    ex.calls = []
    ex._stops = [dict(s) for s in stops]
    ex.instruments = lambda: {"GAZPF": {"uid": "u1", "figi": "f1", "tick": Decimal("0.01"), "mult": 100.0, "lot": 1, "kind": "future"}}
    ex.positions = lambda: [{"symbol": "GAZPF", "side": "Sell", "qty": 3.0}]
    state = {"fail_post": fail_post}

    def call(svc, method, body=None, retries=8):
        ex.calls.append(method)
        if method == "GetStopOrders":
            if fail_list:
                raise RuntimeError("relay timeout")
            return {"stopOrders": list(ex._stops)}
        if method == "PostStopOrder":
            if state["fail_post"] > 0:
                state["fail_post"] -= 1
                raise RuntimeError("30052 rejected")
            ex._stops.append({"stopOrderId": f"new{len(ex.calls)}", "instrumentUid": "u1", "stopPrice": body["stopPrice"], "lotsRequested": body["quantity"]})
            return {}
        if method == "CancelStopOrder":
            ex._stops = [s for s in ex._stops if s["stopOrderId"] != body["stopOrderId"]]
            return {}
        raise AssertionError(method)

    ex.call = call
    return ex


OLD = [{"stopOrderId": "old", "instrumentUid": "u1", "stopPrice": f2q(111.28), "lotsRequested": "4"}]
ex = make(OLD)
ex.set_stop_loss("GAZPF", 110.5)
assert ex.calls == ["GetStopOrders", "PostStopOrder", "CancelStopOrder"], ex.calls
assert len(ex._stops) == 1 and ex._stops[0]["lotsRequested"] == "3" and ex._stops[0]["stopOrderId"] != "old"
print("обычный случай: список, новая заявка на 3, потом снятие старой")

ex = make(OLD, fail_list=True)
try:
    ex.set_stop_loss("GAZPF", 110.5); raise SystemExit("должно быть исключение")
except RuntimeError:
    pass
assert ex.calls == ["GetStopOrders"] and len(ex._stops) == 1 and ex._stops[0]["stopOrderId"] == "old"
print("список стоп-заявок не получен: ничего не изменено, старая на месте")

ex = make(OLD, fail_post=1)      # брокер не принял вторую заявку рядом с первой: меняем через снятие
ex.set_stop_loss("GAZPF", 110.5)
assert ex.calls == ["GetStopOrders", "PostStopOrder", "CancelStopOrder", "PostStopOrder"] and len(ex._stops) == 1
assert abs(float(ex._stops[0]["stopPrice"]["units"]) + ex._stops[0]["stopPrice"]["nano"] / 1e9 - 110.5) < 1e-9
print("вторая заявка не принята: снятие старой и новая заявка")

ex = make(OLD, fail_post=2)      # новая не принимается вообще: возвращаем прежний уровень
try:
    ex.set_stop_loss("GAZPF", 110.5); raise SystemExit("должно быть исключение")
except RuntimeError:
    pass
assert len(ex._stops) == 1 and abs(float(ex._stops[0]["stopPrice"]["units"]) + ex._stops[0]["stopPrice"]["nano"] / 1e9 - 111.28) < 1e-9
print("новый уровень не принят совсем: стоп-заявка восстановлена на прежнем уровне 111.28")

ex = make([], fail_post=1)
try:
    ex.set_stop_loss("GAZPF", 110.5); raise SystemExit("должно быть исключение")
except RuntimeError:
    pass
print("стопа не было и новый не принят: исключение (вызывающий повторит)")

# ---- виртуальное обеспечение песочницы: в капитал не входит ----
tmp = Path(tempfile.mkdtemp())
ex = MoexExchange.__new__(MoexExchange)
ex.s, ex.account, ex.mode = S({"moex": {"sandbox_buffer_rub": 200000, "sandbox_start_rub": 100000}}), "acc", "demo"
ex._buffer_file = tmp / "moex_buffer_demo.txt"
ex._buffer = ex._buffer_paid()
ex._figi2sym, ex._uid2sym = {}, {}
ex.instruments = lambda: {}
cash = {"v": 95705.0}
payins = []


def call2(svc, method, body=None, retries=8):
    if method == "GetSandboxAccounts":
        return {"accounts": [{"id": "acc", "status": "ACCOUNT_STATUS_OPEN"}]}
    if method == "GetPortfolio":
        return {"totalAmountPortfolio": {"units": str(int(cash["v"])), "nano": 0},
                "positions": [{"instrumentType": "currency", "quantity": {"units": str(int(cash["v"])), "nano": 0}}]}
    if method == "SandboxPayIn":
        payins.append(int(body["amount"]["units"])); cash["v"] += int(body["amount"]["units"]); return {}
    raise AssertionError(method)


ex.call = call2
before = ex.balances()
ex._ensure_account_online()
after = ex.balances()
print(f"до пополнения: капитал {before['equity']:.0f}, свободно {before['available']:.0f}; после: капитал {after['equity']:.0f}, свободно {after['available']:.0f}, обеспечение {after['buffer']:.0f}")
assert payins == [200000] and after["equity"] == before["equity"] == 95705.0 and after["wallet"] == before["wallet"]
assert after["available"] == 295705.0 and ex._buffer_file.read_text() == "200000"
ex._ensure_account_online()                      # повторный запуск: второй раз не пополняет
assert payins == [200000]
ex2 = MoexExchange.__new__(MoexExchange)
ex2.s, ex2.account, ex2.mode, ex2._buffer_file = ex.s, "acc", "demo", ex._buffer_file
ex2._buffer = ex2._buffer_paid(); ex2._figi2sym, ex2._uid2sym = {}, {}; ex2.instruments = lambda: {}; ex2.call = call2
assert ex2.balances()["equity"] == 95705.0       # после перезапуска сумма читается из файла
print("капитал и прибыль не изменились ни на рубль; повторного пополнения нет; после перезапуска то же")
print("ВСЁ ОК")
