"""Проверка новостной части бота на истории: архив Telegram (data/news_archive.db) -> классификация той же моделью
и тем же промптом, что в боте -> реакция цены затронутых бумаг по часовой истории (data/history) -> у каких типов
событий, важностей и источников есть преимущество. Результат: таблица events в архиве, data/news_edge.json и отчёт.

Этапы (каждый можно повторять, уже сделанное пропускается):
  classify  — фильтр + разбор (дешёвая модель; --model main для основной), --limit N сообщений за запуск
  react     — доходности затронутых бумаг через 1/4/24 ч после события и «поздний вход» через 1 ч
  report    — сводные таблицы и правила для бота
Запуск: python research/news_backtest.py classify --since 2024-10-01 --limit 3000
        python research/news_backtest.py react
        python research/news_backtest.py report"""
import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MARKET", "moex")
sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.WARNING)

DB, H = "data/news_archive.db", "data/history"
BASES = ["SBER", "GAZP", "LKOH", "ROSN", "NVTK", "GMKN", "TATN", "VTBR", "MGNT", "PLZL", "CHMF", "MOEX", "AFLT", "SNGS",
         "ALRS", "MTSS", "YDEX", "OZON", "T", "X5"]
INDEX = "IMOEXF"


def db():
    c = sqlite3.connect(DB, timeout=120)
    c.execute("""CREATE TABLE IF NOT EXISTS events (channel TEXT, id INTEGER, ts INTEGER, model TEXT, relevant INTEGER,
                 event_type TEXT, importance INTEGER, direction TEXT, confidence REAL, affected TEXT, market_wide INTEGER,
                 already_priced INTEGER, surprise REAL, expected TEXT, title TEXT, PRIMARY KEY(channel, id, model))""")
    c.execute("CREATE TABLE IF NOT EXISTS skipped (channel TEXT, id INTEGER, PRIMARY KEY(channel, id))")
    c.execute("""CREATE TABLE IF NOT EXISTS reactions (channel TEXT, id INTEGER, model TEXT, symbol TEXT, r_bar REAL, r1 REAL, r4 REAL, r24 REAL,
                 late1_r4 REAL, late1_r24 REAL, PRIMARY KEY(channel, id, model, symbol))""")
    return c


def norm_key(text):
    return re.sub(r"[^a-zа-яё0-9%]+", " ", text.lower()).strip()[:70]


# ---------------- classify ----------------
def classify(args):
    from bot.analyst import Analyst
    from bot.config import Settings
    from bot.journal import Journal
    s = Settings()
    an = Analyst(s, Journal(":memory:"), None, None, None, None)
    an.budget = 10_000   # бюджет бота не трогаем: это исследование
    model_tag = args.model
    if args.model == "cheap":
        an._main_model = lambda: an.cheap   # всё дешёвой моделью
    c = db()
    since = int(datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    until = int(datetime.strptime(args.until, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()) if args.until else int(time.time())
    chans = args.channels
    q = f"SELECT channel, id, ts, text FROM msgs WHERE ts BETWEEN ? AND ? {'AND channel IN (%s)' % ','.join('?' * len(chans)) if chans else ''} ORDER BY ts"
    rows = c.execute(q, (since, until, *chans)).fetchall()
    done = {(r[0], r[1]) for r in c.execute("SELECT channel, id FROM events WHERE model=?", (model_tag,))}
    done |= {(r[0], r[1]) for r in c.execute("SELECT channel, id FROM skipped")}
    seen, n, n_rel, t0 = {}, 0, 0, time.time()
    cost0 = an.cost.today()["usd"]
    for ch, mid, ts, text in rows:
        if (ch, mid) in done:
            continue
        if n >= args.limit:
            break
        key = norm_key(text)
        if key and ts - seen.get(key, 0) < 7200:   # повтор одной новости в разных каналах/за 2 часа
            c.execute("INSERT OR IGNORE INTO skipped VALUES(?,?)", (ch, mid))
            continue
        seen[key] = ts
        n += 1
        title = re.sub(r"\s+", " ", text.split("\n")[0])[:200]
        try:
            if not an.prefilter(title, text[:800]):
                c.execute("INSERT OR IGNORE INTO skipped VALUES(?,?)", (ch, mid))
                if n % 200 == 0:
                    c.commit()
                continue
            ev = an.classify(title, text[:4000], f"tg:{ch}", "")
        except Exception as e:  # noqa: BLE001
            print("ошибка модели:", str(e)[:100])
            time.sleep(3)
            continue
        if not ev:
            continue
        n_rel += 1
        c.execute("INSERT OR REPLACE INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (ch, mid, ts, model_tag, int(ev["relevant"]), ev["event_type"], ev["importance"], ev["direction"], ev["confidence"],
                   json.dumps(ev["affected_symbols"]), int(ev["market_wide"]), int(ev["already_priced"]), ev.get("surprise", 0.0),
                   ev.get("expected", ""), title))
        if n % 50 == 0:
            c.commit()
            print(f"{n} сообщений, релевантных {n_rel}, {time.time() - t0:.0f} с, расход {an.cost.today()['usd'] - cost0:.2f}$", flush=True)
    c.commit()
    print(f"готово: {n} сообщений, релевантных {n_rel}, расход {an.cost.today()['usd'] - cost0:.2f}$")


# ---------------- react ----------------
_prices = {}


def prices(sym):
    if sym in _prices:
        return _prices[sym]
    p = os.path.join(H, f"{sym}_60.csv")
    if not os.path.exists(p):
        _prices[sym] = None
        return None
    d = pd.read_csv(p)
    if d.ts.max() > 1e11:
        d["ts"] = d["ts"] // 1000
    d = d.sort_values("ts").reset_index(drop=True)
    _prices[sym] = d
    return d


def reaction(sym, ts):
    d = prices(sym)
    if d is None:
        return None
    i = d.ts.searchsorted(ts, side="right")   # первый бар, начавшийся после события
    bar = i - 1   # бар, в котором новость вышла
    if bar < 1 or i + 24 >= len(d):
        return None
    p0 = float(d.close.iloc[bar - 1])          # цена до бара новости
    p_bar = float(d.close.iloc[bar])            # закрытие бара новости (вход «сразу»)
    r = {"r_bar": (p_bar / p0 - 1) * 100}
    for h in (1, 4, 24):
        r[f"r{h}"] = (float(d.close.iloc[bar + h]) / p_bar - 1) * 100   # вход по закрытию бара новости
    p_late = float(d.close.iloc[bar + 1])        # поздний вход: через час
    r["late1_r4"] = (float(d.close.iloc[bar + 4]) / p_late - 1) * 100
    r["late1_r24"] = (float(d.close.iloc[bar + 24]) / p_late - 1) * 100
    return r


def react(args):
    c = db()
    rows = c.execute("SELECT channel, id, ts, model, affected, market_wide, direction FROM events WHERE relevant=1").fetchall()
    done = {(r[0], r[1], r[2], r[3]) for r in c.execute("SELECT channel, id, model, symbol FROM reactions")}
    n = 0
    for ch, mid, ts, model, affected, mw, direction in rows:
        syms = [a for a in json.loads(affected or "[]") if a in BASES]
        if mw or not syms:
            syms = syms + [INDEX]
        for sym in syms:
            if (ch, mid, model, sym) in done:
                continue
            r = reaction(sym, ts)
            if r is None:
                continue
            c.execute("INSERT OR REPLACE INTO reactions VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (ch, mid, model, sym, r["r_bar"], r["r1"], r["r4"], r["r24"], r["late1_r4"], r["late1_r24"]))
            n += 1
    c.commit()
    print("реакций добавлено:", n)


# ---------------- report ----------------
def report(args):
    c = db()
    q = """SELECT e.channel, e.ts, e.event_type, e.importance, e.direction, e.confidence, e.market_wide, e.already_priced, e.surprise,
                  r.symbol, r.r_bar, r.r1, r.r4, r.r24, r.late1_r4, r.late1_r24
           FROM events e JOIN reactions r ON e.channel=r.channel AND e.id=r.id AND e.model=r.model
           WHERE e.relevant=1 AND e.direction!='neutral' AND e.model=?"""
    d = pd.DataFrame(c.execute(q, (args.model,)).fetchall(),
                     columns=["channel", "ts", "type", "imp", "dir", "conf", "mw", "priced", "surprise", "sym", "r_bar", "r1", "r4", "r24", "l4", "l24"])
    if not len(d):
        print("нет данных")
        return
    sign = np.where(d["dir"] == "bullish", 1, -1)
    for col in ("r_bar", "r1", "r4", "r24", "l4", "l24"):
        d["s_" + col] = sign * d[col]   # доходность в сторону прогноза модели
    print(f"событий с реакцией: {len(d)}, период {datetime.fromtimestamp(d.ts.min(), timezone.utc).date()} — {datetime.fromtimestamp(d.ts.max(), timezone.utc).date()}\n")

    def table(group, title):
        g = d.groupby(group)
        out = pd.DataFrame({"n": g.size(), "бар новости": g.s_r_bar.mean().round(3), "+1ч": g.s_r1.mean().round(3),
                            "+4ч": g.s_r4.mean().round(3), "+24ч": g.s_r24.mean().round(3),
                            "попад.4ч %": (g.s_r4.apply(lambda x: (x > 0).mean()) * 100).round(0),
                            "поздн.+4ч": g.s_l4.mean().round(3), "поздн.+24ч": g.s_l24.mean().round(3)})
        print(f"== {title} (средняя доходность в сторону прогноза, %) ==")
        print(out.sort_values("n", ascending=False).head(25).to_string())
        print()

    d["imp_b"] = pd.cut(d.imp, [0, 4, 5, 6, 7, 8, 10], labels=["<=4", "5", "6", "7", "8", "9-10"])
    table("imp_b", "по важности")
    table("type", "по типу события")
    table("dir", "по направлению")
    table("channel", "по каналу")
    d["sur_b"] = pd.cut(d.surprise.fillna(0), [-2, -0.5, -0.15, 0.15, 0.5, 2], labels=["сильно хуже", "хуже", "совпало/нет", "лучше", "сильно лучше"])
    table("sur_b", "по расхождению с ожиданиями")
    table("priced", "already_priced (1 = модель считает, что уже в цене)")
    d["hour"] = pd.to_datetime(d.ts, unit="s", utc=True).dt.tz_convert("Europe/Moscow").dt.hour
    table("hour", "по часу публикации (МСК)")
    strong = d[d.imp >= 7]
    edge = {
        "n_all": int(len(d)), "mean_s_r4_all": round(float(d.s_r4.mean()), 3), "hit4_all": round(float((d.s_r4 > 0).mean()), 3),
        "n_imp7": int(len(strong)), "mean_s_r4_imp7": round(float(strong.s_r4.mean()), 3) if len(strong) else None,
        "hit4_imp7": round(float((strong.s_r4 > 0).mean()), 3) if len(strong) else None,
        "by_type": {k: {"n": int(len(g)), "s_r4": round(float(g.s_r4.mean()), 3), "s_r24": round(float(g.s_r24.mean()), 3), "hit4": round(float((g.s_r4 > 0).mean()), 3)}
                    for k, g in d.groupby("type") if len(g) >= 15},
        "by_importance": {str(k): {"n": int(len(g)), "s_r4": round(float(g.s_r4.mean()), 3), "hit4": round(float((g.s_r4 > 0).mean()), 3)} for k, g in d.groupby("imp_b", observed=True)},
        "late_entry_penalty_4h": round(float(d.s_r4.mean() - d.s_l4.mean()), 3),
    }
    json.dump(edge, open("data/news_edge.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("сохранено data/news_edge.json")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["classify", "react", "report"])
    ap.add_argument("--since", default="2024-10-01")
    ap.add_argument("--until", default="")
    ap.add_argument("--limit", type=int, default=2000)
    ap.add_argument("--channels", nargs="*", default=[])
    ap.add_argument("--model", default="cheap", choices=["cheap", "main"])
    a = ap.parse_args()
    {"classify": classify, "react": react, "report": report}[a.stage](a)
