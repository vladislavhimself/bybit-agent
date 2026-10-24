"""Архив российских новостей из публичных Telegram-каналов (веб-превью t.me/s/<канал>?before=<id>, 20 сообщений
на страницу, листается назад без ограничений). Складывает в data/news_archive.db (таблица msgs: channel, id, ts, text).
Повторный запуск продолжает с самого старого уже сохранённого сообщения.
Запуск: python research/news_archive.py --channels markettwits interfaxonline --since 2024-10-01"""
import argparse
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

sys.stdout.reconfigure(encoding="utf-8")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"}
DB = "data/news_archive.db"


def db():
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS msgs (channel TEXT, id INTEGER, ts INTEGER, text TEXT, PRIMARY KEY(channel, id))")
    c.execute("CREATE INDEX IF NOT EXISTS msgs_ts ON msgs(ts)")
    return c


def page(session, channel, before=None):
    params = {"before": before} if before else {}
    for attempt in range(4):
        try:
            r = session.get(f"https://t.me/s/{channel}", params=params, headers=UA, timeout=25)
            if r.status_code == 200:
                break
        except requests.RequestException:
            pass
        time.sleep(2 + attempt * 3)
    else:
        return []
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for m in soup.select("div.tgme_widget_message"):
        post = m.get("data-post", "")
        mid = post.split("/")[-1]
        if not mid.isdigit():
            continue
        t = m.select_one("time[datetime]")
        body = m.select_one("div.tgme_widget_message_text")
        if not t or not body:
            continue
        ts = int(datetime.fromisoformat(t["datetime"].replace("Z", "+00:00")).timestamp())
        text = re.sub(r"[ \t\xa0]+", " ", body.get_text("\n", strip=True)).strip()
        if text:
            out.append((int(mid), ts, text))
    return out


def crawl(channel, since_ts, session, max_pages=20000):
    c = db()
    row = c.execute("SELECT MIN(id), MIN(ts), COUNT(*) FROM msgs WHERE channel=?", (channel,)).fetchone()
    before = row[0] if row and row[0] else None
    if row and row[1] and row[1] <= since_ts:
        print(f"{channel}: уже есть до {datetime.fromtimestamp(row[1], timezone.utc).date()}, {row[2]} сообщений")
        return
    total, pages = row[2] or 0, 0
    oldest = row[1] or int(time.time())
    while pages < max_pages and oldest > since_ts:
        msgs = page(session, channel, before)
        pages += 1
        if not msgs:
            print(f"{channel}: пустая страница на before={before}, стоп")
            break
        c.executemany("INSERT OR IGNORE INTO msgs VALUES(?,?,?,?)", [(channel, mid, ts, text) for mid, ts, text in msgs])
        c.commit()
        before = min(m[0] for m in msgs)
        oldest = min(m[1] for m in msgs)
        total += len(msgs)
        if pages % 50 == 0:
            print(f"{channel}: {total} сообщений, дошли до {datetime.fromtimestamp(oldest, timezone.utc).date()}", flush=True)
        time.sleep(0.35)
    print(f"{channel}: готово, {total} сообщений, самое старое {datetime.fromtimestamp(oldest, timezone.utc).date()}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--channels", nargs="+", required=True)
    ap.add_argument("--since", default="2024-10-01")
    a = ap.parse_args()
    since_ts = int(datetime.strptime(a.since, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    s = requests.Session()
    for ch in a.channels:
        crawl(ch, since_ts, s)
