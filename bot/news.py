"""Сбор новостей из быстрых бесплатных источников.
Скорость (от быстрого к медленному): Telegram-каналы (публичный просмотр t.me/s/, без логина) -> анонсы Binance/Bybit
-> RSS изданий. Дедупликация через журнал. Опционально CryptoPanic по ключу."""
import hashlib
import html as html_lib
import logging
import os
import re
import time
from datetime import datetime
from email.utils import parsedate_to_datetime

import feedparser
import requests

log = logging.getLogger("news")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

RSS_SOURCES = {
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "cointelegraph": "https://cointelegraph.com/rss",
    "theblock": "https://www.theblock.co/rss.xml",
    "decrypt": "https://decrypt.co/feed",
}
BINANCE_ANN = ("https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
               "?type=1&pageNo=1&pageSize=20&catalogId=48")

TG_MSG_RE = re.compile(
    r'data-post="([^"]+)".*?<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>.*?<time datetime="([^"]+)"',
    re.S)


def _rss_ts(entry):
    for key in ("published", "updated"):
        val = entry.get(key)
        if val:
            try:
                return int(parsedate_to_datetime(val).timestamp())
            except (TypeError, ValueError):
                pass
    for key in ("published_parsed", "updated_parsed"):
        if entry.get(key):
            return int(time.mktime(entry[key]))
    return int(time.time())


def _clean(s):
    s = re.sub(r"<br\s*/?>", " ", s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return " ".join(html_lib.unescape(s).split())[:1200]


def _nid(prefix, key):
    return prefix + "_" + hashlib.sha1(key.encode()).hexdigest()[:14]


class NewsCollector:
    def __init__(self, exchange, journal, settings, max_age_hours=12):
        self.ex, self.j, self.s = exchange, journal, settings
        self.max_age = max_age_hours * 3600
        self.first_run = True
        self.last = {"rss": 0.0, "binance": 0.0, "bybit": 0.0, "cryptopanic": 0.0}
        self.tg_channels = settings.get("sources.telegram_channels") or []
        self.session = requests.Session()
        self.session.headers.update(UA)

    # ---------- источники ----------
    def _telegram(self, now):
        items = []
        for ch in self.tg_channels:
            try:
                r = self.session.get(f"https://t.me/s/{ch}", timeout=12)
                if r.status_code != 200:
                    continue
                for post, text, dt in TG_MSG_RE.findall(r.text):
                    try:
                        ts = int(datetime.fromisoformat(dt).timestamp())
                    except ValueError:
                        ts = now
                    if now - ts > self.max_age:
                        continue
                    body = _clean(text)
                    if len(body) < 15:
                        continue
                    items.append({"id": _nid("tg", post), "source": f"tg:{ch}", "title": body[:160],
                                  "summary": body, "url": f"https://t.me/{post}", "published_ts": ts})
            except requests.RequestException as e:
                log.debug("telegram %s: %s", ch, e)
        return items

    def _rss(self, now):
        items = []
        for name, url in RSS_SOURCES.items():
            try:
                feed = feedparser.parse(url, request_headers=UA)
                for e in feed.entries:
                    ts = _rss_ts(e)
                    if now - ts > self.max_age:
                        continue
                    link = e.get("link", "")
                    items.append({"id": _nid("rss", link or e.get("title", "")), "source": name,
                                  "title": e.get("title", "").strip(), "summary": _clean(e.get("summary", "")),
                                  "url": link, "published_ts": ts})
            except Exception as ex:  # noqa: BLE001
                log.warning("RSS %s: %s", name, ex)
        return items

    def _binance(self, now):
        items = []
        try:
            r = self.session.get(BINANCE_ANN, timeout=12).json()
            for a in r["data"]["catalogs"][0]["articles"]:
                ts = int(int(a.get("releaseDate") or now * 1000) / 1000)
                if now - ts > self.max_age:
                    continue
                items.append({"id": _nid("bn", a["code"]), "source": "binance_announcement", "title": a["title"],
                              "summary": a["title"], "url": f"https://www.binance.com/en/support/announcement/{a['code']}",
                              "published_ts": ts})
        except Exception as ex:  # noqa: BLE001
            log.warning("Binance announcements: %s", ex)
        return items

    def _bybit(self, now):
        items = []
        try:
            for a in self.ex.announcements(20):
                ts = int(int(a.get("dateTimestamp") or now * 1000) / 1000)
                if now - ts > self.max_age:
                    continue
                items.append({"id": _nid("bb", a.get("url") or a.get("title", "")), "source": "bybit_announcement",
                              "title": a.get("title", ""), "summary": _clean(a.get("description", "")),
                              "url": a.get("url", ""), "published_ts": ts})
        except Exception as ex:  # noqa: BLE001
            log.warning("Bybit announcements: %s", ex)
        return items

    def _cryptopanic(self, now):
        key = os.getenv("CRYPTOPANIC_API_KEY", "").strip()
        if not key:
            return []
        items = []
        try:
            r = self.session.get("https://cryptopanic.com/api/growth/v2/posts/",
                                 params={"auth_token": key, "public": "true", "kind": "news"}, timeout=15).json()
            for p in r.get("results", []):
                ts = int(datetime.fromisoformat(p["published_at"].replace("Z", "+00:00")).timestamp())
                if now - ts > self.max_age:
                    continue
                items.append({"id": _nid("cp", str(p.get("id"))), "source": "cryptopanic", "title": p.get("title", ""),
                              "summary": p.get("description") or p.get("title", ""), "url": p.get("url", ""),
                              "published_ts": ts})
        except Exception as ex:  # noqa: BLE001
            log.warning("CryptoPanic: %s", ex)
        return items

    # ---------- опрос ----------
    def poll(self):
        """Вернуть новые новости. Telegram опрашивается каждый вызов, остальное раз в минуту."""
        now = int(time.time())
        items = self._telegram(now)
        if now - self.last["binance"] >= 60:
            self.last["binance"] = now
            items += self._binance(now)
        if now - self.last["bybit"] >= 60:
            self.last["bybit"] = now
            items += self._bybit(now)
        if now - self.last["rss"] >= 60:
            self.last["rss"] = now
            items += self._rss(now)
        if now - self.last["cryptopanic"] >= 60:
            self.last["cryptopanic"] = now
            items += self._cryptopanic(now)
        fresh = [it for it in items if self.j.add_news(it)]
        if self.first_run:
            self.first_run = False
            for it in fresh:
                self.j.mark_analyzed(it["id"])
            log.info("Первый опрос: %d новостей записано как фон (не торгуем)", len(fresh))
            return []
        fresh.sort(key=lambda x: x["published_ts"])
        return fresh
