"""Настроение толпы по бумаге: форум Смарт-лаба (smart-lab.ru/forum/<тикер>), последние комментарии.
Модель оценивает: куда смотрит розница (−1 шорт … +1 лонг), насколько единодушно, чего ждёт от ближайшего события.
Используется как контраргумент (единодушная толпа = риск против неё) и как ожидания розницы перед новостью.
Не главный сигнал: его ценность проверяется теневыми сделками."""
import json
import logging
import re
import time
from typing import List

from bs4 import BeautifulSoup
from pydantic import BaseModel

from . import net

log = logging.getLogger("crowd")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
      "Accept-Language": "ru"}
SYSTEM = """Ты оцениваешь настроение розничных инвесторов по комментариям с форума по одной акции на Мосбирже.
Верни: sentiment −1..1 (−1 все шортят/ждут падения, +1 все в лонге/ждут роста), agreement 0..1 (насколько единодушны),
expectation — чего толпа ждёт от ближайшего события по бумаге одной фразой (или ""), summary — 1 предложение.
Шутки и оффтоп игнорируй. Если комментариев мало или они ни о чём — sentiment 0, agreement 0."""


class CrowdView(BaseModel):
    sentiment: float
    agreement: float
    expectation: str
    summary: str


class Crowd:
    def __init__(self, settings, analyst, journal=None):
        self.s, self.analyst, self.j = settings, analyst, journal
        self.session = net.session()
        self.session.headers.update(UA)
        self.cache = {}   # base -> (ts, dict)
        self.ttl = 3600

    def _comments(self, base):
        r = self.session.get(f"https://smart-lab.ru/forum/{base}", timeout=25)
        if r.status_code != 200:
            return []
        soup = BeautifulSoup(r.text, "lxml")
        out = []
        for d in soup.find_all("div", class_="cmt_body"):
            t = re.sub(r"\s+", " ", d.get_text(" ", strip=True))
            if 15 <= len(t) <= 600:
                out.append(t)
        return out[-30:]

    def view(self, base):
        """Оценка толпы по базовому тикеру, кэш 1 час. None, если данных нет."""
        c = self.cache.get(base)
        if c and time.time() - c[0] < self.ttl:
            return c[1]
        res = None
        try:
            comments = self._comments(base)
            if len(comments) >= 5 and self.analyst:
                user = f"Акция {base}. Комментарии (от старых к новым):\n" + "\n".join(f"- {x[:300]}" for x in comments)
                v = self.analyst._parse(self.analyst.cheap, SYSTEM, user, CrowdView, 1500, "low")
                if v:
                    res = v.model_dump()
                    res["sentiment"] = max(-1.0, min(1.0, float(res["sentiment"])))
                    res["agreement"] = max(0.0, min(1.0, float(res["agreement"])))
                    res["n"] = len(comments)
        except Exception as e:  # noqa: BLE001
            log.debug("толпа %s: %s", base, e)
        self.cache[base] = (time.time(), res)
        return res

    def score(self, base):
        """Вклад в позиционирование: единодушная толпа — аргумент против неё. −1..1 (знак = против толпы)."""
        v = self.view(base)
        if not v:
            return 0.0, ""
        if v["agreement"] < 0.6 or abs(v["sentiment"]) < 0.4:
            return 0.0, ""
        contra = -v["sentiment"] * v["agreement"]
        side = "в лонге" if v["sentiment"] > 0 else "в шорте"
        return round(contra * 0.5, 3), f"толпа на форуме {side} ({v['sentiment']:+.1f}, единодушие {v['agreement']:.1f}): риск против толпы"

    def text(self, bases):
        parts = []
        for b in bases[:8]:
            v = self.cache.get(b, (0, None))[1]
            if v and (v.get("expectation") or abs(v["sentiment"]) >= 0.4):
                parts.append(f"- {b}: настроение {v['sentiment']:+.1f}, единодушие {v['agreement']:.1f}; ждут: {v.get('expectation') or '—'}")
        return ("Розница на форумах (ожидания толпы, не консенсус аналитиков):\n" + "\n".join(parts)) if parts else ""
