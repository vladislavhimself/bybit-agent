"""Часовые свечи бессрочных фьючерсов Bybit (linear USDT) с 2020 года -> data/history/<SYM>_60.csv (ts в мс, как у крипто-истории бота).
Публичный API v5, без ключей. Запуск из корня: python research/fetch_bybit_hourly.py [--since 2020-01-01] [--symbols BTCUSDT ...]"""
import argparse
import csv
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
URL = "https://api.bybit.com/v5/market/kline"
DEFAULT = ["BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT", "DOGEUSDT", "ADAUSDT", "BNBUSDT", "LINKUSDT", "AVAXUSDT", "DOTUSDT", "LTCUSDT", "BCHUSDT",
           "TRXUSDT", "UNIUSDT", "AAVEUSDT", "ATOMUSDT", "NEARUSDT", "XLMUSDT", "ETCUSDT", "FILUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "INJUSDT",
           "SUIUSDT", "TONUSDT", "1000SHIBUSDT", "1000PEPEUSDT", "WLDUSDT", "TIAUSDT", "SEIUSDT", "FETUSDT", "RENDERUSDT", "ICPUSDT", "HBARUSDT",
           "ALGOUSDT", "XMRUSDT", "ZECUSDT", "ENAUSDT", "ONDOUSDT", "POLUSDT", "SANDUSDT", "AXSUSDT", "GALAUSDT", "CRVUSDT", "LDOUSDT", "MKRUSDT", "RUNEUSDT"]


def fetch(sym, since_ms, s):
    out, start = [], since_ms
    now_ms = int(time.time() * 1000)
    while start < now_ms:
        for attempt in range(4):
            try:
                r = s.get(URL, params={"category": "linear", "symbol": sym, "interval": "60", "start": start, "limit": 1000}, timeout=30)
                j = r.json()
                if j.get("retCode") == 0:
                    break
                if j.get("retCode") == 10001:   # нет такого символа
                    return out, j.get("retMsg")
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1 + attempt)
        else:
            return out, "сеть"
        rows = j["result"]["list"]
        if not rows:
            # Bybit отдаёт пусто, если start раньше листинга: сдвигаемся на 1000 часов вперёд
            start += 1000 * 3600 * 1000
            if start > now_ms:
                break
            continue
        rows = sorted(rows, key=lambda x: int(x[0]))
        out += [(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5]), float(x[6])) for x in rows]
        last = int(rows[-1][0])
        if last <= start:
            break
        start = last + 3600 * 1000
        time.sleep(0.12)
    return out, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2020-01-01")
    ap.add_argument("--symbols", nargs="+", default=DEFAULT)
    a = ap.parse_args()
    since_ms = int(datetime.strptime(a.since, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    s = requests.Session()
    for sym in a.symbols:
        rows, err = fetch(sym, since_ms, s)
        if err:
            print(f"{sym}: {err}", flush=True)
            if not rows:
                continue
        seen, res = set(), []
        for r in rows:
            if r[0] not in seen:
                seen.add(r[0])
                res.append(r)
        res.sort()
        p = ROOT / "data" / "history" / f"{sym}_60.csv"
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["ts", "open", "high", "low", "close", "volume", "turnover"])
            w.writerows(res)
        print(f"{sym}: {len(res)} свечей, {datetime.fromtimestamp(res[0][0] / 1000, timezone.utc).date()} — {datetime.fromtimestamp(res[-1][0] / 1000, timezone.utc).date()}", flush=True)


if __name__ == "__main__":
    main()
