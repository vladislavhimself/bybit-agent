"""Календарь событий по компаниям без сети: даты из текста, тени «на ожидании», ворота «уже выросла перед новостью».
Запуск из корня проекта: python tests/test_corpcal.py"""
import sys
import time
import types
from datetime import date, datetime, timedelta

import pandas as pd

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")
from bot.corpcal import MSK, CorpCalendar, extract  # noqa: E402
from bot.journal import Journal  # noqa: E402
from bot.main import Bot  # noqa: E402

BASES = {"X5", "SBER", "GAZP", "LKOH", "T", "MGNT", "YDEX"}

# ---------- 1. даты из текста по правилам ----------
t = date(2026, 10, 22)
assert extract("🇷🇺 #X5 #дивиденд 30 ноября - X5 - ВОСА по дивидендам 9м 2026г", BASES, t) == [("X5", date(2026, 11, 30), "dividend")]
assert extract("X5 опубликует отчёт по МСФО за 3 квартал 29 октября", BASES, t) == [("X5", date(2026, 10, 29), "report")]
assert extract("#SBER Совет директоров Сбера рассмотрит вопрос 12.11.2026", BASES, t) == [("SBER", date(2026, 11, 12), "board")]
assert extract("#X5 Последний день покупки под дивиденд: 9 декабря", BASES, t) == [("X5", date(2026, 12, 9), "cutoff")]
# факт, а не ожидание: дата сегодня или в прошлом — не событие календаря
assert extract("#X5 Совет директоров X5 6 ноября рекомендовал дивиденды 204 руб.", BASES, date(2026, 11, 6)) == []
# дата без слов про собрание/совет/дивиденды/отчёт — не берём; цена «204.00» датой не считается
assert extract("#X5 X5 откроет 30 ноября новый магазин, дивиденд 204.00 руб.", BASES, t) == [], extract("#X5 X5 откроет 30 ноября новый магазин, дивиденд 204.00 руб.", BASES, t)
# подборка с тремя тикерами — не берём
assert extract("#SBER #GAZP #LKOH отчёты выйдут 29 октября", BASES, t) == []
# раскрытие: тикер известен заранее
assert extract("Повестка дня заседания совета директоров. Дата проведения заседания: 12.11.2026. О рекомендациях по размеру дивидендов",
               BASES, t, base="MGNT") == [("MGNT", date(2026, 11, 12), "dividend")]
# год не указан и дата уже прошла в этом году -> следующий год, но дальше 90 дней не берём
assert extract("#X5 ГОСА по дивидендам 15 января", BASES, date(2026, 12, 20)) == [("X5", date(2027, 1, 15), "dividend")]
assert extract("#X5 ГОСА по дивидендам 15 июня", BASES, date(2026, 12, 20)) == []
print("даты из текста: назначенное собрание, отчёт, совет директоров, отсечка; факты и подборки не берутся")


# ---------- 2. календарь и тени «на ожидании» ----------
class Ex:
    def __init__(self):
        self.px = {"X5": 2300.0, "IMOEXF": 2500.0, "SBER": 300.0}
        self.up = {"X5": 1.0, "SBER": -1.0, "IMOEXF": 0.0}       # дневной ход, % в день: X5 сильнее рынка, Сбер слабее
    def instruments(self): return {b: {"base": b, "kind": "share"} for b in BASES}
    def last_price(self, sym): return self.px[sym]
    def klines(self, sym, iv, n):
        last, k = self.px[sym], self.up[sym] / 100
        closes = [last / (1 + k) ** (n - 1 - i) for i in range(n)]
        return pd.DataFrame({"ts": range(n), "close": closes, "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes]})


class S:
    def get(self, k, d=None): return {"corpcal": {"enabled": True, "pre_event_shadow": True, "pre_event_min_days": 3, "pre_event_max_days": 20}}.get(k, d)


today = datetime.now(MSK).date()
ex, j = Ex(), Journal(":memory:")
cc = CorpCalendar(S(), j, ex)
d5 = today + timedelta(days=7)
assert cc.add("X5", d5, "dividend", "ВОСА по дивидендам", "tg:test") and not cc.add("X5", d5, "dividend"), "повтор не добавляется"
assert not cc.add("ZZZZ", d5, "dividend") and not cc.add("X5", today - timedelta(days=1), "report"), "неизвестный тикер и прошлая дата не берутся"
assert cc.ingest({"title": f"#SBER Совет директоров Сбера обсудит дивиденды {d5.strftime('%d.%m.%Y')}", "summary": "", "source": "tg:test"}) == 1
assert cc.ingest_model({"future_event_date": (today + timedelta(days=10)).isoformat(), "future_event_kind": "report", "affected_symbols": ["GAZP"]}, "Газпром: отчёт", "rbc") == 1
assert cc.ingest_model({"future_event_date": "", "future_event_kind": "", "affected_symbols": ["GAZP"]}, "x", "rbc") == 0
assert [r["base"] for r in cc.upcoming(21)] == ["SBER", "X5", "GAZP"] and cc.soon("X5", 1) is None and cc.soon("X5", 7)["kind"] == "dividend"
print("календарь:", cc.text(21))
assert "X5: решение по дивидендам" in " ".join(cc.text(21)) and cc.context_text().startswith("Календарь событий")
cc.tick()
rows = j._run("SELECT * FROM pre_event", (), "all")
print("тени «на ожидании»:", [(r["base"], r["entry"], round(r["stop"], 1), r["status"]) for r in rows])
assert [r["base"] for r in rows] == ["X5"], "входим только в бумагу сильнее рынка (Сбер слабее, Газпром — нет цены в заглушке)"
# цена упала ниже стопа -> тень закрыта по стопу
ex.px["X5"] = rows[0]["stop"] - 1
cc._tick_ts = 0
cc.tick()
r = j._run("SELECT * FROM pre_event", (), "one")
assert r["status"] == "closed" and r["why"] == "стоп" and r["pct"] < 0
# событие сегодня: торговый день перед ним уже прошёл -> выход «за день до события»
ev_id = j._run("INSERT INTO corp_events(base,date,kind,title,source,created_ts) VALUES('MGNT',?, 'report','','t',0)", (today.isoformat(),))
j._run("INSERT INTO pre_event(event_id,base,event_date,kind,entry_ts,entry,stop,idx_entry,status) VALUES(?,?,?,?,?,?,?,?,'open')",
       (ev_id, "X5", today.isoformat(), "report", int(time.time()) - 5 * 86400, 2200.0, 2100.0, 2450.0))
ex.px["X5"] = 2310.0
cc._tick_ts = 0
cc.tick()
r = j._run("SELECT * FROM pre_event WHERE event_id=?", (ev_id,), "one")
assert r["status"] == "closed" and r["why"] == "выход за день до события" and abs(r["pct"] - 5.0) < 1e-6 and abs(r["exc_pct"] - (5.0 - (2500 / 2450 - 1) * 100)) < 1e-6
print("тени: вход при силе бумаги, выход по стопу и за день до события;", cc.shadow_text()[0])

# ---------- 3. ворота «уже выросла перед новостью» ----------
now = time.time()
b = types.SimpleNamespace(s=types.SimpleNamespace(get=lambda k, d=None: {"signals": {"news_entry_min_importance": 7, "news_entry_max_age_min": 15},
                                                                        "psychology": {"runup_days": 10, "runup_excess_pct": 4.0}}.get(k, d)), paper_mode=False)
ev = lambda imp: {"importance": imp, "confidence": 0.9, "ts": now - 60, "direction": "bullish"}  # noqa: E731
sig = lambda events, direction="long": types.SimpleNamespace(symbol="X5", news_driven=True, events=events, direction=direction, tier="medium", reasons=[])  # noqa: E731
b._runup_excess = lambda sym, n=10: 4.7
assert Bot._gate(b, sig([ev(7)]), {}) == "priced_in"
assert Bot._gate(b, sig([ev(9)]), {}) is None, "важность 9+ — новое событие, а не ожидавшийся факт"
b._runup_excess = lambda sym, n=10: 2.0
assert Bot._gate(b, sig([ev(7)]), {}) is None
b._runup_excess = lambda sym, n=10: None
assert Bot._gate(b, sig([ev(7)]), {}) is None, "нет данных — не блокируем"

# расчёт опережения: сегодняшний день не считается
day_ms = 86400 * 1000
t0 = int(time.time()) // 86400 * 86400 * 1000


def kl(sym, iv, limit, age):
    grow = {"X5": 1.005, "IMOEXF": 1.0}[sym]
    closes = [100 * grow ** i for i in range(30)] + [500.0]          # последний бар — сегодня, с выбросом на новости
    return pd.DataFrame({"ts": [t0 - (30 - i) * day_ms for i in range(31)], "close": closes})


b2 = types.SimpleNamespace(s=types.SimpleNamespace(get=lambda k, d=None: d), _klines_cached=kl)
exc = Bot._runup_excess(b2, "X5", 10)
assert abs(exc - (1.005 ** 10 - 1) * 100) < 1e-6, exc
print(f"ворота: бумага обогнала рынок на 4.7% — на новости не покупаем; расчёт опережения без сегодняшнего дня: {exc:.2f}%")
print("ВСЁ ОК")
