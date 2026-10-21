"""Память ожиданий рынка: что консенсус ждёт от ближайших событий (ставка ЦБ, отчёты, дивиденды, инфляция).
Раз в час модель вытаскивает ожидания из свежих новостей (строки с «консенсус», «ожидают», «прогноз», «аналитики»)
и календаря; список хранится в журнале и подаётся аналитику в контекст: когда выходит факт, он сравнивается
с ожиданием (expected/surprise), а не с заголовком."""
import json
import logging
import re
import time
from typing import List

from pydantic import BaseModel

log = logging.getLogger("expectations")
KEY = re.compile(r"консенсус|ожида|прогноз|аналитик|рынок закладыва|опрос", re.I)
SYSTEM = """Ты собираешь ожидания рынка перед событиями на российском рынке акций. По выдержкам из новостей и календарю
составь список ожиданий: событие, дата (если известна), что ждут (число/решение/диапазон), откуда ожидание (консенсус
Интерфакса/РБК, опрос аналитиков, прогноз компании, прошлое значение как база), уверенность 0..1.
Только то, что прямо следует из текста; не выдумывай консенсус. Уже вышедшие факты не включай. Не больше 12 пунктов.
Приоритет: ставка и риторика ЦБ РФ, недельная инфляция, отчёты и дивиденды российских компаний, санкции и переговоры,
нефть и рубль. Зарубежная макростатистика — не больше 2 пунктов и только если прямо влияет на нефть или рубль."""


class Expectation(BaseModel):
    event: str
    date: str          # ГГГГ-ММ-ДД или ""
    expected: str
    source: str
    symbols: List[str]
    confidence: float


class Expectations(BaseModel):
    items: List[Expectation]


class ExpectationsMemory:
    def __init__(self, settings, journal, analyst, calendar=None):
        self.s, self.j, self.analyst, self.cal = settings, journal, analyst, calendar
        self.items = json.loads(journal.kv_get("expectations_json", "[]") or "[]")
        self.last_ts = float(journal.kv_get("expectations_ts", 0) or 0)

    def due(self, now=None):
        return (now or time.time()) - self.last_ts >= 3600

    def refresh(self, now=None):
        now = now or time.time()
        if not self.analyst:
            return
        rows = self.j._run("SELECT title, summary, published_ts FROM news WHERE published_ts > ? ORDER BY published_ts DESC LIMIT 400",
                           (int(now - 36 * 3600),), "all")
        lines = []
        for r in rows:
            text = f"{r['title']}. {r['summary'] or ''}"
            if KEY.search(text):
                lines.append(text[:400])
            if len(lines) >= 60:
                break
        cal = self.cal.context_text(240) if self.cal else ""
        if not lines and not cal:
            self.last_ts = now
            return
        user = "Календарь:\n" + (cal or "нет") + "\n\nВыдержки из новостей за 36 часов:\n" + "\n".join(f"- {l}" for l in lines)
        try:
            res = self.analyst._parse(self.analyst.cheap, SYSTEM, user, Expectations, 4000, "low")
        except Exception as e:  # noqa: BLE001
            log.warning("ожидания: %s", str(e)[:120])
            self.last_ts = now
            return
        if res:
            self.items = [i.model_dump() for i in res.items if i.confidence >= 0.3][:12]
            self.j.kv_set("expectations_json", json.dumps(self.items, ensure_ascii=False))
            log.info("Ожидания рынка обновлены: %d пунктов", len(self.items))
        self.last_ts = now
        self.j.kv_set("expectations_ts", now)

    def text(self):
        if not self.items:
            return ""
        parts = []
        for i in self.items:
            d = f" ({i['date']})" if i.get("date") else ""
            sym = f" [{', '.join(i['symbols'])}]" if i.get("symbols") else ""
            parts.append(f"- {i['event']}{d}{sym}: ждут {i['expected']} (источник: {i['source']}, уверенность {i['confidence']:.1f})")
        return "Ожидания рынка (консенсус до выхода факта; сравнивай факт с ними):\n" + "\n".join(parts)
