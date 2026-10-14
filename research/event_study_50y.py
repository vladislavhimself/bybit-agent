"""Событийное исследование за 50 лет: реакция S&P 500 (с 1973) и BTC (с 2011) на крупные события по типам.
Горизонты: 1, 5, 20 торговых дней. Результат -> data/playbook_stocks.json (подмешивается в контекст модели).
Список событий составлен по памяти модели; даты сверены по возможности, но могут содержать ошибки на день.
Запуск: python research/event_study_50y.py (сначала research/fetch_macro_history.py)"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bot.config import DATA  # noqa: E402

# (дата, тип, ожидаемое направление, заголовок). Дата = первый торговый день, когда рынок мог отреагировать.
STOCK_EVENTS = [
    ("1973-10-17", "macro", "bearish", "Нефтяное эмбарго ОПЕК"),
    ("1979-10-08", "macro", "bearish", "Волкер: резкое ужесточение ФРС"),
    ("1982-08-17", "macro", "bullish", "Разворот ФРС к смягчению, старт бычьего рынка"),
    ("1987-10-19", "collapse", "bearish", "Чёрный понедельник"),
    ("1989-10-13", "collapse", "bearish", "Мини-крах (срыв сделки UAL)"),
    ("1990-08-02", "macro", "bearish", "Ирак вторгается в Кувейт"),
    ("1991-01-17", "macro", "bullish", "Начало войны в Заливе"),
    ("1994-02-04", "macro", "bearish", "Неожиданное повышение ставки ФРС"),
    ("1997-10-27", "collapse", "bearish", "Азиатский кризис, мини-крах"),
    ("1998-08-17", "collapse", "bearish", "Дефолт России"),
    ("1998-09-29", "macro", "bullish", "ФРС снижает ставку (LTCM)"),
    ("2001-01-03", "macro", "bullish", "Внеплановое снижение ставки ФРС на 50 б.п."),
    ("2001-09-17", "macro", "bearish", "Открытие рынка после 11 сентября"),
    ("2002-07-22", "collapse", "bearish", "Банкротство WorldCom"),
    ("2003-03-20", "macro", "bullish", "Начало войны в Ираке"),
    ("2007-08-09", "collapse", "bearish", "BNP Paribas замораживает фонды"),
    ("2008-03-17", "collapse", "bearish", "Крах Bear Stearns"),
    ("2008-09-15", "collapse", "bearish", "Банкротство Lehman Brothers"),
    ("2008-09-29", "regulation", "bearish", "Конгресс отклоняет TARP"),
    ("2008-10-13", "macro", "bullish", "Рекапитализация банков, скоординированные меры"),
    ("2008-11-25", "macro", "bullish", "Объявление QE1"),
    ("2010-05-06", "collapse", "bearish", "Flash crash"),
    ("2010-11-03", "macro", "bullish", "Объявление QE2"),
    ("2011-08-08", "regulation", "bearish", "S&P понижает рейтинг США"),
    ("2012-09-13", "macro", "bullish", "Объявление QE3"),
    ("2013-05-22", "macro", "bearish", "Taper tantrum (Бернанке)"),
    ("2013-10-17", "macro", "bullish", "Сделка по потолку долга"),
    ("2015-08-11", "macro", "bearish", "Китай девальвирует юань"),
    ("2015-08-24", "collapse", "bearish", "Китайский чёрный понедельник"),
    ("2015-12-16", "macro", "bearish", "Первое повышение ставки ФРС с 2006"),
    ("2016-06-24", "macro", "bearish", "Итоги референдума Brexit"),
    ("2016-11-09", "macro", "bullish", "Избрание Трампа"),
    ("2018-02-05", "collapse", "bearish", "Volmageddon"),
    ("2018-03-22", "macro", "bearish", "Тарифы Трампа против Китая"),
    ("2018-12-19", "macro", "bearish", "Ястребиное повышение ФРС"),
    ("2019-01-04", "macro", "bullish", "Разворот Пауэлла"),
    ("2019-08-05", "macro", "bearish", "Юань выше 7, эскалация торговой войны"),
    ("2020-03-03", "macro", "bullish", "Экстренное снижение ставки ФРС"),
    ("2020-03-09", "collapse", "bearish", "COVID-крах + нефтяная война"),
    ("2020-03-16", "macro", "bullish", "ФРС: ставка 0 и QE"),
    ("2020-03-23", "macro", "bullish", "ФРС: безлимитное QE"),
    ("2020-11-09", "macro", "bullish", "Вакцина Pfizer"),
    ("2021-11-10", "macro", "bearish", "CPI 6.2%"),
    ("2022-06-10", "macro", "bearish", "CPI 8.6%"),
    ("2022-06-15", "macro", "bearish", "ФРС +75 б.п."),
    ("2022-11-10", "macro", "bullish", "CPI замедляется"),
    ("2023-03-10", "collapse", "bearish", "Крах SVB"),
    ("2024-08-05", "collapse", "bearish", "Разворот carry trade в Японии"),
    ("2025-04-03", "macro", "bearish", "Тарифы 'Liberation Day'"),
    ("2025-04-09", "macro", "bullish", "Пауза по тарифам на 90 дней"),
]

# BTC с 2011 (дневные данные Bitstamp), события до появления Bybit-истории
BTC_EVENTS = [
    ("2013-03-16", "macro", "bullish", "Кипрский банковский кризис"),
    ("2013-12-05", "regulation", "bearish", "НБ Китая запрещает банкам работать с BTC"),
    ("2014-02-07", "collapse", "bearish", "Mt.Gox останавливает вывод"),
    ("2014-02-24", "collapse", "bearish", "Mt.Gox закрывается"),
    ("2015-01-05", "hack", "bearish", "Взлом Bitstamp"),
    ("2016-07-09", "upgrade", "bullish", "BTC halving #2"),
    ("2016-08-02", "hack", "bearish", "Взлом Bitfinex"),
    ("2017-03-10", "etf", "bearish", "SEC отклоняет ETF Winklevoss"),
    ("2017-08-01", "upgrade", "neutral", "Хардфорк Bitcoin Cash"),
    ("2017-09-04", "regulation", "bearish", "Китай запрещает ICO"),
    ("2017-09-14", "regulation", "bearish", "Китай закрывает биржи"),
    ("2017-12-10", "listing", "bullish", "Запуск фьючерсов CBOE"),
    ("2017-12-18", "listing", "bullish", "Запуск фьючерсов CME"),
    ("2018-01-26", "hack", "bearish", "Взлом Coincheck"),
    ("2018-03-07", "hack", "bearish", "Инцидент Binance (API)"),
    ("2018-11-15", "upgrade", "bearish", "Хардфорк BCH, 'hash war'"),
    ("2019-04-02", "other", "bullish", "Внезапный рост на 20% (апрель 2019)"),
    ("2019-05-08", "hack", "bearish", "Взлом Binance на 7000 BTC"),
    ("2019-06-18", "partnership", "bullish", "Facebook анонсирует Libra"),
    ("2019-09-23", "listing", "bearish", "Запуск Bakkt (разочарование объёмами)"),
    ("2020-03-12", "collapse", "bearish", "Чёрный четверг COVID"),
    ("2020-05-11", "upgrade", "bullish", "BTC halving #3"),
]

HORIZONS = {"1d": 1, "5d": 5, "20d": 20}


def load(name):
    p = DATA / "history" / f"{name}_D.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p, parse_dates=["date"]).set_index("date").sort_index()
    return df


def study(df, events, label):
    rows = []
    for d, typ, direction, title in events:
        dt = pd.Timestamp(d)
        idx = df.index.searchsorted(dt)
        if idx <= 0 or idx >= len(df):
            continue
        prev_close = float(df["close"].iloc[idx - 1])
        rec = {"date": d, "type": typ, "direction": direction, "title": title, "market": label}
        ok = True
        for name, h in HORIZONS.items():
            j = idx + h - 1
            if j >= len(df):
                ok = False
                break
            rec[name] = (float(df["close"].iloc[j]) / prev_close - 1) * 100
        if ok:
            rows.append(rec)
    return rows


def stats(rows):
    t = pd.DataFrame(rows)
    t["sign"] = np.where(t["direction"] == "bullish", 1, np.where(t["direction"] == "bearish", -1, 0))
    out = {}
    for (typ, d), g in t.groupby(["type", "direction"]):
        out[f"{typ}:{d}"] = {
            "n": int(len(g)),
            "med_1d": float(g["1d"].median()), "med_5d": float(g["5d"].median()), "med_20d": float(g["20d"].median()),
            "mean_abs_1d": float(g["1d"].abs().mean()),
            "hit_1d": float(((g["1d"] * g["sign"]) > 0).mean()) if g["sign"].abs().sum() else 0.0,
            "hit_5d": float(((g["5d"] * g["sign"]) > 0).mean()) if g["sign"].abs().sum() else 0.0,
            "hit_20d": float(((g["20d"] * g["sign"]) > 0).mean()) if g["sign"].abs().sum() else 0.0,
        }
    return out


def main():
    spx, btc = load("SPX"), load("BTC")
    if spx is None or btc is None:
        raise SystemExit("Нет данных: сначала python research/fetch_macro_history.py")
    s_rows = study(spx, STOCK_EVENTS, "SPX")
    b_rows = study(btc, BTC_EVENTS, "BTC")
    out = {"built": datetime.now(timezone.utc).isoformat(),
           "spx": {"events": s_rows, "stats": stats(s_rows)},
           "btc_daily": {"events": b_rows, "stats": stats(b_rows)}}
    (DATA / "playbook_stocks.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    pd.set_option("display.width", 220)
    for label, rows in (("S&P 500 (1973-2025)", s_rows), ("BTC дневные (2013-2020)", b_rows)):
        print(f"\n== {label}: {len(rows)} событий ==")
        print(pd.DataFrame(rows)[["date", "type", "direction", "title", "1d", "5d", "20d"]].round(1).to_string(index=False))
        print("-- по типам --")
        for k, v in sorted(stats(rows).items(), key=lambda kv: -kv[1]["n"]):
            print(f"{k:20s} n={v['n']:2d}  медиана 1д {v['med_1d']:+5.1f}  5д {v['med_5d']:+5.1f}  20д {v['med_20d']:+5.1f}"
                  f"  отработало 1д {v['hit_1d'] * 100:3.0f}%  5д {v['hit_5d'] * 100:3.0f}%  20д {v['hit_20d'] * 100:3.0f}%")
    print(f"\nСохранено: {DATA / 'playbook_stocks.json'}")


if __name__ == "__main__":
    main()
