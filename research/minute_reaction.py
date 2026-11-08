"""Реакция в первые минуты на большие сюрпризы — по минутным свечам ISS Мосбиржи (TQBR). Вопрос владельца 08.11.2026.
Берём события: дивиденды с |сюрприз| ≥ 30% или отмена (data/news_expectations.json), отчёты с числом консенсуса и |сюрприз| ≥ 15%
(data/news_earnings_numeric.json). Для каждого: цена до новости (открытие минуты новости), вход бота через lag_min минут (открытие),
и ход от входа к +5, +15, +60 минутам и к закрытию дня, в сторону сюрприза. Так видно, сколько остаётся после задержки.
Запуск из корня: python research/minute_reaction.py [--lag 1]. Только ISS, без модели."""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import requests

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
MSK = ZoneInfo("Europe/Moscow")
URL = "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/{sec}/candles.json"
ALIAS = {"X5": "X5", "T": "T", "YDEX": "YDEX"}


def candles(sec, day, s):
    rows, start = [], 0
    while True:
        r = s.get(URL.format(sec=sec), params={"interval": 1, "from": day, "till": day, "iss.meta": "off", "start": start}, timeout=30)
        r.raise_for_status()
        j = r.json()["candles"]
        cols, data = j["columns"], j["data"]
        rows += [dict(zip(cols, x)) for x in data]
        if len(data) < 500:
            break
        start += len(data)
        time.sleep(0.2)
    return rows


def react(rows, ts, lag_min, side):
    """side +1 = сюрприз вверх. Возвращает dict или None."""
    d = datetime.fromtimestamp(ts, MSK)
    mins = {}
    for c in rows:
        b = datetime.strptime(c["begin"], "%Y-%m-%d %H:%M:%S")
        mins[b.replace(tzinfo=MSK)] = c
    t0 = d.replace(second=0, microsecond=0)
    if t0 not in mins:
        # новость вне торгов или минуты без сделок: первая минута после
        later = sorted(k for k in mins if k > t0)
        if not later or (later[0] - t0).total_seconds() > 3 * 3600:
            return None
        t0 = later[0]
    pre = float(mins[t0]["open"])
    keys = sorted(mins)
    i0 = keys.index(t0)
    if i0 + lag_min >= len(keys):
        return None
    entry = float(mins[keys[i0 + lag_min]]["open"])
    out = {"pre": pre, "entry": entry, "jump_to_entry": side * (entry / pre - 1) * 100}
    for h, lab in ((5, "m5"), (15, "m15"), (60, "m60")):
        j = i0 + lag_min + h
        out[lab] = side * (float(mins[keys[min(j, len(keys) - 1)]]["close"]) / entry - 1) * 100
    out["eod"] = side * (float(mins[keys[-1]]["close"]) / entry - 1) * 100
    out["first_min"] = side * (float(mins[t0]["close"]) / pre - 1) * 100
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lag", type=int, default=1)
    a = ap.parse_args()
    events = []
    ex = json.load(open(ROOT / "data" / "news_expectations.json", encoding="utf-8"))
    for d in ex.get("dividends") or []:
        s = d.get("surprise")
        if s is None:
            continue
        if d.get("cancel") or abs(s) >= 0.3:
            events.append({"ts": d["ts"], "sym": d["sym"], "side": -1 if (d.get("cancel") or s < 0) else 1, "what": f"дивиденд {d.get('amount')} vs {d.get('expected')}" if not d.get("cancel") else "отмена дивиденда", "kind": "div"})
    p = ROOT / "data" / "news_earnings_numeric.json"
    if p.exists():
        for d in json.load(open(p, encoding="utf-8")).get("events") or []:
            if abs(d["s"]) >= 15:
                events.append({"ts": d["ts"], "sym": d["base"], "side": 1 if d["s"] > 0 else -1, "what": f"прибыль {d['fact']:.0f} vs {d['exp']:.0f} ({d['s']:+.0f}%)", "kind": "earn"})
    events.sort(key=lambda e: e["ts"])
    print(f"событий: {len(events)} (дивиденды {sum(1 for e in events if e['kind'] == 'div')}, отчёты {sum(1 for e in events if e['kind'] == 'earn')}); задержка входа {a.lag} мин")
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0"
    res = []
    for e in events:
        day = datetime.fromtimestamp(e["ts"], MSK).strftime("%Y-%m-%d")
        try:
            rows = candles(ALIAS.get(e["sym"], e["sym"]), day, s)
        except Exception as ex_:  # noqa: BLE001
            print("  ISS:", e["sym"], day, ex_)
            continue
        if not rows:
            continue
        r = react(rows, e["ts"], a.lag, e["side"])
        if not r:
            continue
        r.update(e)
        res.append(r)
        print(f"  {datetime.fromtimestamp(e['ts'], MSK):%Y-%m-%d %H:%M} {e['sym']:5} {'↑' if e['side'] > 0 else '↓'} {e['what'][:36]:36} 1-я мин {r['first_min']:+.2f}% | до входа {r['jump_to_entry']:+.2f}% | от входа: +5м {r['m5']:+.2f} +15м {r['m15']:+.2f} +60м {r['m60']:+.2f} день {r['eod']:+.2f}")
        time.sleep(0.3)
    if not res:
        return
    print("\nСРЕДНЕЕ (в сторону сюрприза, %), доля в плюс:")
    for kind in ("div", "earn", "all"):
        sel = [r for r in res if kind == "all" or r["kind"] == kind]
        if len(sel) < 3:
            continue
        for side, lab in ((1, "вверх"), (-1, "вниз")):
            s2 = [r for r in sel if r["side"] == side]
            if len(s2) < 3:
                continue
            def m(k):
                xs = [r[k] for r in s2]
                return f"{np.mean(xs):+.2f}% ({np.mean([x > 0 for x in xs]):.0%})"
            print(f"  {kind:4} {lab:5} n={len(s2):2}  первая минута {m('first_min')}  до входа {m('jump_to_entry')}  от входа: +5м {m('m5')}  +15м {m('m15')}  +60м {m('m60')}  день {m('eod')}")
    json.dump(res, open(ROOT / "data" / "minute_reaction.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    print("сохранено: data/minute_reaction.json")


if __name__ == "__main__":
    main()
