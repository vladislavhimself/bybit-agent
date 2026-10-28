"""Проверка драйвера движения в прибыльной сделке (пока только в тенях).

Когда сделка забралась выше min_peak_r, раз в every_min минут дешёвая модель смотрит: жив ли драйвер движения
(поток новостей по бумаге и рынку за последние часы, тренд 4ч, импульс последних часов, насколько далеко от пика)
и говорит: hold (держать, поводок не укорачивать), tighten (укоротить поводок), take (забрать прибыль).
В режиме shadow бот ничего не делает, а записывает вердикт и цену; через 4 и 24 часа дописывает, куда ушла цена,
и в /analytics показывает, кто был прав: модель или трейлинг. Включать боевой режим — только когда тени покажут плюс.
"""
import json
import logging
import time
from typing import Literal

from pydantic import BaseModel

from . import fmt, ta

log = logging.getLogger("driver")

SYSTEM = """Ты риск-менеджер открытой прибыльной позиции на Мосбирже. Решаешь только одно: продолжать ли держать.
Отвечай строго по схеме. Критерии:
- hold: драйвер движения жив (свежие новости в ту же сторону, тренд 4ч в сторону позиции, цена у пика, импульс не угас);
- tighten: драйвер выдыхается (новостей нет или появились противоположные, импульс замедлился, откат от пика растёт);
- take: драйвер сломан (важная противоположная новость, разворот тренда, цена быстро уходит от пика).
Не придумывай новости, опирайся только на переданные. Уверенность 0..1."""


class DriverVerdict(BaseModel):
    verdict: Literal["hold", "tighten", "take"]
    driver_alive: bool
    confidence: float
    reasoning: str


class DriverCheck:
    def __init__(self, settings, analyst, journal, exchange):
        self.s, self.an, self.j, self.ex = settings, analyst, journal, exchange
        self.last = {}   # trade_id -> ts последней проверки
        self.j._run("""CREATE TABLE IF NOT EXISTS driver_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, trade_id INTEGER, symbol TEXT, side TEXT,
            r_now REAL, mfe REAL, mark REAL, risk_dist REAL, verdict TEXT, confidence REAL, reasoning TEXT,
            r4 REAL, r24 REAL, final_r REAL)""")

    def cfg(self):
        return self.s.get("risk.driver_check") or {}

    # ---------- проверка ----------
    def maybe_check(self, t, r_now, mfe, mark, risk_dist, hours):
        cfg = self.cfg()
        if not cfg.get("mode") or mfe < cfg.get("min_peak_r", 2.0) or r_now <= 0:
            return None
        now = time.time()
        if now - self.last.get(t["id"], 0) < cfg.get("every_min", 30) * 60:
            return None
        self.last[t["id"]] = now
        try:
            v = self._ask(t, r_now, mfe, mark, hours)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: проверка драйвера не удалась: %s", t["symbol"], e)
            return None
        if v is None:
            return None
        self.j._run("INSERT INTO driver_checks(ts,trade_id,symbol,side,r_now,mfe,mark,risk_dist,verdict,confidence,reasoning) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (int(now), t["id"], t["symbol"], t["side"], r_now, mfe, mark, risk_dist, v.verdict, v.confidence, v.reasoning[:400]))
        log.info("%s: драйвер (%s): %s %.2f — %s", t["symbol"], cfg.get("mode"), v.verdict, v.confidence, v.reasoning[:120])
        return v

    def _ask(self, t, r_now, mfe, mark, hours):
        base = fmt.base_of(t["symbol"])
        long = t["side"] == "Buy"
        same, opp, lines = 0.0, 0.0, []
        for e in self.j.recent_events(12):
            direct = base in (e.get("affected") or [])
            if not direct and not e.get("market_wide"):
                continue
            if e["direction"] == "neutral":
                continue
            w = e["importance"] * (1.0 if direct else 0.5)
            with_us = (e["direction"] == "bullish") == long
            same += w if with_us else 0
            opp += 0 if with_us else w
            age = (time.time() - e["ts"]) / 3600
            if len(lines) < 10:
                lines.append(f"- {age:.1f}ч назад, важность {e['importance']}, {e['direction']}{' (по бумаге)' if direct else ' (рынок)'}: "
                             f"{(e.get('title') or '')[:110]}")
        d = self.ex.klines(t["symbol"], "60", 60)
        c = d["close"]
        e20 = ta.ema(c, 20).iloc[-1]
        last4 = (c.iloc[-1] / c.iloc[-5] - 1) * 100 if len(c) >= 5 else 0.0
        trend4h = "вверх" if c.iloc[-1] > e20 else "вниз"
        user = (f"Позиция: {fmt.label(t['symbol'])} {'ЛОНГ' if long else 'ШОРТ'}, в сделке {hours:.1f} ч.\n"
                f"Прибыль сейчас {r_now:.2f}R, пик был {mfe:.2f}R (откат от пика {mfe - r_now:.2f}R). Цена {mark:g}.\n"
                f"Техника: цена {trend4h} относительно EMA20 на часах, ход за последние 4 часа {last4:+.2f}%.\n"
                f"Новости за 12 ч: за позицию вес {same:.0f}, против {opp:.0f}.\n" + ("\n".join(lines) if lines else "Новостей нет."))
        return self.an._parse(self.an.cheap, SYSTEM, user, DriverVerdict, max_tokens=1500, effort="low")

    # ---------- проверка задним числом ----------
    def evaluate_due(self):
        """Дописать, куда ушла цена через 4 и 24 часа после вердикта (в R, в сторону позиции)."""
        now = int(time.time())
        rows = self.j._run("SELECT * FROM driver_checks WHERE (r4 IS NULL AND ts<=?) OR (r24 IS NULL AND ts<=?)",
                           (now - 4 * 3600, now - 24 * 3600), "all")
        cache = {}
        for r in rows:
            try:
                if r["symbol"] not in cache:
                    cache[r["symbol"]] = self.ex.klines(r["symbol"], "60", 200)
                d = cache[r["symbol"]]
            except Exception as e:  # noqa: BLE001
                log.debug("driver eval %s: %s", r["symbol"], e)
                continue
            sign = 1 if r["side"] == "Buy" else -1
            ts_col = d["ts"] if "ts" in d.columns else None
            if ts_col is None or not r["risk_dist"]:
                continue
            tsv = ts_col.values / (1000 if ts_col.max() > 1e11 else 1)
            upd = {}
            for h, col in ((4, "r4"), (24, "r24")):
                if r[col] is not None or r["ts"] + h * 3600 > now:
                    continue
                idx = (tsv >= r["ts"] + h * 3600).nonzero()[0]
                if len(idx):
                    px = float(d["close"].iloc[idx[0]])
                    upd[col] = sign * (px - r["mark"]) / r["risk_dist"]
            if upd:
                sets = ", ".join(f"{k}=?" for k in upd)
                self.j._run(f"UPDATE driver_checks SET {sets} WHERE id=?", (*upd.values(), r["id"]))
        # итог сделки, когда она закрыта
        self.j._run("""UPDATE driver_checks SET final_r = (
                SELECT (CASE WHEN t.side='Buy' THEN 1 ELSE -1 END) * (t.exit - driver_checks.mark) / driver_checks.risk_dist
                FROM trades t WHERE t.id = driver_checks.trade_id AND t.closed_ts IS NOT NULL)
            WHERE final_r IS NULL""")

    # ---------- сводка ----------
    def summary_text(self, days=30):
        rows = self.j._run("SELECT verdict, r4, r24, final_r FROM driver_checks WHERE ts>=?",
                           (int(time.time()) - days * 86400, ), "all")
        if not rows:
            return None
        out = []
        for v in ("hold", "tighten", "take"):
            g = [r for r in rows if r["verdict"] == v]
            if not g:
                continue
            r24 = [r["r24"] for r in g if r["r24"] is not None]
            fin = [r["final_r"] for r in g if r["final_r"] is not None]
            line = f"{v}: {len(g)}"
            if r24:
                line += f", цена через 24ч {sum(r24) / len(r24):+.2f}R (в сторону позиции {sum(1 for x in r24 if x > 0) / len(r24) * 100:.0f}%)"
            if fin:
                line += f", факт до выхода {sum(fin) / len(fin):+.2f}R"
            out.append(line)
        hold = [r["r24"] for r in rows if r["verdict"] == "hold" and r["r24"] is not None]
        take = [r["r24"] for r in rows if r["verdict"] == "take" and r["r24"] is not None]
        verdict = "мало данных"
        if len(hold) >= 10 and len(take) >= 5:
            verdict = ("модель права: после hold цена шла дальше, после take — нет" if sum(hold) / len(hold) > 0 > sum(take) / len(take)
                       else "модель не лучше трейлинга")
        out.append("Вывод: " + verdict)
        return out
