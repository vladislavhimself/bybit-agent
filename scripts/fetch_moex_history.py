"""Часовая и дневная история Мосбиржи с ISS (бесплатно) -> data/history/<TICKER>_60.csv / _D.csv.
Акции с доски TQBR, фьючерсы по базовому активу (склейка контрактов по дате экспирации не делается: берётся
история конкретного контракта). Пример: python scripts/fetch_moex_history.py SBER GAZP LKOH --days 730"""
import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.config import DATA  # noqa: E402

ISS = "https://iss.moex.com/iss"


def board(ticker):
    if ticker.endswith("F") and ticker not in ("AFLT",) or len(ticker) == 4 and ticker[-2:].isalnum() and ticker[2].isalpha() and ticker[3].isdigit():
        pass
    return None


def fetch(ticker, interval, days, futures=False):
    url = (f"{ISS}/engines/futures/markets/forts/boards/RFUD/securities/{ticker}/candles.json" if futures
           else f"{ISS}/engines/stock/markets/shares/boards/TQBR/securities/{ticker}/candles.json")
    start = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    frames, begin = [], 0
    while True:
        r = requests.get(url, params={"interval": interval, "from": start, "start": begin, "iss.meta": "off"}, timeout=30).json()
        cols, rows = r["candles"]["columns"], r["candles"]["data"]
        if not rows:
            break
        df = pd.DataFrame(rows, columns=cols)
        frames.append(df)
        begin += len(rows)
        if len(rows) < 500:
            break
        time.sleep(0.2)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames)
    # ISS отдаёт время по Москве: переводим в UTC-миллисекунды, как у Bybit
    ts = (pd.to_datetime(df["begin"]) - pd.Timedelta(hours=3)).astype("int64") // 10**6
    out = pd.DataFrame({"ts": ts, "open": df["open"], "high": df["high"], "low": df["low"], "close": df["close"],
                        "volume": df["volume"], "turnover": df["value"]})
    return out.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="+")
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--futures", action="store_true", help="тикеры фьючерсов (SRZ6, MXZ6, SBERF ...)")
    a = ap.parse_args()
    out_dir = DATA / "history"
    out_dir.mkdir(exist_ok=True)
    for t in a.tickers:
        for iv, suffix in ((60, "60"), (24, "D")):
            df = fetch(t, iv, a.days, a.futures)
            path = out_dir / f"{t}_{suffix}.csv"
            df.to_csv(path, index=False)
            print(f"{t} {suffix}: {len(df)} свечей -> {path.name}")
