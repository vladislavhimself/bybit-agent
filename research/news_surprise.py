"""Неожиданные события: «тема молчала — и бац». Вопрос пользователя 07.11.2026: неожиданные события — чуть ли не главное, что двигает рынок.

Мера неожиданности для события (серии сообщений одной темы «на весь рынок», как в news_event_study):
  тишина = сообщений по теме за 7 дней до события (в среднем в час);  взрыв = сообщений в первые 30 минут события.
  шок      = за 7 дней почти ничего (≤ 0.2 сообщения в час) и взрыв ≥ 5 сообщений;
  ожидали  = тема шла плотно всю неделю (≥ 1 сообщение в час) — что бы ни случилось, это обсуждалось;
  обычное  = всё остальное.
Для каждого класса: |ход индекса| за 1ч/4ч/день/неделю/2 недели, и главное — продолжает ли рынок за первым часом
(знак первого часа против хода с конца первого часа до дня / недели / 2 недель), отдельно до и после 2024.
Плюс список 25 самых сильных шоков — проверить глазами, что это настоящие события.
Запуск из корня: python research/news_surprise.py. Модель не вызывается."""
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research.news_event_study import TOPICS, build_events, load_msgs  # noqa: E402
from research.news_expectations import move  # noqa: E402
from research.news_psychology import load_hist  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
MSK = ZoneInfo("Europe/Moscow")
SPLIT = int(datetime(2024, 1, 1, tzinfo=MSK).timestamp())


def main():
    since = int(datetime(2019, 1, 1, tzinfo=MSK).timestamp())
    items = load_msgs(since)
    mw = {name for name, m, _, _ in TOPICS if m}
    # сообщения по темам по времени — для «тишины до»
    by_topic = defaultdict(list)
    for ts, ch, name, s in items:
        if name in mw:
            by_topic[name].append(ts)
    for k in by_topic:
        by_topic[k] = np.array(sorted(by_topic[k]), dtype=np.int64)  # массив один раз, иначе searchsorted пересобирает список на каждом событии
    events = [e for e in build_events(items) if e["topic"] in mw]
    idx = load_hist(["IMOEX"])["IMOEX"]
    H = (1, 4, 9, 27, 45, 90)
    rows = []
    for e in events:
        arr = by_topic[e["topic"]]
        lo = int(np.searchsorted(arr, e["ts"] - 7 * 86400))
        hi = int(np.searchsorted(arr, e["ts"]))
        quiet = (hi - lo) / (7 * 24)  # сообщений в час за неделю до
        burst = int(np.searchsorted(arr, e["ts"] + 1800, side="right")) - hi
        if quiet <= 0.2 and burst >= 5:
            cls = "ШОК (тишина → взрыв)"
        elif quiet >= 1.0:
            cls = "ожидали (тема шла всю неделю)"
        else:
            cls = "обычное"
        mv = move(idx, e["ts"], horizons=H, back=(27,))
        if not mv or mv["lag_h"] > 16:
            continue
        rows.append({"ts": e["ts"], "topic": e["topic"], "cls": cls, "quiet": round(quiet, 2), "burst": burst, "n": e["n"], "mv": mv,
                     "half": "до 2024" if e["ts"] < SPLIT else "после 2024"})
    print(f"событий на весь рынок с реакцией: {len(rows)}")
    print("\n1. РАЗМЕР ХОДА ИНДЕКСА ПО КЛАССАМ (|ход|, %; доля >1% за день)")
    print(f"   {'класс':32} {'половина':12} {'n':>6} {'|1ч|':>6} {'|4ч|':>6} {'|день|':>7} {'|нед|':>6} {'|2нед|':>6}  >1% день")
    for cls in ("ШОК (тишина → взрыв)", "обычное", "ожидали (тема шла всю неделю)"):
        for half in ("до 2024", "после 2024"):
            sel = [r for r in rows if r["cls"] == cls and r["half"] == half]
            if len(sel) < 20:
                continue
            def a(k):
                return np.mean([abs(r["mv"][k]) for r in sel if r["mv"].get(k) is not None])
            print(f"   {cls:32} {half:12} {len(sel):6} {a('f1'):6.2f} {a('f4'):6.2f} {a('day'):7.2f} {a('f45'):6.2f} {a('f90'):6.2f}  {np.mean([abs(r['mv']['day']) > 1 for r in sel]):.0%}")

    print("\n2. ПРОДОЛЖЕНИЕ ЗА ПЕРВЫМ ЧАСОМ: знак первого часа (|1ч| ≥ 0.5%) против дальнейшего хода (с конца первого часа)")
    print(f"   {'класс':32} {'половина':12} {'n':>5}  {'→4ч':>14} {'→день':>14} {'→нед':>14} {'→2нед':>14}   (в ту же сторону %, средний ход в сторону первого часа)")
    out = {}
    for cls in ("ШОК (тишина → взрыв)", "обычное", "ожидали (тема шла всю неделю)"):
        for half in ("до 2024", "после 2024", "всё"):
            sel = [r for r in rows if r["cls"] == cls and (half == "всё" or r["half"] == half) and abs(r["mv"]["f1"]) >= 0.5]
            if len(sel) < 15:
                continue
            cells = []
            for k in ("f4", "day", "f45", "f90"):
                xs = [(np.sign(r["mv"]["f1"]), r["mv"][k] - r["mv"]["f1"]) for r in sel if r["mv"].get(k) is not None]
                cells.append(f"{np.mean([(x > 0) == (s > 0) for s, x in xs]):.0%} / {np.mean([s * x for s, x in xs]):+.2f}%")
            print(f"   {cls:32} {half:12} {len(sel):5}  " + " ".join(f"{c:>14}" for c in cells))
            out[f"{cls}|{half}"] = {"n": len(sel), "cells": cells}
    # то же для сильного первого часа ≥ 1%
    print("\n   то же при |первый час| ≥ 1.0%:")
    for cls in ("ШОК (тишина → взрыв)", "обычное", "ожидали (тема шла всю неделю)"):
        sel = [r for r in rows if r["cls"] == cls and abs(r["mv"]["f1"]) >= 1.0]
        if len(sel) < 15:
            continue
        cells = []
        for k in ("f4", "day", "f45", "f90"):
            xs = [(np.sign(r["mv"]["f1"]), r["mv"][k] - r["mv"]["f1"]) for r in sel if r["mv"].get(k) is not None]
            cells.append(f"{np.mean([(x > 0) == (s > 0) for s, x in xs]):.0%} / {np.mean([s * x for s, x in xs]):+.2f}%")
        print(f"   {cls:32} {'всё':12} {len(sel):5}  " + " ".join(f"{c:>14}" for c in cells))

    print("\n3. САМЫЕ СИЛЬНЫЕ ШОКИ (по |ход за день|) — проверить глазами")
    shocks = sorted([r for r in rows if r["cls"].startswith("ШОК")], key=lambda r: -abs(r["mv"]["day"]))[:25]
    for r in shocks:
        d = datetime.fromtimestamp(r["ts"], MSK)
        print(f"   {d:%Y-%m-%d %H:%M} {r['topic']:30} тишина {r['quiet']:.2f}/ч взрыв {r['burst']:3}  1ч {r['mv']['f1']:+.2f} день {r['mv']['day']:+.2f} нед {r['mv']['f45']:+.2f} 2нед {r['mv'].get('f90') or 0:+.2f}")
    json.dump({"continuation": out}, open(ROOT / "data" / "news_surprise.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("\nсохранено: data/news_surprise.json")


if __name__ == "__main__":
    main()
