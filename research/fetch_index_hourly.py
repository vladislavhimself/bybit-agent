"""Часовые свечи индексов Мосбиржи (IMOEX, RTSI) с ISS за много лет -> data/history/<индекс>_60.csv в формате остальных файлов
(ts в секундах UTC, open, high, low, close, volume, turnover). Запуск из корня: python research/fetch_index_hourly.py --since 2019-01-01"""
import argparse
import csv
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
MSK = ZoneInfo("Europe/Moscow")
URL = "https://iss.moex.com/iss/engines/stock/markets/index/boards/SNDX/securities/{sec}/candles.json"


def fetch(sec, since, till):
    out = []
    d = since
    s = requests.Session()
    while d < till:
        e = min(till, d + timedelta(days=90))
        start = 0
        while True:
            r = s.get(URL.format(sec=sec), params={"interval": 60, "from": d.isoformat(), "till": e.isoformat(), "start": start, "iss.meta": "off"}, timeout=30)
            r.raise_for_status()
            j = r.json()["candles"]
            cols, rows = j["columns"], j["data"]
            if not rows:
                break
            for row in rows:
                x = dict(zip(cols, row))
                ts = int(datetime.strptime(x["begin"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK).astimezone(timezone.utc).timestamp())
                out.append((ts, x["open"], x["high"], x["low"], x["close"], x["volume"], x["value"]))
            if len(rows) < 500:
                break
            start += len(rows)
            time.sleep(0.2)
        print(f"{sec}: до {e}, всего {len(out)}", flush=True)
        d = e + timedelta(days=1)
        time.sleep(0.2)
    seen, res = set(), []
    for row in sorted(out):
        if row[0] not in seen:
            seen.add(row[0])
            res.append(row)
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2019-01-01")
    ap.add_argument("--secs", nargs="+", default=["IMOEX", "RTSI"])
    a = ap.parse_args()
    since = datetime.strptime(a.since, "%Y-%m-%d").date()
    for sec in a.secs:
        rows = fetch(sec, since, datetime.now(MSK).date())
        p = ROOT / "data" / "history" / f"{sec}_60.csv"
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["ts", "open", "high", "low", "close", "volume", "turnover"])
            w.writerows(rows)
        print(f"{sec}: записано {len(rows)} свечей в {p.name}, "
              f"{datetime.fromtimestamp(rows[0][0], timezone.utc).date()} — {datetime.fromtimestamp(rows[-1][0], timezone.utc).date()}")
