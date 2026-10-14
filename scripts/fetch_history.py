"""Выгрузка истории свечей с Bybit в data/history/<SYMBOL>_<interval>.csv.
Пример: python scripts/fetch_history.py BTCUSDT ETHUSDT SOLUSDT --interval 60 --days 730"""
import argparse
import sys
import time
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.config import DATA, Settings  # noqa: E402
from bot.exchange import Exchange  # noqa: E402

INTERVAL_MS = {"1": 60_000, "5": 300_000, "15": 900_000, "60": 3_600_000, "240": 14_400_000, "D": 86_400_000}


def fetch(ex, symbol, interval, days):
    end = int(time.time() * 1000)
    start = end - days * 86_400_000
    frames = []
    while end > start:
        df = ex.klines(symbol, interval, 1000, end=end)
        if df.empty:
            break
        frames.append(df)
        oldest = int(df["ts"].iloc[0])
        if oldest <= start or len(df) < 1000:
            break
        end = oldest - 1
        time.sleep(0.15)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    return out[out["ts"] >= start]


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="+")
    ap.add_argument("--interval", default="60")
    ap.add_argument("--days", type=int, default=730)
    a = ap.parse_args()
    ex = Exchange(Settings())
    out_dir = DATA / "history"
    out_dir.mkdir(exist_ok=True)
    for sym in a.symbols:
        df = fetch(ex, sym, a.interval, a.days)
        path = out_dir / f"{sym}_{a.interval}.csv"
        df.to_csv(path, index=False)
        print(f"{sym}: {len(df)} свечей -> {path}")
