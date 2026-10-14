"""Проверка классических правил теханализа на 50 годах S&P 500 и 15 годах BTC (дневные данные).
Для каждого правила: сколько раз сработало, средняя и медианная доходность через 5 и 20 дней, доля положительных,
и то же для базы (любой день). Результат -> data/ta_rules.json, краткий текст идёт в контекст модели.
Запуск: python research/ta_rules_50y.py"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.config import DATA  # noqa: E402
from bot.ta import rsi  # noqa: E402


def load(name):
    p = DATA / "history" / f"{name}_D.csv"
    return pd.read_csv(p, parse_dates=["date"]).set_index("date").sort_index() if p.exists() else None


def rules(df):
    c = df["close"]
    sma50, sma200 = c.rolling(50).mean(), c.rolling(200).mean()
    r = rsi(c, 14)
    hi20 = c.rolling(20).max().shift(1)
    lo20 = c.rolling(20).min().shift(1)
    ret1 = c.pct_change()
    vol20 = ret1.rolling(20).std()
    cond = {
        "тренд: цена выше SMA200 и SMA50>SMA200": (c > sma200) & (sma50 > sma200),
        "антитренд: цена ниже SMA200 и SMA50<SMA200": (c < sma200) & (sma50 < sma200),
        "пробой 20-дневного максимума": c > hi20,
        "пробой 20-дневного минимума": c < lo20,
        "пробой максимума в тренде (SMA50>SMA200)": (c > hi20) & (sma50 > sma200),
        "пробой минимума против тренда (SMA50>SMA200)": (c < lo20) & (sma50 > sma200),
        "RSI14 < 30": r < 30,
        "RSI14 > 70": r > 70,
        "RSI14 < 30 в тренде вверх": (r < 30) & (sma50 > sma200),
        "день падения > 5%": ret1 < -0.05,
        "день роста > 5%": ret1 > 0.05,
        "3 дня падения подряд": (ret1 < 0) & (ret1.shift(1) < 0) & (ret1.shift(2) < 0),
        "волатильность в верхних 10% (20д)": vol20 > vol20.rolling(250).quantile(0.9),
        "золотой крест (SMA50 пересекла SMA200 вверх)": (sma50 > sma200) & (sma50.shift(1) <= sma200.shift(1)),
        "крест смерти (SMA50 пересекла SMA200 вниз)": (sma50 < sma200) & (sma50.shift(1) >= sma200.shift(1)),
    }
    fwd5 = c.shift(-5) / c - 1
    fwd20 = c.shift(-20) / c - 1
    base = {"n": int(fwd5.notna().sum()), "mean5": float(fwd5.mean() * 100), "mean20": float(fwd20.mean() * 100),
            "hit5": float((fwd5 > 0).mean()), "hit20": float((fwd20 > 0).mean())}
    out = {}
    for name, mask in cond.items():
        m = mask.fillna(False) & fwd5.notna()
        if m.sum() < 15:
            continue
        f5, f20 = fwd5[m], fwd20[m]
        out[name] = {"n": int(m.sum()), "mean5": float(f5.mean() * 100), "med5": float(f5.median() * 100),
                     "hit5": float((f5 > 0).mean()), "mean20": float(f20.mean() * 100), "med20": float(f20.median() * 100),
                     "hit20": float((f20 > 0).mean()),
                     "edge5": float((f5.mean() - fwd5.mean()) * 100), "edge20": float((f20.mean() - fwd20.mean()) * 100)}
    return base, out


def main():
    result = {"built": datetime.now(timezone.utc).isoformat()}
    for name, label in (("SPX", "S&P 500 1970-2026"), ("BTC", "BTC 2011-2026")):
        df = load(name)
        if df is None:
            print(f"{name}: нет данных, сначала research/fetch_macro_history.py")
            continue
        base, out = rules(df)
        result[name] = {"label": label, "base": base, "rules": out}
        print(f"\n== {label}: база {base['n']} дней, средн. 5д {base['mean5']:+.2f}% (рост в {base['hit5'] * 100:.0f}%), "
              f"20д {base['mean20']:+.2f}% ({base['hit20'] * 100:.0f}%) ==")
        for k, v in sorted(out.items(), key=lambda kv: -abs(kv[1]["edge20"])):
            print(f"{k:48s} n={v['n']:5d}  5д {v['mean5']:+.2f}% ({v['hit5'] * 100:.0f}%)  "
                  f"20д {v['mean20']:+.2f}% ({v['hit20'] * 100:.0f}%)  преимущество 20д {v['edge20']:+.2f}%")
    (DATA / "ta_rules.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nСохранено: {DATA / 'ta_rules.json'}")


if __name__ == "__main__":
    main()
