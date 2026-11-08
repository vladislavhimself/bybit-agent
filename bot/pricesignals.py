"""Ценовые сигналы по дневным свечам — пока только в тенях (решение владельца 08.11.2026: 4–6 недель теней, потом решение).

История 2019–2026 на 57 ликвидных бумагах (research/price_strategies.py, breakout_check.py, breakdown_exits.py; издержки 0.3%, шорт +0.04%/день,
февраль–апрель 2022 исключены):
  breakdown  — закрытие ниже минимума закрытий за 20 дней при объёме ≥ 1.5× среднего → шорт 3–5 дней: ~5 сделок/нед, 51% в плюс,
               +0.42–0.50% на сделку, портфель ≤8 позиций +8–9%/год при просадке 16–21%, 5–6 лет из 8 в плюсе.
  breakout   — закрытие выше максимума 20 дней при объёме ≥ 1.5× → лонг 5 дней: слабее (+0.37%), после 2024 почти ноль.
  reversal12 — бумага за 5 дней отстала от индекса на 12%+ → лонг 5 дней: ~0.8/нед, 55%, +2.3% на сделку, просадка 11%.
Без фильтра объёма пробой не работает. Выходы «по тезису» на пробое хуже времени (40% в плюс), поэтому в тенях — обычные правила теней.
Скан раз в торговый день после закрытия основной сессии (hour_msk), запись через ShadowBook.record с аварийным стопом stop_pct."""
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

log = logging.getLogger("pricesignals")
MSK = ZoneInfo("Europe/Moscow")
REASONS = {"breakdown": "пробой вниз 20 дн с объёмом (тень)", "breakout": "пробой вверх 20 дн с объёмом (тень)", "reversal12": "разворот после −12% к индексу (тень)"}


def detect(daily, index_daily=None, lookback=20, vol_mult=1.5, reversal_pct=-12.0):
    """daily: DataFrame дневных свечей бумаги (ts, open, high, low, close, volume), завершённые дни, последний — сегодняшний.
    index_daily: то же по индексу (для разворота). Возвращает список причин: breakdown / breakout / reversal12."""
    out = []
    if daily is None or len(daily) < lookback + 6:
        return out
    c = daily["close"].astype(float).to_numpy()
    h = daily["high"].astype(float).to_numpy()
    v = daily["volume"].astype(float).to_numpy()
    prev_c, prev_h, prev_v = c[-lookback - 1:-1], h[-lookback - 1:-1], v[-lookback - 1:-1]
    if prev_v.mean() > 0 and v[-1] >= vol_mult * prev_v.mean():
        if c[-1] < prev_c.min():
            out.append("breakdown")
        if c[-1] > prev_h.max():
            out.append("breakout")
    if index_daily is not None and len(index_daily) >= 6 and len(c) >= 6:
        ic = index_daily["close"].astype(float).to_numpy()
        rel = (c[-1] / c[-6] - 1) * 100 - (ic[-1] / ic[-6] - 1) * 100
        if rel <= reversal_pct:
            out.append("reversal12")
    return out


class PriceSignals:
    def __init__(self, settings, exchange, shadow, journal):
        self.s, self.ex, self.shadow, self.j = settings, exchange, shadow, journal
        self.last_day = self.j.kv_get("pricesignals_day", "")

    def cfg(self, k, d=None):
        return (self.s.get("pricesignals") or {}).get(k, d)

    def due(self, now=None):
        if not self.cfg("enabled", True):
            return False
        d = datetime.fromtimestamp(now or time.time(), MSK)
        if d.weekday() >= 5 or d.hour < int(self.cfg("hour_msk", 19)):
            return False
        return d.strftime("%Y-%m-%d") != self.last_day

    def _index_daily(self):
        for sym in self.cfg("index_symbols") or ["IMOEXF", "SBERF"]:
            try:
                d = self.ex.klines(sym, "D", 30)
                if d is not None and len(d) >= 6:
                    return d
            except Exception as e:  # noqa: BLE001
                log.debug("дневные свечи индекса %s: %s", sym, e)
        return None

    def scan(self, symbols, now=None):
        """Проверить бумаги и записать тени. Возвращает список (символ, причина)."""
        now = now or time.time()
        today = datetime.fromtimestamp(now, MSK).strftime("%Y-%m-%d")
        self.last_day = today
        self.j.kv_set("pricesignals_day", today)
        idx = self._index_daily()
        found = []
        inst = self.ex.instruments()
        for sym in symbols:
            it = inst.get(sym) or {}
            try:
                d = self.ex.klines(sym, "D", int(self.cfg("lookback", 20)) + 10)
            except Exception as e:  # noqa: BLE001
                log.debug("дневные свечи %s: %s", sym, e)
                continue
            if d is None or len(d) < int(self.cfg("lookback", 20)) + 6:
                continue
            # последняя свеча должна быть сегодняшней (сессия закрыта, свеча завершена)
            last_day = datetime.fromtimestamp(int(d["ts"].iloc[-1]) / 1000, MSK).strftime("%Y-%m-%d")
            if last_day != today:
                continue
            reasons = detect(d, idx, int(self.cfg("lookback", 20)), float(self.cfg("vol_mult", 1.5)), float(self.cfg("reversal_pct", -12)))
            for why in reasons:
                side = "Sell" if why == "breakdown" else "Buy"
                if side == "Sell" and it.get("kind") == "share" and not it.get("short_ok", False):
                    continue
                price = float(d["close"].iloc[-1])
                stop_pct = float(self.cfg("stop_pct", 5.0))
                atr = price * stop_pct / 100 / float(self.s.get("risk.stop_atr_mult") or 2.0)   # record() умножит на stop_atr_mult → стоп = stop_pct
                sid = self.shadow.record(sym, side, why, 1.0, 0.0, price, atr, regime="daily", comps={"vol_mult": round(float(d["volume"].iloc[-1]) /
                                         max(1e-9, float(d["volume"].iloc[-21:-1].mean())), 2)}, ts=int(now), pull_atr=0.0)
                if sid:
                    found.append((sym, why))
        if found:
            log.info("ценовые тени за %s: %s", today, ", ".join(f"{s} {w}" for s, w in found))
        return found

    @staticmethod
    def text(found):
        if not found:
            return ["сегодня сигналов нет"]
        return [f"{s}: {REASONS.get(w, w)}" for s, w in found]
