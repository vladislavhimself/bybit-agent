"""Офлайн-проверка: стоп-лист действует на акцию и её фьючерс; топ-40 по обороту; Сбер и Сбер-п — одна компания."""
import sys
import time

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")
from bot.moex import MoexExchange  # noqa: E402
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


names = [f"S{i:02d}" for i in range(50)] + ["HYDR", "SBER", "SBERP"]
inst = {n: {"kind": "share", "base": n, "expiration": None, "mult": 1} for n in names}
inst["HYZ6"] = {"kind": "future", "base": "HYDR", "expiration": "2027-03-20", "mult": 100}
inst["SBERF"] = {"kind": "future", "base": "SBER", "expiration": None, "mult": 100}
turn = {n: 600e6 + i * 1e6 for i, n in enumerate(names)}
turn.update({"HYZ6": 9e9, "SBERF": 8e9, "S00": 100e6})
cfg = {"universe": {"top_n_by_turnover": 40, "min_turnover_24h_usd": 500e6, "always": ["SBERF"], "blacklist": ["HYDR"]}, "moex": {}}
ex = MoexExchange.__new__(MoexExchange)
ex.s = S(cfg); ex.instruments = lambda: inst; ex._turnover_today = lambda: turn
u = ex.universe()
print("в списке", len(u), "| HYDR:", "HYDR" in u, "| фьючерс HYZ6:", "HYZ6" in u, "| S00 (оборот 100 млн):", "S00" in u, "| SBERF:", "SBERF" in u, "| SBER:", "SBER" in u)
assert len(u) == 40 and "HYDR" not in u and "HYZ6" not in u and "S00" not in u and "SBERF" in u and "SBER" not in u

RISK = {"max_open_positions": 5, "risk_per_trade_pct": {"low": 0.5, "medium": 0.8, "high": 1.2}, "max_direction_risk_pct": 3.5, "max_total_exposure_x": 3,
        "max_same_direction": 4, "max_per_base": 1, "max_per_sector": 2, "sectors": {"banks": ["SBER", "SBERP", "VTBR"]},
        "base_alias": {"SBERP": "SBER"}}


class Ex:
    def instruments(self): return inst


j = Journal(":memory:")
rm = RiskManager(S({"risk": RISK}), Ex(), j)
rm.tradable = lambda eq: 100000.0
held = [{"symbol": "SBERF", "side": "Buy", "entry": 300.0, "stop_init": 295.0, "qty": 1, "qty_init": 1, "event_key": "", "opened_ts": time.time() - 3600}]
ok, why = rm.can_open("SBERP", held, [], 100000, "long", "", "low")
print("Сбер-п при открытом фьючерсе на Сбер:", ok, "|", why)
assert not ok
ok, why = rm.can_open("S10", held, [], 100000, "long", "", "low")
print("другая компания:", ok); assert ok
print("ВСЁ ОК")
