"""Реакция BTC и ETH (часовые данные Bybit) на все решения FOMC 2022-2026: размах и направление через 1/4/24 часа.
Решение публикуется в 14:00 по Нью-Йорку. Результат -> data/fomc_hourly.json (в контекст модели).
Запуск: python research/fomc_hourly.py (нужна история: scripts/fetch_history.py BTCUSDT ETHUSDT --days 2400)"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.config import DATA  # noqa: E402

NY = ZoneInfo("America/New_York")
# Второй день заседания (день решения). Источник: federalreserve.gov, календари FOMC
FOMC = {
    2022: ["01-26", "03-16", "05-04", "06-15", "07-27", "09-21", "11-02", "12-14"],
    2023: ["02-01", "03-22", "05-03", "06-14", "07-26", "09-20", "11-01", "12-13"],
    2024: ["01-31", "03-20", "05-01", "06-12", "07-31", "09-18", "11-07", "12-18"],
    2025: ["01-29", "03-19", "05-07", "06-18", "07-30", "09-17", "10-29", "12-10"],
    2026: ["01-28", "03-18", "04-29", "06-17", "07-29", "09-16"],
}
# Повышения/снижения по памяти модели (для разбивки по типу решения); None = без изменения
DECISION = {
    "2022-03-16": "+25", "2022-05-04": "+50", "2022-06-15": "+75", "2022-07-27": "+75", "2022-09-21": "+75",
    "2022-11-02": "+75", "2022-12-14": "+50", "2023-02-01": "+25", "2023-03-22": "+25", "2023-05-03": "+25",
    "2023-07-26": "+25", "2024-09-18": "-50", "2024-11-07": "-25", "2024-12-18": "-25",
}


def load(sym):
    p = DATA / "history" / f"{sym}_60.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p)
    df["ts"] = df["ts"].astype("int64")
    return df.set_index("ts")


def main():
    hist = {s: load(s) for s in ("BTCUSDT", "ETHUSDT")}
    rows = []
    for year, days in FOMC.items():
        for d in days:
            date = f"{year}-{d}"
            dt = datetime.strptime(date + " 14:00", "%Y-%m-%d %H:%M").replace(tzinfo=NY)
            ts = int(dt.timestamp()) * 1000
            ts -= ts % 3_600_000
            for sym, df in hist.items():
                if df is None or ts not in df.index:
                    continue
                base = df.loc[ts, "open"]
                rec = {"date": date, "symbol": sym, "decision": DECISION.get(date, "0"),
                       "pre_24h": (base / df.loc[ts - 24 * 3_600_000, "close"] - 1) * 100 if ts - 24 * 3_600_000 in df.index else np.nan}
                ok = True
                for name, h in (("1h", 1), ("4h", 4), ("24h", 24), ("72h", 72)):
                    t2 = ts + h * 3_600_000
                    if t2 not in df.index:
                        ok = False
                        break
                    rec[name] = (df.loc[t2, "close"] / base - 1) * 100
                if ok:
                    # размах внутри первых 4 часов
                    seg = df.loc[ts: ts + 4 * 3_600_000]
                    rec["range_4h"] = (seg["high"].max() / seg["low"].min() - 1) * 100
                    rows.append(rec)
    t = pd.DataFrame(rows)
    btc = t[t.symbol == "BTCUSDT"]
    stats = {
        "n": int(len(btc)),
        "mean_abs_1h": float(btc["1h"].abs().mean()), "mean_abs_4h": float(btc["4h"].abs().mean()),
        "mean_abs_24h": float(btc["24h"].abs().mean()), "mean_range_4h": float(btc["range_4h"].mean()),
        "up_share_24h": float((btc["24h"] > 0).mean()),
        "reversal_share": float(((btc["1h"] * btc["24h"]) < 0).mean()),   # движение первого часа развернулось к суткам
        "hikes": {"n": int((btc.decision.str.startswith("+")).sum()),
                  "med_24h": float(btc[btc.decision.str.startswith("+")]["24h"].median())},
        "cuts": {"n": int((btc.decision.str.startswith("-")).sum()),
                 "med_24h": float(btc[btc.decision.str.startswith("-")]["24h"].median()) if (btc.decision.str.startswith("-")).any() else 0.0},
        "holds": {"n": int((btc.decision == "0").sum()), "med_24h": float(btc[btc.decision == "0"]["24h"].median())},
    }
    out = {"built": datetime.now(timezone.utc).isoformat(), "events": rows, "stats": stats}
    (DATA / "fomc_hourly.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    pd.set_option("display.width", 200)
    print(btc[["date", "decision", "pre_24h", "1h", "4h", "24h", "72h", "range_4h"]].round(1).to_string(index=False))
    print(f"\nFOMC x BTC: n={stats['n']}, средний |ход| 1ч {stats['mean_abs_1h']:.1f}%, 4ч {stats['mean_abs_4h']:.1f}%, "
          f"24ч {stats['mean_abs_24h']:.1f}%, размах первых 4ч {stats['mean_range_4h']:.1f}%")
    print(f"рост через 24ч в {stats['up_share_24h'] * 100:.0f}% случаев; движение первого часа развернулось к суткам "
          f"в {stats['reversal_share'] * 100:.0f}% случаев")
    print(f"повышения: n={stats['hikes']['n']}, медиана 24ч {stats['hikes']['med_24h']:+.1f}%; "
          f"снижения: n={stats['cuts']['n']}, {stats['cuts']['med_24h']:+.1f}%; без изменения: n={stats['holds']['n']}, "
          f"{stats['holds']['med_24h']:+.1f}%")
    print(f"\nСохранено: {DATA / 'fomc_hourly.json'}")


if __name__ == "__main__":
    main()
