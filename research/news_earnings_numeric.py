"""Отчёты с ЧИСЛОМ консенсуса: размер расхождения и реакция бумаги. Вопрос владельца 08.11.2026: «сильно лучше ожиданий — лонговать же надо».
Ожидание: «консенсус(-прогноз) … чистая прибыль … X млрд» по компании за 1–30 дней до отчёта (cbrstocks #Прогноз, Интерфакс в лентах).
Факт: «чистая прибыль … (составила|выросла до|снизилась до|…) Y млрд» по той же компании. Сюрприз = (Y−X)/X.
Реакция бумаги сверх индекса: скачок (до новости → открытие следующего часа), 1ч, 4ч, день, 3 дня, неделя, 2 недели; разгон за 10 дней до.
Группы по размеру: сильно лучше (≥ +15%), чуть лучше (+5..+15), как ждали (±5), чуть хуже, сильно хуже (≤ −15%). Запуск из корня."""
import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
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
NUM = r"(\d{1,4}(?:[  ]\d{3})*(?:[.,]\d{1,3})?)"
CONS = re.compile(r"консенсус[^\n]{0,200}?(чист\w+ прибыл\w*|прибыл\w*)[^\n]{0,100}?" + NUM + r"\s?(млрд|трлн)", re.I)
CONS2 = re.compile(r"(чист\w+ прибыл\w*)[^\n]{0,80}?(прогнозиру\w+|ожида\w+|консенсус\w*)[^\n]{0,100}?" + NUM + r"\s?(млрд|трлн)", re.I)
FACT = re.compile(r"чист\w+ прибыл\w*[^\n]{0,120}?(составил\w*|выросл\w* до|увеличил\w* до|снизил\w* до|сократил\w* до|упал\w* до|в размере|достигл\w*)\s?" + NUM + r"\s?(млрд|трлн)", re.I)
PERIOD = re.compile(r"МСФО|РСБУ|квартал|полугод|9 месяц|девять месяц|за год|20\d\d", re.I)
H = (1, 4, 9, 27, 45, 90)
KEYS = ("jump", "f1", "f4", "day", "f27", "f45", "f90")


def num(s, unit):
    v = float(s.replace(" ", "").replace(" ", "").replace(",", "."))
    return v * 1000 if unit.lower().startswith("трлн") else v


def main():
    c = sqlite3.connect(ROOT / "data" / "news_archive.db")
    hist = load_hist(["IMOEX"] + list(NAME_RX))
    since = int(datetime(2019, 1, 1, tzinfo=MSK).timestamp())
    cons = defaultdict(list)   # base -> [(ts, value)]
    for ch, ts, t in c.execute("SELECT channel, ts, text FROM msgs WHERE ts>=? AND text LIKE '%консенсус%' AND text LIKE '%прибыл%' ORDER BY ts", (since,)):
        t = t or ""
        m = CONS.search(t[:700]) or CONS2.search(t[:700])
        if not m:
            continue
        base = company_of(t)
        if base and base in hist:
            cons[base].append((ts, num(m.group(2) if m.re is CONS else m.group(3), m.group(3) if m.re is CONS else m.group(4))))
    print(f"консенсусов по прибыли с компанией: {sum(len(v) for v in cons.values())} по {len(cons)} бумагам")
    out = []
    last = {}
    for ch, ts, t in c.execute("SELECT channel, ts, text FROM msgs WHERE ts>=? AND (text LIKE '%чистая прибыль%' OR text LIKE '%чистой прибыли%') AND text LIKE '%млрд%' ORDER BY ts", (since,)):
        t = t or ""
        head = t[:500]
        if not PERIOD.search(head):
            continue
        m = FACT.search(head)
        if not m:
            continue
        base = company_of(t)
        if not base or base not in hist or base not in cons:
            continue
        if ts - last.get(base, 0) < 20 * 86400:
            continue
        exp = [v for t0, v in cons[base] if 0 < ts - t0 <= 30 * 86400]
        if not exp:
            continue
        expected = float(np.median(exp))
        fact = num(m.group(2), m.group(3))
        if not (0.2 <= fact / expected <= 5):
            continue
        last[base] = ts
        mv = move(hist[base], ts, horizons=H, back=(27, 90))
        mi = move(hist["IMOEX"], ts, horizons=H, back=(27, 90))
        if not mv or not mi or mv["lag_h"] > 16:
            continue
        rel = {k: mv[k] - mi[k] if mv.get(k) is not None and mi.get(k) is not None else None for k in KEYS + ("b90",)}
        s = (fact - expected) / expected * 100
        out.append({"ts": ts, "base": base, "exp": expected, "fact": fact, "s": s, "rel": rel, "n_exp": len(exp), "half": "до 2024" if ts < SPLIT else "после 2024",
                    "snippet": re.sub(r"\s+", " ", head[:120])})
    print(f"отчётов с числом консенсуса и фактом: {len(out)}")
    groups = (("сильно лучше (≥ +15%)", lambda d: d["s"] >= 15), ("чуть лучше (+5..+15)", lambda d: 5 <= d["s"] < 15), ("как ждали (±5)", lambda d: -5 < d["s"] < 5),
              ("чуть хуже (−15..−5)", lambda d: -15 < d["s"] <= -5), ("сильно хуже (≤ −15%)", lambda d: d["s"] <= -15))
    print(f"   {'группа':24} {'n':>3} {'разгон10д':>9} " + " ".join(f"{k:>14}" for k in ("скачок", "1ч", "4ч", "день", "3д", "нед", "2нед")))
    res = {}
    for lab, cond in groups:
        lst = [d for d in out if cond(d)]
        if len(lst) < 5:
            print(f"   {lab:24} {len(lst):3} мало")
            continue
        def agg(k):
            xs = [d["rel"][k] for d in lst if d["rel"].get(k) is not None]
            return f"{np.mean(xs):+.2f}% ({np.mean([x > 0 for x in xs]):.0%})" if xs else "—"
        print(f"   {lab:24} {len(lst):3} {np.mean([d['rel']['b90'] for d in lst]):+8.2f}% " + " ".join(f"{agg(k):>14}" for k in KEYS))
        res[lab] = {"n": len(lst), **{k: agg(k) for k in KEYS}}
        for half in ("до 2024", "после 2024"):
            l2 = [d for d in lst if d["half"] == half]
            if len(l2) >= 5:
                print(f"      {half:12} {len(l2):3} " + " ".join(f"{(lambda xs: f'{np.mean(xs):+.2f}% ({np.mean([x > 0 for x in xs]):.0%})' if xs else '—')([d['rel'][k] for d in l2 if d['rel'].get(k) is not None]):>14}" for k in ("jump", "day", "f45", "f90")) + "   (скачок, день, нед, 2нед)")
    print("\n   вход после первого часа (с конца первого часа), сверх индекса — то, что доступно боту с задержкой:")
    for lab, cond in groups:
        lst = [d for d in out if cond(d)]
        if len(lst) >= 5:
            for k, hl in (("day", "до конца дня"), ("f45", "неделя"), ("f90", "2 недели")):
                xs = [d["rel"][k] - d["rel"]["f1"] for d in lst if d["rel"].get(k) is not None and d["rel"].get("f1") is not None]
                print(f"     {lab:24} → {hl:13} n={len(xs):3} {np.mean(xs):+.2f}% (вверх {np.mean([x > 0 for x in xs]):.0%})")
    print("\n   самые большие сюрпризы:")
    for d in sorted(out, key=lambda d: -abs(d["s"]))[:16]:
        print(f"     {datetime.fromtimestamp(d['ts'], MSK):%Y-%m-%d %H:%M} {d['base']:5} ждали {d['exp']:.0f} факт {d['fact']:.0f} ({d['s']:+.0f}%) скачок {d['rel']['jump']:+.2f} день {d['rel']['day']:+.2f} нед {d['rel']['f45']:+.2f} | {d['snippet'][:80]}")
    json.dump({"groups": res, "events": [{k: v for k, v in d.items() if k != "rel"} | {"rel": d["rel"]} for d in out]},
              open(ROOT / "data" / "news_earnings_numeric.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    print("\nсохранено: data/news_earnings_numeric.json")


if __name__ == "__main__":
    main()
