"""Выгрузка дневной истории за 50+ лет: S&P 500 (с 1970), VIX (с 1990), индекс доллара (с 1971), золото (с 2000),
BTC дневные с 2011 (Bitstamp) -> data/history/*.csv. Запуск: python research/fetch_macro_history.py"""
import sys
import time
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.config import DATA  # noqa: E402

OUT = DATA / "history"
OUT.mkdir(exist_ok=True)

TICKERS = {"SPX": "^GSPC", "VIX": "^VIX", "DXY": "DX-Y.NYB", "GOLD": "GC=F", "NASDAQ": "^IXIC"}


def fetch_yf():
    for name, tk in TICKERS.items():
        df = yf.download(tk, start="1970-01-01", progress=False, auto_adjust=True)
        if df.empty:
            print(f"{name}: пусто")
            continue
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] for c in df.columns]
        df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
        df.index.name = "date"
        df.to_csv(OUT / f"{name}_D.csv")
        print(f"{name}: {len(df)} дней, {df.index.min().date()} -> {df.index.max().date()}")


def fetch_btc_daily():
    frames, start = [], 1313000000
    while True:
        r = requests.get("https://www.bitstamp.net/api/v2/ohlc/btcusd/",
                         params={"step": 86400, "limit": 1000, "start": start}, timeout=30)
        rows = r.json()["data"]["ohlc"]
        if not rows:
            break
        df = pd.DataFrame(rows).astype(float)
        frames.append(df)
        last = int(df["timestamp"].iloc[-1])
        if last >= time.time() - 86400 or last + 86400 <= start:
            break
        start = last + 86400
        time.sleep(0.3)
    df = pd.concat(frames).drop_duplicates("timestamp").sort_values("timestamp")
    df["date"] = pd.to_datetime(df["timestamp"], unit="s").dt.date
    df = df.set_index("date")[["open", "high", "low", "close", "volume"]]
    df.to_csv(OUT / "BTC_D.csv")
    print(f"BTC: {len(df)} дней, {df.index.min()} -> {df.index.max()}")


if __name__ == "__main__":
    fetch_yf()
    fetch_btc_daily()
