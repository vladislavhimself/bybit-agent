"""Случай Циана 06.11 без сети: пустой стакан при входе по рынку и тикеры из ответа модели.
Запуск из корня проекта: python tests/test_open_alias.py"""
import sys
import types

import yaml

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")
import bot.executor as executor_mod  # noqa: E402
from bot.analyst import normalize_tickers  # noqa: E402
from bot.executor import Executor  # noqa: E402
from bot.journal import Journal  # noqa: E402

executor_mod.time.sleep = lambda s: None


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
    """Бумага на планке: рыночная заявка отклоняется «Order book is empty», лимитная принимается."""
    def __init__(self, market_error):
        self.market_error, self.market_calls, self.limits = market_error, 0, []
    def instruments(self): return {"CNRU": {"base": "CNRU", "mult": 1.0, "min_qty": 1, "tick": 0.2, "lot": 1, "kind": "share"}}
    def trading_open(self, sym): return True
    def set_leverage(self, sym, lev): pass
    def place_market(self, sym, side, qty, sl=None, **k):
        self.market_calls += 1
        raise RuntimeError(self.market_error)
    def place_limit(self, sym, side, qty, price, reduce_only=True):
        self.limits.append((side, qty, round(price, 2)))
        return "lim1"


class T:
    def __init__(self): self.msgs = []
    def send(self, text, html=False): self.msgs.append(text)


def setup(err):
    ex, j, tg = Ex(err), Journal(":memory:"), T()
    e = Executor(S({"risk": {"stop_atr_mult": 2.0, "risk_per_trade_pct": {"low": 0.4}}, "entry": {"mode": "market", "pullback_atr_news": 0.15, "wait_minutes": 60}}),
                 ex, types.SimpleNamespace(size=lambda *a, **k: (27.0, 1), tradable=lambda eq: 295000.0), j, tg)
    sig = types.SimpleNamespace(symbol="CNRU", direction="long", strength=0.82, tier="low", components={"total": 0.82}, reasons=[], regime="high_vol",
                                news_driven=True, events=[], event_key="k", force_market=False)
    ta = {"price_now": 922.4, "close": 922.4, "atr": 21.0, "pull_15m": 0.0}
    return ex, j, tg, e, sig, ta


ex, j, tg, e, sig, ta = setup('OrdersService.PostOrder -> 400: {"code":3,"message":"Post order error: Order book is empty","description":"30049"}')
res = e.open(sig, ta, 295000.0, [])
p = j.pendings()
print("пустой стакан: рыночных попыток", ex.market_calls, "| лимитная заявка", ex.limits, "| отложенных в журнале", len(p))
assert res == -1 and ex.market_calls == 1 and ex.limits == [("Buy", 27.0, round(922.4 - 0.15 * 21.0, 2))] and len(p) == 1
assert abs(p[0]["stop"] - (922.4 - 0.15 * 21.0 - 42.0)) < 1e-6
assert not any("Не удалось" in m for m in tg.msgs) and any("ОРДЕР НА ОТКАТ" in m for m in tg.msgs), tg.msgs
assert any("стакан пуст" in r for r in sig.reasons)

ex, j, tg, e, sig, ta = setup("OrdersService.PostOrder -> 400: some other error")
assert e.open(sig, ta, 295000.0, []) is None and ex.limits == [] and any("Не удалось открыть" in m for m in tg.msgs)
print("другая ошибка брокера: как раньше — сообщение владельцу, заявок нет")

# ---- тикеры из ответа модели ----
alias = yaml.safe_load(open("config.yaml", encoding="utf-8"))["markets"]["moex"]["llm"]["ticker_alias"]
assert normalize_tickers(["cian", "T"], alias) == ["CNRU", "T"]
assert normalize_tickers(["#CNRU", "CIAN", "$T", " "], alias) == ["CNRU", "T"], "без повторов и служебных знаков"
assert normalize_tickers(["YNDX", "TCSG", "FIVE", "SBER"], alias) == ["YDEX", "T", "X5", "SBER"]
assert normalize_tickers(["SBER"], None) == ["SBER"]
bad = [k for k, v in alias.items() if str(k).upper() == str(v).upper() and k != "NLMK"]
print("тикеры: CIAN -> CNRU, YNDX -> YDEX, TCSG -> T, FIVE -> X5; повторы убираются")
print("ВСЁ ОК")
