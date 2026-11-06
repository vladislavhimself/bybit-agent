"""Случай X5 06.11 без сети: суть раскрытия и дивиденд по правилу; защита открытой позиции от шока на новости.
Запуск из корня проекта: python tests/test_disclosure_shock.py"""
import sys
import time

import pandas as pd

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")
import bot.executor as executor_mod  # noqa: E402
from bot.disclosure import essence  # noqa: E402
from bot.executor import Executor  # noqa: E402
from bot.journal import Journal  # noqa: E402

executor_mod.time.sleep = lambda s: None

# ---------- 1. раскрытие: суть в начале, дивиденд по правилу ----------
X5 = ("2. Содержание сообщения 2.1. Сведения о кворуме заседания совета директоров (наблюдательного совета) эмитента и результатах голосования "
      "по вопросам о принятии решений: в голосовании приняли участие 9 (девять) из 9 (девяти) членов Наблюдательного совета, кворум имелся. "
      "Результаты голосования по вопросу № 1 повестки дня «О рекомендациях по вопросам повестки дня внеочередного заочного голосования Общего "
      "собрания акционеров Общества»: «ЗА» - 9 голосов, «ПРОТИВ» - нет, «ВОЗДЕРЖАЛСЯ» - нет. Решение принято. 2.2. Содержание решений, принятых "
      "советом директоров (наблюдательным советом) эмитента: По вопросу № 1 повестки дня «О рекомендациях по вопросам повестки дня внеочередного "
      "заочного голосования Общего собрания акционеров Общества». Принятое решение: 1.1. О выплате (объявлении) дивидендов по результатам 9 месяцев "
      "2026 года: Рекомендовать Общему собранию акционеров Общества принять следующее решение: 1. Выплатить (объявить) дивиденды по обыкновенным "
      "акциям ПАО «Корпоративный центр ИКС 5» по результатам 9 месяцев 2026 года в размере 204,00 (двести четыре) рубля 00 копеек на одну "
      "обыкновенную акцию ПАО «Корпоративный центр ИКС 5» в денежной форме. Источником выплаты дивидендов определить чистую прибыль.")
add, txt, div = essence(X5)
print("X5:", add, "| суть в первых 300 символах:", "дивиденд" in txt[:300].lower())
assert div == 204.0 and add == "дивиденды 204 руб. на акцию по итогам 9 месяцев 2026 года", add
assert txt.startswith("Суть: ") and "дивиденд" in txt[:300].lower() and X5 in txt
add, txt, div = essence("2. Содержание сообщения Совет директоров рекомендовал не выплачивать дивиденды по итогам 2026 года.")
assert div is None and add == "дивиденды не выплачивать"
add, txt, div = essence("2. Содержание сообщения 2.1. Утвердить бюджет на 2027 год. 2.2. Утвердить план работы комитета по аудиту.")
assert add == "" and div is None and not txt.startswith("Суть")
add, txt, div = essence("2. Содержание сообщения Выплатить дивиденды по привилегированным акциям в размере 1 234,5 рубля на одну акцию.")
assert div == 1234.5, div
print("дивиденд извлекается правилом; «не выплачивать» распознаётся; сообщение без сути не меняется")


# ---------- 2. шок на новости ----------
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
    def __init__(self, sym, base, side, qty, entry, mark, high, low):
        self.sym, self.base, self.side, self.q, self.entry, self.mark, self.high, self.low = sym, base, side, qty, entry, mark, high, low
        self.stops, self.closed = [], False
    def instruments(self): return {self.sym: {"base": self.base, "mult": 1.0, "min_qty": 1, "tick": 0.5, "lot": 1, "kind": "share", "expiration": None}}
    def positions(self):
        return [] if self.closed else [{"symbol": self.sym, "side": self.side, "qty": self.q, "entry": self.entry, "mark": self.mark, "mark_ok": True}]
    def klines(self, sym, iv, n):
        if iv == "5":
            return pd.DataFrame({"ts": range(6), "high": [self.high] * 6, "low": [self.low] * 6, "close": [self.mark] * 6})
        raise RuntimeError("нет данных")
    def set_stop_loss(self, sym, p, side_closing=None): self.stops.append(round(p, 2))
    def trading_open(self, sym): return True
    def cancel_all(self, sym): pass
    def close_position(self, sym): self.closed = True
    def last_price(self, sym): return self.mark
    def closed_pnl(self, *a, **k): return []
    def equity(self): return 295000.0, 295000.0
    def fmt_qty(self, sym, q): return str(int(q))
    def open_orders(self, sym): return []


class T:
    def __init__(self): self.msgs = []
    def send(self, text, html=False): self.msgs.append(text)


class Risk:
    def tradable(self, eq): return 295000.0
    def _alias(self, b): return {"SBERP": "SBER"}.get(b, b)


RISK = {"daily_trend_exit": False, "soft_stops": True, "max_hours_in_trade": 96, "breakeven_r": 99, "giveback_r": 9, "giveback_min_peak_r": 99,
        "stall_hours": 999, "trail_after_r": 99, "pyramid_part": 0, "tp_levels": [],
        "news_shock": {"enabled": True, "shock_atr": 1.5, "bars": 6, "event_minutes": 30, "min_importance": 5}}


def case(entry, stop_init, mark, high, low, side="Buy", sym="X5", base="X5", ev_sym="X5", ev_age_min=3, imp=6):
    ex, j, tg = Ex(sym, base, side, 10.0, entry, mark, high, low), Journal(":memory:"), T()
    e = Executor(S({"risk": RISK}), ex, Risk(), j, tg)
    tid = j.open_trade({"symbol": sym, "side": side, "qty": 10.0, "entry": entry, "stop": stop_init, "tp": 0, "tier": "low", "score": 0.6, "confidence": 0.7,
                        "reasons": [], "regime": "trend", "atr": 14.0, "leverage": 1, "tp_plan": [], "mult": 1})
    j._run("UPDATE trades SET entry_init=entry, opened_ts=? WHERE id=?", (int(time.time()) - 5 * 3600, tid))
    if ev_sym:
        j.add_event("n1", int(time.time()) - ev_age_min * 60, {"event_type": "earnings", "importance": imp, "direction": "bullish", "confidence": 0.5,
                    "affected_symbols": [ev_sym], "market_wide": False, "horizon_hours": 24, "reasoning": ""}, "СД X5 рекомендовал дивиденды 204 руб.")
    closed = e.manage()
    return ex, j, tg, tid, closed


# лонг в плюсе (+1.4R), бумага за минуты упала с 2377 до 2340 (2.6 ATR): стоп в безубыток, по рынку не выходим
ex, j, tg, tid, closed = case(2300.0, 2272.0, 2340.0, 2377.0, 2338.0)
assert not closed and ex.stops == [round(2300.0 * 1.0015, 2)] and any("ШОК НА НОВОСТИ" in m for m in tg.msgs), (ex.stops, tg.msgs)
print("лонг в плюсе: стоп в безубыток", ex.stops)
# лонг в небольшом минусе (−0.2R): риск вдвое меньше (стоп на −0.5R)
ex, j, tg, tid, closed = case(2346.0, 2318.0, 2340.0, 2377.0, 2338.0)
assert not closed and ex.stops == [2332.0], ex.stops
print("лонг в минусе: стоп ближе, риск вдвое меньше", ex.stops)
# лонг уже глубже нового уровня (−0.7R): ничего не делаем, по рынку на дне шока не выходим
ex, j, tg, tid, closed = case(2360.0, 2332.0, 2340.0, 2377.0, 2338.0)
assert not closed and ex.stops == [] and not ex.closed
print("цена уже за новым уровнем: исходный стоп остаётся, выхода по рынку нет")
# нет свежей новости по компании / новость по другой компании / новость старая / ход небольшой: ничего
for kw in ({"ev_sym": None}, {"ev_sym": "MGNT"}, {"ev_age_min": 90}, {"high": 2352.0}):
    args = dict(entry=2300.0, stop_init=2272.0, mark=2340.0, high=2377.0, low=2338.0)
    args.update(kw)
    ex, j, tg, tid, closed = case(**args)
    assert ex.stops == [] and not closed, kw
print("без свежей новости по компании или при небольшом ходе: позицию не трогаем")
# шорт: зеркально; новость про обычную акцию защищает позицию в привилегированной
ex, j, tg, tid, closed = case(300.0, 328.0, 280.0, 281.0, 250.0, side="Sell", sym="SBERP", base="SBERP", ev_sym="SBER")
assert ex.stops == [round(300.0 * (1 - 0.0015), 2)], ex.stops
print("шорт и привилегированная акция: работает зеркально", ex.stops)
print("ВСЁ ОК")
