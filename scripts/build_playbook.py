"""Событийное исследование: как BTC и ETH реагировали на крупные события 2020-2025.
Берёт список событий ниже (дата/час UTC, тип, ожидаемое направление) и часовые свечи из data/history,
считает доходность через 1ч, 4ч, 24ч, 72ч после события и сводную статистику по типам -> data/playbook.json.

Список событий составлен по памяти модели, время некоторых событий приблизительное (помечено approx).
Его нужно расширять и проверять. Запуск: python scripts/build_playbook.py"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.config import DATA  # noqa: E402

# (UTC время, тип, ожидаемое направление, заголовок, затронутые)
EVENTS = [
    ("2020-05-11 19:00", "upgrade", "bullish", "BTC halving #3", ["BTC"]),
    ("2020-10-08 12:00", "whale", "bullish", "Square покупает BTC на $50M", ["BTC"]),
    ("2020-10-21 13:00", "partnership", "bullish", "PayPal запускает покупку крипты", ["BTC"]),
    ("2021-02-08 13:00", "whale", "bullish", "Tesla купила BTC на $1.5B", ["BTC"]),
    ("2021-04-14 17:00", "listing", "bullish", "Прямой листинг Coinbase (approx)", ["BTC"]),
    ("2021-05-12 22:00", "regulation", "bearish", "Tesla перестаёт принимать BTC", ["BTC"]),
    ("2021-05-18 14:00", "regulation", "bearish", "Китай запрещает банкам крипто-услуги (approx)", ["BTC"]),
    ("2021-06-09 04:00", "regulation", "bullish", "Сальвадор: BTC законное платёжное средство (approx)", ["BTC"]),
    ("2021-09-24 09:00", "regulation", "bearish", "НБ Китая: все крипто-операции незаконны", ["BTC"]),
    ("2021-10-19 13:30", "etf", "bullish", "Запуск первого фьючерсного BTC ETF (ProShares)", ["BTC"]),
    ("2022-01-05 19:00", "macro", "bearish", "Протокол FOMC: ястребиный (approx)", ["BTC"]),
    ("2022-02-24 03:00", "macro", "bearish", "Вторжение России в Украину", ["BTC"]),
    ("2022-05-09 22:00", "collapse", "bearish", "Депег UST / крах Terra (approx)", ["BTC", "ETH"]),
    ("2022-06-12 22:00", "collapse", "bearish", "Celsius останавливает вывод", ["BTC", "ETH"]),
    ("2022-06-15 18:00", "macro", "bearish", "ФРС +75 б.п.", ["BTC"]),
    ("2022-09-15 06:45", "upgrade", "bullish", "Ethereum Merge", ["ETH"]),
    ("2022-11-08 16:00", "collapse", "bearish", "FTX останавливает вывод, Binance LOI (approx)", ["BTC", "ETH"]),
    ("2022-11-11 14:00", "collapse", "bearish", "FTX подаёт на банкротство", ["BTC", "ETH"]),
    ("2023-01-12 13:30", "macro", "bullish", "CPI США замедляется", ["BTC"]),
    ("2023-03-10 21:00", "collapse", "bearish", "Крах SVB, депег USDC (approx)", ["BTC", "ETH"]),
    ("2023-03-12 22:00", "macro", "bullish", "ФРС/Казначейство гарантируют вклады SVB", ["BTC"]),
    ("2023-06-05 15:00", "legal", "bearish", "SEC подаёт иск к Binance (approx)", ["BTC"]),
    ("2023-06-06 14:00", "legal", "bearish", "SEC подаёт иск к Coinbase (approx)", ["BTC"]),
    ("2023-06-15 20:00", "etf", "bullish", "BlackRock подаёт заявку на спот BTC ETF (approx)", ["BTC"]),
    ("2023-08-29 14:00", "legal", "bullish", "Grayscale выигрывает у SEC", ["BTC"]),
    ("2023-10-16 13:20", "etf", "bullish", "Фейк об одобрении ETF (Cointelegraph)", ["BTC"]),
    ("2023-11-21 20:00", "legal", "bearish", "Binance/CZ: сделка с DOJ", ["BTC"]),
    ("2024-01-09 21:10", "etf", "bullish", "Взлом аккаунта SEC в X: фейк одобрения ETF", ["BTC"]),
    ("2024-01-10 21:30", "etf", "bullish", "Одобрение спот BTC ETF", ["BTC"]),
    ("2024-04-20 00:10", "upgrade", "bullish", "BTC halving #4", ["BTC"]),
    ("2024-05-23 21:00", "etf", "bullish", "Одобрение 19b-4 по спот ETH ETF (approx)", ["ETH"]),
    ("2024-08-05 00:00", "macro", "bearish", "Крах carry trade в Японии", ["BTC", "ETH"]),
    ("2024-09-18 18:00", "macro", "bullish", "Первое снижение ставки ФРС на 50 б.п.", ["BTC"]),
    ("2024-11-06 06:00", "macro", "bullish", "Победа Трампа на выборах (approx)", ["BTC"]),
    ("2024-12-18 19:00", "macro", "bearish", "Ястребиное снижение ФРС", ["BTC"]),
    ("2025-02-21 15:00", "hack", "bearish", "Взлом Bybit на $1.5B (approx)", ["BTC", "ETH"]),
    ("2025-03-07 02:00", "regulation", "bullish", "Указ о стратегическом резерве BTC (approx)", ["BTC"]),
    ("2025-04-02 20:00", "macro", "bearish", "Тарифы 'Liberation Day'", ["BTC"]),
    ("2025-04-09 17:20", "macro", "bullish", "Пауза по тарифам на 90 дней", ["BTC"]),
    ("2025-10-10 20:00", "macro", "bearish", "Тарифы 100% на Китай, рекордные ликвидации (approx)", ["BTC", "ETH"]),
]

HORIZONS = {"1h": 1, "4h": 4, "24h": 24, "72h": 72}


def load(symbol):
    p = DATA / "history" / f"{symbol}_60.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p)
    df["ts"] = df["ts"].astype("int64")
    return df.set_index("ts")


def main():
    hist = {s: load(s) for s in ("BTCUSDT", "ETHUSDT")}
    rows = []
    for dt, typ, direction, title, syms in EVENTS:
        ts = int(datetime.strptime(dt, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()) * 1000
        ts = ts - ts % 3_600_000
        for sym in ("BTCUSDT", "ETHUSDT"):
            df = hist.get(sym)
            if df is None or ts not in df.index:
                continue
            base = df.loc[ts, "open"]  # цена на открытии часа события
            rec = {"ts": dt, "type": typ, "direction": direction, "title": title, "symbol": sym,
                   "direct": sym[:3] in syms}
            ok = True
            for name, h in HORIZONS.items():
                t2 = ts + h * 3_600_000
                if t2 not in df.index:
                    ok = False
                    break
                rec[name] = (df.loc[t2, "close"] / base - 1) * 100
            if ok:
                rows.append(rec)
    if not rows:
        raise SystemExit("Нет данных: сначала python scripts/fetch_history.py BTCUSDT ETHUSDT --days 2400")
    t = pd.DataFrame(rows)
    t["sign"] = np.where(t["direction"] == "bullish", 1, -1)
    btc = t[t.symbol == "BTCUSDT"].copy()
    stats = {}
    for (typ, d), g in btc.groupby(["type", "direction"]):
        stats[f"{typ}:{d}"] = {
            "n": int(len(g)),
            "med_1h": float(g["1h"].median()), "med_4h": float(g["4h"].median()),
            "med_24h": float(g["24h"].median()), "med_72h": float(g["72h"].median()),
            "mean_abs_1h": float(g["1h"].abs().mean()), "mean_abs_24h": float(g["24h"].abs().mean()),
            "hit_4h": float(((g["4h"] * g["sign"]) > 0).mean()),
            "hit_24h": float(((g["24h"] * g["sign"]) > 0).mean()),
            "hit_72h": float(((g["72h"] * g["sign"]) > 0).mean()),
        }
    out = {"built": datetime.now(timezone.utc).isoformat(), "events": rows, "stats": stats}
    (DATA / "playbook.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    pd.set_option("display.width", 200)
    print("Реакция BTC по событиям (%):")
    print(btc[["ts", "type", "direction", "title", "1h", "4h", "24h", "72h"]].round(1).to_string(index=False))
    print("\nСводка по типам (BTC):")
    for k, v in sorted(stats.items(), key=lambda kv: -kv[1]["n"]):
        print(f"{k:22s} n={v['n']:2d}  медиана 4ч {v['med_4h']:+5.1f}  24ч {v['med_24h']:+5.1f}  72ч {v['med_72h']:+5.1f}"
              f"  отработало 4ч {v['hit_4h'] * 100:3.0f}%  24ч {v['hit_24h'] * 100:3.0f}%  72ч {v['hit_72h'] * 100:3.0f}%")
    print(f"\nСохранено: {DATA / 'playbook.json'}")


if __name__ == "__main__":
    main()
