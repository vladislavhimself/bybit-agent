"""Календарь будущих событий: макро (ForexFactory, бесплатно), заседания ФРС (зашиты), крипто-события (CoinMarketCal, по ключу).
Используется для: запрета новых входов вокруг важных релизов, контекста для модели, снижения размера перед событием."""
import logging
import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("calendar")
NY = ZoneInfo("America/New_York")

# Дни решений FOMC (второй день заседания), решение в 14:00 по Нью-Йорку. Источник: federalreserve.gov
FOMC_DECISION_DAYS = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FF_NEXT_URL = "https://nfs.faireconomy.media/ff_calendar_nextweek.json"
CMC_URL = "https://developers.coinmarketcal.com/v1/events"

HIGH_KEYWORDS = ("CPI", "FOMC", "Federal Funds", "Non-Farm", "Nonfarm", "Unemployment Rate", "PCE", "GDP", "Fed Chair",
                 "PPI", "Retail Sales", "ISM")


class Calendar:
    def __init__(self, refresh_sec=3600):
        self.events = []        # {ts, title, impact: high|medium|low, kind: macro|crypto|fomc, source}
        self.refresh_sec = refresh_sec
        self.last = 0.0

    def refresh(self, force=False):
        if not force and time.time() - self.last < self.refresh_sec:
            return
        self.last = time.time()
        ev = []
        for day in FOMC_DECISION_DAYS:
            dt = datetime.strptime(day + " 14:00", "%Y-%m-%d %H:%M").replace(tzinfo=NY)
            ev.append({"ts": int(dt.timestamp()), "title": "FOMC: решение по ставке", "impact": "high",
                       "kind": "fomc", "source": "fed"})
        for url in (FF_URL, FF_NEXT_URL):
            try:
                r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code != 200:
                    continue
                for e in r.json():
                    if e.get("country") != "USD" or e.get("impact") not in ("High", "Medium"):
                        continue
                    try:
                        ts = int(datetime.fromisoformat(e["date"]).timestamp())
                    except (KeyError, ValueError):
                        continue
                    impact = "high" if e["impact"] == "High" or any(k in e["title"] for k in HIGH_KEYWORDS) else "medium"
                    ev.append({"ts": ts, "title": f"{e['title']} (прогноз {e.get('forecast') or '-'}, "
                                                  f"пред. {e.get('previous') or '-'})",
                               "impact": impact, "kind": "macro", "source": "forexfactory"})
            except (requests.RequestException, ValueError) as ex:
                log.warning("ForexFactory: %s", ex)
        key = os.getenv("COINMARKETCAL_API_KEY", "").strip()
        if key:
            try:
                r = requests.get(CMC_URL, params={"max": 75, "sortBy": "hot_events"},
                                 headers={"x-api-key": key, "Accept": "application/json"}, timeout=20)
                for e in r.json().get("body", []):
                    ts = int(datetime.fromisoformat(e["date_event"].replace("Z", "+00:00")).timestamp())
                    coins = ",".join(c.get("symbol", "") for c in e.get("coins", []))
                    ev.append({"ts": ts, "title": f"{e['title'].get('en', '')} [{coins}]", "impact": "medium",
                               "kind": "crypto", "source": "coinmarketcal"})
            except Exception as ex:  # noqa: BLE001
                log.warning("CoinMarketCal: %s", ex)
        # дедупликация по (ts, title)
        seen, out = set(), []
        for e in sorted(ev, key=lambda x: x["ts"]):
            k = (e["ts"], e["title"][:40])
            if k not in seen:
                seen.add(k)
                out.append(e)
        self.events = out
        log.info("Календарь: %d событий, ближайшее высокой важности: %s", len(out), self.next_high())

    def upcoming(self, hours=72, now=None):
        now = now or time.time()
        return [e for e in self.events if now <= e["ts"] <= now + hours * 3600]

    def next_high(self, now=None):
        now = now or time.time()
        for e in self.events:
            if e["ts"] >= now and e["impact"] == "high":
                return f"{e['title']} через {(e['ts'] - now) / 3600:.1f} ч"
        return None

    def blackout(self, before_min, after_min, now=None):
        """(True, событие) если сейчас окно вокруг релиза высокой важности."""
        now = now or time.time()
        for e in self.events:
            if e["impact"] != "high":
                continue
            if e["ts"] - before_min * 60 <= now <= e["ts"] + after_min * 60:
                return True, e
        return False, None

    def context_text(self, hours=96, now=None):
        now = now or time.time()
        rows = self.upcoming(hours, now)
        if not rows:
            return "Запланированных важных событий в ближайшие дни нет."
        lines = []
        for e in rows[:15]:
            dt = datetime.fromtimestamp(e["ts"], tz=timezone.utc).strftime("%d.%m %H:%M UTC")
            lines.append(f"- {dt} [{e['impact']}] {e['title']}")
        return "Ближайшие запланированные события:\n" + "\n".join(lines)
