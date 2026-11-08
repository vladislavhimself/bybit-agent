"""Сделки на расхождении (A: ЦБ по облигациям, B: отчёт хуже ожиданий) и выходы по причине; ценовые тени.
Запуск из корня: python tests/test_eventtrades.py"""
import os
import sys
import tempfile
import time

import pandas as pd

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")
from bot.eventtrades import EventTrades  # noqa: E402
from bot.expect_rules import ExpectRules  # noqa: E402
from bot.journal import Journal  # noqa: E402
from bot.pricesignals import PriceSignals, detect  # noqa: E402


class S:
    def __init__(self, d):
        self.d = d

    def get(self, k, default=None):
        return self.d.get(k, default)


class Ex:
    def __init__(self):
        self.px = {"IMOEXF": 2500.0, "PLZL": 1000.0}

    def instruments(self):
        return {"IMOEXF": {"kind": "future", "base": "IMOEX", "short_ok": True, "mult": 1.0},
                "PLZL": {"kind": "share", "base": "PLZL", "short_ok": True, "mult": 1.0},
                "MGNT": {"kind": "share", "base": "MGNT", "short_ok": False, "mult": 1.0}}

    def last_price(self, sym):
        return self.px[sym]

    def news_pick(self, base):
        return {"PLZL": "PLZL", "MGNT": "MGNT"}.get(base)

    def trading_open(self, sym):
        return True


class Exe:
    def __init__(self):
        self.opened, self.closed = [], []
        self.event_manager = None

    def open(self, sig, ta, equity, positions, risk_cap=None):
        self.opened.append((sig.symbol, sig.direction, sig.tier, round(sig.stop_dist, 2), risk_cap, sig.event_key))
        return len(self.opened)

    def _close_now(self, t, reason, defer=False):
        self.closed.append((t["event_key"], reason))
        return True


class Risk:
    last_reject = ""

    def base_of(self, s):
        return {"IMOEXF": "IMOEX"}.get(s, s)

    def can_open(self, *a, **k):
        return True

    def tradable(self, equity):
        return 300000.0


class Tg:
    def __init__(self):
        self.msgs = []

    def send(self, text, html=False):
        self.msgs.append(text)


class Bot:
    def __init__(self):
        self.s = S({"events": {"symbols": ["IMOEXF"], "cbr_wait_hours": 4, "cbr_rgbi_min_pct": 0.15, "earn_confirm_min": 60, "earn_max_up_pct": 0.2,
                               "trail_pct": 3.0, "trail_min_pct": 1.0, "stop_pct": 5.0, "max_days": 14, "risk_cap_pct": 0.35},
                    "moex.cbr_current_rate": 14.0})
        self.j = Journal(os.path.join(tempfile.mkdtemp(), "t.db"))
        self.ex, self.exe, self.risk, self.tg = Ex(), Exe(), Risk(), Tg()
        self.expect_rules = ExpectRules(self.s, self.j)
        self.eventplay = self.divcal = self.corpcal = None
        self._shock_until = 0.0

    def market_data(self, sym, max_age=0):
        return {"atr": 10.0, "close": self.ex.px[sym], "price_now": self.ex.px[sym], "regime": "range"}, {}, []


# ---- A: решение ЦБ, подтверждение облигациями ----
b = Bot()
et = EventTrades(b)
b.exe.event_manager = et
et.rgbi_at = lambda ts: 100.0
rg = {"v": 99.8}
et.rgbi_last = lambda now=None: rg["v"]
t0 = time.time() - 5 * 3600
sid = b.expect_rules.record("cbr", "CBR", 14.0, 14.0, 0.0, 2, 10.0, "n", "ЦБ сохранил 14%")
et.arm_cbr("2026-12-18", t0, 14.0, 14.0, sid)
et.tick(t0 + 3600, 300000, [])                      # рано: ждём 4 часа
assert not b.exe.opened
et.tick(time.time(), 300000, [])                    # RGBI −0.2% → шорт индекса
assert b.exe.opened and b.exe.opened[0][:3] == ("IMOEXF", "short", "low"), b.exe.opened
assert b.exe.opened[0][3] == 125.0 and abs(b.exe.opened[0][4] - 1050.0) < 1e-6, b.exe.opened   # стоп 5%, риск 0.35% от 300 000
row = b.j._run("SELECT * FROM surprises WHERE id=?", (sid,), "one")
assert row["confirmed"] == 1 and row["decision"].startswith("short"), row
assert any("ШОРТ ИНДЕКСА" in m for m in b.tg.msgs)

# сопровождение: трейлинг и тезис
key = b.exe.opened[0][5]
t = {"id": 1, "symbol": "IMOEXF", "side": "Sell", "entry": 2500.0, "entry_init": 2500.0, "stop_init": 2625.0, "opened_ts": time.time() - 86400, "event_key": key, "mfe_r": 0}
assert et.manage(t, 2450.0, {}) is False            # +2% в нашу сторону: держим
assert et.manage(t, 2460.0, {}) is False            # откат 0.4%: держим
assert et.manage(t, 2530.0, {}) is True and "откат" in b.exe.closed[-1][1], b.exe.closed   # откат 3.2% от лучшей: выход
rg["v"] = 100.2
t2 = dict(t, event_key=key)
et.state["meta"][key]["best"] = 0.0
assert et.manage(t2, 2495.0, {}) is True and "тезис" in b.exe.closed[-1][1], b.exe.closed   # облигации вернулись: выход
t3 = dict(t, opened_ts=time.time() - 15 * 86400)
rg["v"] = 99.0
et.state["meta"][key]["best"] = 0.0
assert et.manage(t3, 2499.0, {}) is True and "предохранитель" in b.exe.closed[-1][1]
assert et.manage(dict(t, opened_ts=time.time()), 2640.0, {}) is True and "аварийный стоп" in b.exe.closed[-1][1]

# облигации не сдвинулись → сделки нет
b2 = Bot()
et2 = EventTrades(b2)
et2.rgbi_at = lambda ts: 100.0
et2.rgbi_last = lambda now=None: 100.05
et2.arm_cbr("2026-12-18", time.time() - 5 * 3600, 14.0, 14.0, None)
et2.tick(time.time(), 300000, [])
assert not b2.exe.opened and any("СДЕЛКИ НЕТ" in m for m in b2.tg.msgs)

# ---- B: отчёт хуже ожиданий, проверка через час ----
b3 = Bot()
et3 = EventTrades(b3)
ts = time.time() - 3600 - 5
info = b3.expect_rules.on_event({"event_type": "earnings", "affected_symbols": ["PLZL"]}, "«Полюс» отчитался: результаты хуже ожиданий аналитиков", "e1", ts, "tg:x")
et3.on_surprise(info, "Полюс хуже ожиданий", ts, "e1")
assert et3.state["earn"] and et3.state["earn"][0]["pre"] == 1000.0
b3.ex.px["PLZL"] = 998.0                               # не выросла → шорт
et3.tick(time.time(), 300000, [])
assert b3.exe.opened and b3.exe.opened[0][1] == "short" and b3.exe.opened[0][0] == "PLZL", b3.exe.opened
# бумага выросла после отчёта → сделки нет
b4 = Bot()
et4 = EventTrades(b4)
info = b4.expect_rules.on_event({"event_type": "earnings", "affected_symbols": ["PLZL"]}, "Полюс: результаты хуже ожиданий", "e2", ts, "tg:x")
et4.on_surprise(info, "x", ts, "e2")
b4.ex.px["PLZL"] = 1012.0
et4.tick(time.time(), 300000, [])
assert not b4.exe.opened and any("СДЕЛКИ НЕТ" in m for m in b4.tg.msgs)
# бумага без шорта у брокера → кандидата нет; «лучше ожиданий» → кандидата нет
info = b4.expect_rules.on_event({"event_type": "earnings", "affected_symbols": ["MGNT"]}, "Магнит отчитался хуже ожиданий", "e3", ts, "tg:x")
et4.on_surprise(info, "x", ts, "e3")
assert not any(x["base"] == "MGNT" for x in et4.state["earn"])
info = b4.expect_rules.on_event({"event_type": "earnings", "affected_symbols": ["PLZL"]}, "Полюс: результаты лучше ожиданий", "e4", ts, "tg:x")
et4.on_surprise(info, "x", ts, "e4")
assert len([x for x in et4.state["earn"] if x["base"] == "PLZL"]) == 1
# тезис B: цена снова выше уровня до новости
key = b3.exe.opened[0][5]
tb = {"id": 2, "symbol": "PLZL", "side": "Sell", "entry": 998.0, "entry_init": 998.0, "stop_init": 1047.9, "opened_ts": time.time() - 7200, "event_key": key, "mfe_r": 0}
assert et3.manage(tb, 990.0, {}) is False
assert et3.manage(tb, 1001.0, {}) is True and "тезис" in b3.exe.closed[-1][1]

# ---- ценовые тени по дневным свечам ----
days = 30
base = [100 + i * 0.1 for i in range(days)]
df = pd.DataFrame({"ts": [(1700000000 + i * 86400) * 1000 for i in range(days)], "open": base, "high": [x + 1 for x in base], "low": [x - 1 for x in base],
                   "close": base, "volume": [1000.0] * days})
assert detect(df) == []
df2 = df.copy()
df2.loc[days - 1, "close"] = 95.0
df2.loc[days - 1, "volume"] = 2000.0
assert detect(df2) == ["breakdown"], detect(df2)
df3 = df.copy()
df3.loc[days - 1, "close"] = 110.0
df3.loc[days - 1, "high"] = 110.5
df3.loc[days - 1, "volume"] = 1600.0
assert detect(df3) == ["breakout"], detect(df3)
df4 = df.copy()
df4.loc[days - 1, "volume"] = 900.0
df4.loc[days - 1, "close"] = 95.0   # без объёма пробой не считается
idx = pd.DataFrame({"close": [1000.0] * days})
assert "breakdown" not in detect(df4, idx)
df5 = df.copy()
for i in range(days - 5, days):
    df5.loc[i, "close"] = 86.0      # −14% за 5 дней при индексе на месте
assert "reversal12" in detect(df5, idx), detect(df5, idx)


class ShadowFake:
    def __init__(self):
        self.rec = []

    def record(self, symbol, side, reason, strength, threshold, price, atr, **kw):
        self.rec.append((symbol, side, reason, round(atr * 2.0 / price * 100, 2)))
        return len(self.rec)


class ExD:
    def __init__(self, frames):
        self.frames = frames

    def instruments(self):
        return {"PLZL": {"kind": "share", "short_ok": True}, "MGNT": {"kind": "share", "short_ok": False}, "IMOEXF": {"kind": "future", "short_ok": True}}

    def klines(self, sym, interval, limit, end=None):
        return self.frames.get(sym)


class J2:
    def __init__(self):
        self.kv = {}

    def kv_get(self, k, d=None):
        return self.kv.get(k, d)

    def kv_set(self, k, v):
        self.kv[k] = v


from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

today_ts = int(datetime.now(ZoneInfo("Europe/Moscow")).replace(hour=12, minute=0, second=0, microsecond=0).timestamp())
for f in (df2, df5):
    f["ts"] = [(today_ts - (days - 1 - i) * 86400) * 1000 for i in range(days)]
idx_df = pd.DataFrame({"ts": df2["ts"], "close": [1000.0] * days})
sh = ShadowFake()
ps = PriceSignals(S({"pricesignals": {"enabled": True, "hour_msk": 19, "stop_pct": 5.0}, "risk.stop_atr_mult": 2.0}), ExD({"PLZL": df2, "MGNT": df2, "IMOEXF": idx_df}), sh, J2())
found = ps.scan(["PLZL", "MGNT"], time.time())
assert found == [("PLZL", "breakdown")], found          # MGNT без шорта у брокера пропущен
assert sh.rec[0][1] == "Sell" and sh.rec[0][3] == 5.0, sh.rec  # стоп 5% через atr·stop_atr_mult
assert not ps.due(time.time())                            # за день второй раз не сканируем
print("ВСЁ ОК: сделки A/B, выходы по причине, ценовые тени")
