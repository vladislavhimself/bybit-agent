"""Исследование реакции российского рынка на события по часовой истории Мосбиржи (data/history):
1) решения ЦБ РФ по ключевой ставке — даты и значения с cbr.ru (hd_base/KeyRate), реакция индекса и банков
   по типу решения (повышение / сохранение / снижение) и по неожиданности (изменение против предыдущего шага);
2) известные шоки (вторжение, мобилизация, отмена дивидендов Газпрома, мятеж, санкции на Мосбиржу, мирные
   заголовки) — глубина первого движения, время до дна/пика, откат через 1 и 3 дня.
Результат: data/moex_playbook.json и data/moex_playbook.txt — текст для контекста аналитика (что обычно происходит
после таких событий), плюс печать таблиц. Запуск: python research/moex_events.py data/history"""
import io
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests

sys.stdout.reconfigure(encoding="utf-8")
H = sys.argv[1] if len(sys.argv) > 1 else "data/history"
MSK = timezone(timedelta(hours=3))
OUT_JSON, OUT_TXT = "data/moex_playbook.json", "data/moex_playbook.txt"

# Шоки: (дата-время МСК первой реакции рынка, название, тип, ожидаемый знак)
SHOCKS = [
    ("2022-02-24 09:00", "Начало военной операции", "geopolitics_shock", -1),
    ("2022-04-05 09:00", "Пакет санкций ЕС/США, Бучa", "sanctions", -1),
    ("2022-06-30 12:00", "Газпром отказался от дивидендов за 2021", "dividend_cancel", -1),
    ("2022-08-30 12:00", "Газпром объявил промежуточные дивиденды 51 руб", "dividend_surprise_plus", 1),
    ("2022-09-21 09:00", "Объявление мобилизации", "geopolitics_shock", -1),
    ("2022-12-05 09:00", "Потолок цен на нефть и эмбарго ЕС", "sanctions", -1),
    ("2023-06-26 09:00", "Мятеж ЧВК (реакция в понедельник 26.06)", "geopolitics_shock", -1),
    ("2023-08-15 10:00", "Внеплановое повышение ставки до 12% (рубль 100)", "cbr_emergency_hike", -1),
    ("2024-06-13 09:00", "Санкции США на Мосбиржу, остановка торгов долларом", "sanctions", -1),
    ("2024-11-19 09:00", "Обновление ядерной доктрины, удары ATACMS", "geopolitics_shock", -1),
    ("2025-02-12 18:00", "Разговор Трамп–Путин, старт переговоров", "peace_talks", 1),
    ("2025-02-18 09:00", "Переговоры в Эр-Рияде", "peace_talks", 1),
    ("2025-04-28 09:00", "Пасхальное перемирие, ожидания сделки", "peace_talks", 1),
]


def load(sym):
    p = os.path.join(H, f"{sym}_60.csv")
    if not os.path.exists(p):
        return None
    df = pd.read_csv(p)
    df["t"] = pd.to_datetime(df.ts, unit="s", utc=True).dt.tz_convert(MSK)
    return df.set_index("t")


def reaction(df, t0, horizons=(1, 4, 8, 24, 72)):
    """Доходности от последней цены до события к ценам через N часов торговли, плюс экстремумы."""
    if df is None:
        return None
    before = df[df.index < t0]
    after = df[df.index >= t0]
    if len(before) < 5 or len(after) < 10:
        return None
    p0 = before.close.iloc[-1]
    out = {}
    for h in horizons:
        seg = after.iloc[:h]
        out[f"ret_{h}h"] = round((seg.close.iloc[-1] / p0 - 1) * 100, 2)
    seg = after.iloc[:24]
    out["min_24h"] = round((seg.low.min() / p0 - 1) * 100, 2)
    out["max_24h"] = round((seg.high.max() / p0 - 1) * 100, 2)
    out["t_min_h"] = int(np.argmin(seg.low.values)) + 1
    out["t_max_h"] = int(np.argmax(seg.high.values)) + 1
    return out


def cbr_decisions():
    """История ключевой ставки с cbr.ru: дата вступления и значение. Решение объявляется в пятницу в 13:30 МСК,
    вступает в понедельник, поэтому дату решения берём как последний рабочий день перед вступлением."""
    try:
        r = requests.get("https://www.cbr.ru/hd_base/KeyRate/", params={"UniDbQuery.Posted": "True", "UniDbQuery.From": "01.01.2019",
                                                                        "UniDbQuery.To": datetime.now().strftime("%d.%m.%Y")},
                         timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        tables = pd.read_html(io.StringIO(r.text), decimal=",", thousands=" ")
        t = max(tables, key=len)
        t.columns = ["date", "rate"][:len(t.columns)] if len(t.columns) >= 2 else t.columns
        t = t.iloc[:, :2]
        t.columns = ["date", "rate"]
        t["date"] = pd.to_datetime(t["date"], dayfirst=True)
        t["rate"] = t["rate"].astype(str).str.replace(",", ".").astype(float)
        t = t.sort_values("date").drop_duplicates("rate", keep="first") if False else t.sort_values("date")
        # оставляем только даты, где ставка изменилась или это первый день периода
        rows, prev = [], None
        for _, row in t.iterrows():
            if prev is None or abs(row.rate - prev) > 1e-9:
                rows.append((row.date, row.rate, prev))
            prev = row.rate
        return rows
    except Exception as e:  # noqa: BLE001
        print("cbr.ru недоступен:", e)
        return []


def main():
    idx = load("IMOEXF") if load("IMOEXF") is not None else load("MOEX")
    sber, gazp, lkoh = load("SBER"), load("GAZP"), load("LKOH")
    # индекс через IMOEXF доступен только с 2024; для событий раньше берём SBER как прокси рынка
    res = {"cbr": [], "shocks": []}

    print("== Решения ЦБ (изменения ставки, с cbr.ru) ==")
    decisions = [(d, r, p) for d, r, p in cbr_decisions() if p is not None and d.year >= 2022]
    # заседания по графику, на которых ставку сохранили (в таблице cbr.ru их нет): дата вступления = понедельник после
    holds = ["2024-02-19", "2024-03-25", "2024-04-29", "2024-06-10", "2024-12-23", "2025-02-17", "2025-03-24", "2025-04-28"]
    rate_by_day = {}
    for d, r, p in cbr_decisions():
        rate_by_day[d] = r
    for h in holds:
        hd = pd.Timestamp(h)
        prev_rates = [r for d, r in sorted(rate_by_day.items()) if d <= hd]
        if prev_rates:
            decisions.append((hd, prev_rates[-1], prev_rates[-1]))
    decisions.sort(key=lambda x: x[0])
    for date, rate, prev in decisions:
        # объявление: предыдущий рабочий день (пт) 13:30 МСК; вступление в силу с понедельника
        ann = date - timedelta(days=1)
        while ann.weekday() >= 5:
            ann -= timedelta(days=1)
        t0 = ann.replace(hour=13, minute=0, tzinfo=MSK)
        kind = "hike" if rate > prev else ("cut" if rate < prev else "hold")
        rs = reaction(sber, t0)
        ri = reaction(idx, t0) if idx is not None else None
        if rs:
            res["cbr"].append({"date": ann.strftime("%Y-%m-%d"), "rate": rate, "prev": prev, "kind": kind, "sber": rs, "index": ri})
            print(f"  {ann.date()} {prev:>5}% -> {rate:>5}% {kind:4s} SBER: 1ч {rs['ret_1h']:+.2f} 4ч {rs['ret_4h']:+.2f} 24ч {rs['ret_24h']:+.2f} 72ч {rs['ret_72h']:+.2f}")
    for kind in ("hike", "hold", "cut"):
        rows = [c["sber"] for c in res["cbr"] if c["kind"] == kind]
        if rows:
            d = pd.DataFrame(rows)
            print(f"  {kind}: n={len(d)}  медианы SBER 1ч {d.ret_1h.median():+.2f}% 4ч {d.ret_4h.median():+.2f}% 24ч {d.ret_24h.median():+.2f}% 72ч {d.ret_72h.median():+.2f}% | доля роста за 24ч {(d.ret_24h > 0).mean() * 100:.0f}%")

    print("\n== Шоки ==")
    for ts, name, kind, sign in SHOCKS:
        t0 = datetime.strptime(ts, "%Y-%m-%d %H:%M").replace(tzinfo=MSK)
        rs = reaction(sber, t0)
        rg = reaction(gazp, t0)
        if not rs:
            continue
        res["shocks"].append({"date": ts, "name": name, "kind": kind, "sign": sign, "sber": rs, "gazp": rg})
        print(f"  {ts[:10]} {name[:45]:45s} SBER 1ч {rs['ret_1h']:+.1f} 4ч {rs['ret_4h']:+.1f} 24ч {rs['ret_24h']:+.1f} 72ч {rs['ret_72h']:+.1f} | дно {rs['min_24h']:+.1f} через {rs['t_min_h']}ч, пик {rs['max_24h']:+.1f}")
    by = {}
    for s in res["shocks"]:
        by.setdefault(s["kind"], []).append(s["sber"])
    print("\n== По типам (SBER, медианы) ==")
    summary = {}
    for kind, rows in by.items():
        d = pd.DataFrame(rows)
        summary[kind] = {"n": len(d), "ret_1h": round(d.ret_1h.median(), 2), "ret_4h": round(d.ret_4h.median(), 2),
                         "ret_24h": round(d.ret_24h.median(), 2), "ret_72h": round(d.ret_72h.median(), 2),
                         "extreme_24h": round((d.min_24h if d.ret_4h.median() < 0 else d.max_24h).median(), 2),
                         "t_extreme_h": int((d.t_min_h if d.ret_4h.median() < 0 else d.t_max_h).median())}
        print(f"  {kind:24s} n={len(d)} 1ч {summary[kind]['ret_1h']:+.2f} 4ч {summary[kind]['ret_4h']:+.2f} 24ч {summary[kind]['ret_24h']:+.2f} 72ч {summary[kind]['ret_72h']:+.2f} экстремум {summary[kind]['extreme_24h']:+.2f} через {summary[kind]['t_extreme_h']}ч")
    res["summary"] = summary
    cbr_sum = {}
    for kind in ("hike", "hold", "cut"):
        rows = [c["sber"] for c in res["cbr"] if c["kind"] == kind]
        if rows:
            d = pd.DataFrame(rows)
            cbr_sum[kind] = {"n": len(d), "ret_1h": round(d.ret_1h.median(), 2), "ret_4h": round(d.ret_4h.median(), 2),
                             "ret_24h": round(d.ret_24h.median(), 2), "ret_72h": round(d.ret_72h.median(), 2), "up_24h": round((d.ret_24h > 0).mean(), 2)}
    res["cbr_summary"] = cbr_sum
    os.makedirs("data", exist_ok=True)
    json.dump(res, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    lines = ["Историческая реакция российского рынка (SBER как прокси, медианы по часовой истории 2022-2026):"]
    names = {"geopolitics_shock": "геополитический шок (вторжение, мобилизация, мятеж, эскалация)", "sanctions": "крупные санкции",
             "dividend_cancel": "отказ от дивидендов крупного эмитента", "dividend_surprise_plus": "неожиданные дивиденды",
             "cbr_emergency_hike": "внеплановое повышение ставки", "peace_talks": "заголовки о мирных переговорах"}
    for kind, s in summary.items():
        lines.append(f"- {names.get(kind, kind)} (n={s['n']}): через 1ч {s['ret_1h']:+.1f}%, 4ч {s['ret_4h']:+.1f}%, 24ч {s['ret_24h']:+.1f}%, "
                     f"72ч {s['ret_72h']:+.1f}%; экстремум {s['extreme_24h']:+.1f}% примерно через {s['t_extreme_h']}ч")
    for kind, s in cbr_sum.items():
        kname = {"hike": "повышение", "cut": "снижение", "hold": "сохранение"}[kind]
        lines.append(f"- решение ЦБ: {kname} ставки (n={s['n']}): SBER через 1ч {s['ret_1h']:+.1f}%, 4ч {s['ret_4h']:+.1f}%, "
                     f"24ч {s['ret_24h']:+.1f}%, 72ч {s['ret_72h']:+.1f}%; рост за сутки в {s['up_24h'] * 100:.0f}% случаев")
    lines.append("Вывод для сделок: первая реакция на шок обычно не последняя — экстремум приходит через несколько часов; "
                 "мирные заголовки без документов дают короткий импульс и откат (через сутки рынок ниже); снижение ставки чаще продают "
                 "(ожидаемое событие, 'продавай новость'), сохранение и повышение сами по себе двигают мало — движет расхождение с ожиданиями.")
    open(OUT_TXT, "w", encoding="utf-8").write("\n".join(lines))
    print("\n" + "\n".join(lines))


if __name__ == "__main__":
    main()
