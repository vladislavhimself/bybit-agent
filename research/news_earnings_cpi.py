"""Ожидание против факта для ЧАСТЫХ событий: отчётности компаний и недельная инфляция. Цель — частота сделок (пользователь: минимум 4 в неделю).

1. Отчётности. Факт расхождения берём не парсингом чисел, а из самих лент: «результаты (выше|лучше|ниже|хуже) (консенсус|ожиданий|прогноз)»
   и «совпали с ожиданиями / в рамках ожиданий». Событие = первое такое сообщение по компании за 5 дней. Реакция бумаги сверх индекса:
   скачок (от цены до новости), 1ч, 4ч, день, 3 дня, неделя, 2 недели; разгон за 10 дней до; до/после 2024.
2. Недельная инфляция (Росстат, среда ~19:00). Факт и «неделей ранее» — из текста. Ожидание = прошлая неделя (консенсуса по недельной
   инфляции в лентах нет), сюрприз = ускорение/замедление ≥ 0.05 п.п. Реакция: RGBI и индекс со следующего открытия (вечером индекс не считается):
   скачок открытия, день, 3 дня, неделя, 2 недели. Отдельно 2024+ (когда ставка была главной темой).
Запуск из корня: python research/news_earnings_cpi.py. Модель не вызывается."""
import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research.news_expectations import company_of, move  # noqa: E402
from research.news_psychology import NAME_RX, load_hist  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
MSK = ZoneInfo("Europe/Moscow")
SPLIT = int(datetime(2024, 1, 1, tzinfo=MSK).timestamp())
BEAT = re.compile(r"(выше|лучше|превысил\w*|опередил\w*|сильнее)\s(\w+\s){0,3}(консенсус|ожидани|прогноз)", re.I)
MISS = re.compile(r"(ниже|хуже|не оправдал\w*|не дотянул\w*|слабее|разочаров\w*)\s(\w+\s){0,3}(консенсус|ожидани|прогноз)", re.I)
INLINE = re.compile(r"(совпал\w*|в рамках|в соответствии|на уровне|в русле)\s(\w+\s){0,2}(консенсус|ожидани|прогноз)", re.I)
REPORT = re.compile(r"отчита|отчёт|отчет|результат|МСФО|РСБУ|прибыл|выручк|EBITDA", re.I)
H = (1, 4, 9, 27, 45, 90)
KEYS = ("jump", "f1", "f4", "day", "f27", "f45", "f90")


def fmt(x):
    return "   —  " if x is None else f"{x:+6.2f}"


def agg(lst, key, rel=True):
    xs = [(d["rel"] if rel else d["mv"])[key] for d in lst if (d["rel"] if rel else d["mv"]).get(key) is not None]
    return (f"{np.mean(xs):+.2f}% ({np.mean([x > 0 for x in xs]):.0%})" if xs else "—")


def earnings(c, hist):
    rows = c.execute("SELECT channel, ts, text FROM msgs WHERE ts>=? AND (text LIKE '%консенсус%' OR text LIKE '%ожидани%' OR text LIKE '%прогноз%') ORDER BY ts",
                     (int(datetime(2019, 1, 1, tzinfo=MSK).timestamp()),)).fetchall()
    last = {}
    out = []
    for ch, ts, t in rows:
        t = t or ""
        head = t[:400]
        if not REPORT.search(head):
            continue
        kind = "лучше ожиданий" if BEAT.search(head) else "хуже ожиданий" if MISS.search(head) else "как ждали" if INLINE.search(head) else None
        if not kind:
            continue
        sym = company_of(t)
        if not sym or sym not in hist:
            continue
        if ts - last.get(sym, 0) < 5 * 86400:
            continue
        last[sym] = ts
        mv = move(hist[sym], ts, horizons=H, back=(27, 90))
        mi = move(hist["IMOEX"], ts, horizons=H, back=(27, 90))
        if not mv or not mi or mv["lag_h"] > 16:
            continue
        rel = {k: mv[k] - mi[k] if mv.get(k) is not None and mi.get(k) is not None else None for k in KEYS + ("b90",)}
        out.append({"ts": ts, "sym": sym, "kind": kind, "ch": ch, "rel": rel, "mv": mv, "half": "до 2024" if ts < SPLIT else "после 2024",
                    "snippet": head[:140].replace("\n", " ")})
    print(f"\n1. ОТЧЁТНОСТИ ПРОТИВ ОЖИДАНИЙ (по формулировкам лент): событий {len(out)} "
          f"(лучше {sum(1 for d in out if d['kind'] == 'лучше ожиданий')}, хуже {sum(1 for d in out if d['kind'] == 'хуже ожиданий')}, как ждали {sum(1 for d in out if d['kind'] == 'как ждали')})")
    print("   бумага сверх индекса, %: средний (доля вверх)")
    print(f"   {'группа':16} {'половина':11} {'n':>4} {'скачок':>14} {'1ч':>14} {'4ч':>14} {'день':>14} {'3д':>14} {'нед':>14} {'2нед':>14}")
    res = {}
    for kind in ("лучше ожиданий", "хуже ожиданий", "как ждали"):
        for half in ("до 2024", "после 2024", "всё"):
            lst = [d for d in out if d["kind"] == kind and (half == "всё" or d["half"] == half)]
            if len(lst) < 10:
                continue
            print(f"   {kind:16} {half:11} {len(lst):4} " + " ".join(f"{agg(lst, k):>14}" for k in KEYS))
            res[f"{kind}|{half}"] = {"n": len(lst), **{k: agg(lst, k) for k in KEYS}}
    print("\n   разгон за 10 дней до отчёта × результат (сверх индекса):")
    for kind in ("лучше ожиданий", "хуже ожиданий", "как ждали"):
        for lab, cond in (("разгон ≥4%", lambda d: d["rel"]["b90"] >= 4), ("без разгона", lambda d: d["rel"]["b90"] < 4), ("падение ≤−4%", lambda d: d["rel"]["b90"] <= -4)):
            lst = [d for d in out if d["kind"] == kind and cond(d)]
            if len(lst) >= 10:
                print(f"     {kind:16} × {lab:12} n={len(lst):3}  день {agg(lst, 'day'):>14}  нед {agg(lst, 'f45'):>14}  2нед {agg(lst, 'f90'):>14}")
    print("\n   вход после первого часа (с конца первого часа до недели / 2 недель), сверх индекса:")
    for kind in ("лучше ожиданий", "хуже ожиданий"):
        lst = [d for d in out if d["kind"] == kind]
        for k, lab in (("f45", "неделя"), ("f90", "2 недели")):
            xs = [d["rel"][k] - d["rel"]["f1"] for d in lst if d["rel"].get(k) is not None and d["rel"].get("f1") is not None]
            print(f"     {kind:16} → {lab:9} n={len(xs):3} {np.mean(xs):+.2f}% (вверх {np.mean([x > 0 for x in xs]):.0%})")
    print("\n   примеры (проверить глазами):")
    for d in out[-12:]:
        print(f"     {datetime.fromtimestamp(d['ts'], MSK):%Y-%m-%d %H:%M} {d['sym']:5} {d['kind']:15} скачок {fmt(d['rel']['jump'])} день {fmt(d['rel']['day'])} нед {fmt(d['rel']['f45'])} | {d['snippet'][:90]}")
    # как сделки: шорт бумаги (не сверх индекса, а сама бумага) после первого часа, комиссия+шорт 0.4% на круг, удержание 2 недели / неделя
    print("\n   КАК СДЕЛКИ (сама бумага, шорт после первого часа, издержки 0.4% на круг, без плеча, сумма % по сделкам):")
    for kind in ("хуже ожиданий", "лучше ожиданий", "как ждали"):
        for k, lab in (("f45", "неделя"), ("f90", "2 недели")):
            tr = [(-(d["mv"][k] - d["mv"]["f1"]) / (1 + d["mv"]["f1"] / 100) - 0.4, d["ts"]) for d in out if d["kind"] == kind and d["mv"].get(k) is not None]
            if len(tr) < 10:
                continue
            pn = np.array([p for p, _ in tr])
            cum = np.cumsum(pn)
            dd = float((np.maximum.accumulate(cum) - cum).max())
            bef = pn[[t < SPLIT for _, t in tr]]
            aft = pn[[t >= SPLIT for _, t in tr]]
            print(f"     шорт после «{kind}», {lab:9} n={len(pn):3} приб. {np.mean(pn > 0):.0%} средняя {pn.mean():+.2f}% сумма {cum[-1]:+.1f}% просадка {dd:.1f}% | "
                  f"до 2024 {bef.sum():+.1f}% ({len(bef)}), после {aft.sum():+.1f}% ({len(aft)})")
    return res


CPI_FACT = re.compile(r"(инфляци\w*|цен\w*)[^.\n]{0,80}?(составил\w*|на уровне|замедлил\w* до|ускорил\w* до|выросл\w* на)\s(\d[.,]\d{1,2})\s?%", re.I)
CPI_PREV = re.compile(r"(неделей ранее|предыдущ\w+ недел\w+|прошл\w+ недел\w+)[^.\n]{0,40}?(\d[.,]\d{1,2})\s?%", re.I)
CPI_PREV2 = re.compile(r"(\d[.,]\d{1,2})\s?%[^.\n]{0,30}?(неделей ранее|предыдущ\w+ недел\w+)", re.I)


def weekly_cpi(c, hist):
    rows = c.execute("SELECT channel, ts, text FROM msgs WHERE ts>=? AND text LIKE '%инфляци%' AND (text LIKE '%неделей ранее%' OR text LIKE '%за неделю%' OR text LIKE '%недельн%') ORDER BY ts",
                     (int(datetime(2019, 1, 1, tzinfo=MSK).timestamp()),)).fetchall()
    events = []
    last_ts = 0
    for ch, ts, t in rows:
        t = (t or "")[:500]
        d = datetime.fromtimestamp(ts, MSK)
        if d.weekday() != 2 or not (18 <= d.hour <= 21):  # среда вечером
            continue
        m = CPI_FACT.search(t)
        p = CPI_PREV.search(t) or CPI_PREV2.search(t)
        if not m or not p:
            continue
        fact = float(m.group(3).replace(",", "."))
        prev = float((p.group(2) if p.re is CPI_PREV else p.group(1)).replace(",", "."))
        if fact > 1.5 or prev > 1.5:  # это годовая, не недельная
            continue
        if ts - last_ts < 3 * 86400:
            continue
        last_ts = ts
        events.append({"ts": ts, "fact": fact, "prev": prev, "delta": round(fact - prev, 2), "ch": ch})
    print(f"\n2. НЕДЕЛЬНАЯ ИНФЛЯЦИЯ (среда вечером): событий {len(events)}, с {datetime.fromtimestamp(events[0]['ts'], MSK).date() if events else '—'}")
    out = []
    for e in events:
        mi = move(hist["IMOEX"], e["ts"], horizons=H, back=(27,))
        mr = move(hist["RGBI"], e["ts"], horizons=H, back=(27,))
        if not mi or not mr:
            continue
        e["imoex"], e["rgbi"] = mi, mr
        e["half"] = "до 2024" if e["ts"] < SPLIT else "после 2024"
        out.append(e)
    print("   реакция со следующего открытия: скачок открытия (к закрытию среды), день, 3 дня, неделя, 2 недели; средний (доля вверх)")
    print(f"   {'группа':34} {'половина':11} {'n':>4} {'инструмент':10} {'скачок':>14} {'день':>14} {'3д':>14} {'нед':>14} {'2нед':>14}")
    res = {}
    for lab, cond in (("ускорение ≥0.05 п.п. (хуже)", lambda e: e["delta"] >= 0.05), ("замедление ≥0.05 п.п. (лучше)", lambda e: e["delta"] <= -0.05),
                      ("без изменений (±0.04)", lambda e: abs(e["delta"]) < 0.05), ("дефляция (факт < 0)", lambda e: e["fact"] < 0)):
        for half in ("всё", "после 2024"):
            lst = [e for e in out if cond(e) and (half == "всё" or e["half"] == half)]
            if len(lst) < 8:
                continue
            for inst in ("imoex", "rgbi"):
                def a(k):
                    xs = [e[inst][k] for e in lst if e[inst].get(k) is not None]
                    return f"{np.mean(xs):+.2f}% ({np.mean([x > 0 for x in xs]):.0%})" if xs else "—"
                print(f"   {lab:34} {half:11} {len(lst):4} {inst.upper():10} {a('jump'):>14} {a('day'):>14} {a('f27'):>14} {a('f45'):>14} {a('f90'):>14}")
                res[f"{lab}|{half}|{inst}"] = {"n": len(lst), "day": a("day"), "f45": a("f45")}
    # подтверждение рынком: знак RGBI в первый час против дальнейшего индекса
    sel = [e for e in out if abs(e["delta"]) >= 0.05 and e["rgbi"].get("f1") is not None and abs(e["rgbi"]["jump"] + e["rgbi"]["f1"]) >= 0.1]
    if sel:
        for k, lab in (("day", "день"), ("f45", "неделя"), ("f90", "2 недели")):
            xs = [(np.sign(e["rgbi"]["jump"] + e["rgbi"]["f1"]), e["imoex"][k] - e["imoex"].get("f1", 0)) for e in sel if e["imoex"].get(k) is not None]
            print(f"   подтверждение: RGBI за первый час ≥0.1% → индекс в ту же сторону до {lab}: {np.mean([(s > 0) == (x > 0) for s, x in xs]):.0%}, "
                  f"в сторону облигаций {np.mean([s * x for s, x in xs]):+.2f}% (n={len(xs)})")
    print("   примеры: " + "; ".join(f"{datetime.fromtimestamp(e['ts'], MSK):%Y-%m-%d} {e['prev']}→{e['fact']}" for e in out[-6:]))
    print("\n   КАК СДЕЛКИ (индекс, вход по открытию четверга, комиссия 0.2%):")
    for lab, cond, side in (("ускорение → шорт индекса", lambda e: e["delta"] >= 0.05, -1), ("замедление → лонг индекса", lambda e: e["delta"] <= -0.05, 1)):
        for k, hl in (("day", "до конца дня"), ("f27", "3 дня"), ("f45", "неделя")):
            tr = [(side * e["imoex"][k] - 0.2, e["ts"]) for e in out if cond(e) and e["imoex"].get(k) is not None]
            if len(tr) < 8:
                continue
            pn = np.array([p for p, _ in tr])
            aft = pn[[t >= SPLIT for _, t in tr]]
            print(f"     {lab:26} {hl:13} n={len(pn):3} приб. {np.mean(pn > 0):.0%} средняя {pn.mean():+.2f}% сумма {pn.sum():+.1f}% | после 2024 {aft.sum():+.1f}% ({len(aft)}, приб. {np.mean(aft > 0) if len(aft) else 0:.0%})")
    return res


def main():
    c = sqlite3.connect(ROOT / "data" / "news_archive.db")
    hist = load_hist(["IMOEX", "RGBI"] + list(NAME_RX))
    r1 = earnings(c, hist)
    r2 = weekly_cpi(c, hist)
    json.dump({"earnings": r1, "weekly_cpi": r2}, open(ROOT / "data" / "news_earnings_cpi.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\nсохранено: data/news_earnings_cpi.json")


if __name__ == "__main__":
    main()
