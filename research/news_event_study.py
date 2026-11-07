"""Как рынок реагировал на новости за много лет: темы по правилам, серии, реакция индекса.

Вход: data/news_archive.db (msgs: channel, id, ts, text — публичные Telegram-каналы агентств и трейдерских лент),
data/history/IMOEX_60.csv (часовые свечи индекса Мосбиржи с 2019).
Шаги: 1) каждое сообщение относится к темам по словарю TOPICS (первая подошедшая тема в порядке приоритета) и получает знак
по словам (повышение/снижение, сорваны/подтверждены); 2) сообщения одной темы ближе 30 минут друг к другу — одно событие
(серия: число сообщений и каналов); 3) реакция индекса: от открытия первой часовой свечи после события через 1, 4, 9 (≈день)
и 27 (≈3 дня) свечей; 4) сравнение с фоном (все часы), разрезы по режиму рынка, размеру серии, новизне темы;
5) проверка вне выборки: темы, у которых на 2019–2024 ход значим, проверяются на 2025–2026.
Модель не вызывается. Запуск из корня: python research/news_event_study.py [--since 2019-01-01] [--split 2025-01-01]
Результат: таблицы в консоль и data/news_event_study.json."""
import argparse
import json
import math
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]

UP = r"(сниз|снижен|понизил|понижен|опустил|смягч)"
DOWN = r"(повыс|повышен|поднял|ужесточ)"
# (тема, рынок ли в целом, шаблон, знак: +1/-1/0 или (шаблон_плюс, шаблон_минус))
TOPICS = [
    ("ставка ЦБ", True, r"ключев\w+ ставк|ставк\w+ (ЦБ|Банка России)|Банк\w* России[^.]{0,40}ставк|ЦБ[^.]{0,30}ставк", (UP, DOWN)),
    ("мобилизация", True, r"мобилизац", -1),
    ("мирные переговоры / перемирие", True,
     r"мирн\w+ (переговор|план|соглашен|урегулирован|сделк)|перемири|прекращени\w+ огня|переговор\w*[^.]{0,80}(Украин|Росси|США|Трамп|Путин|Зеленск|Байден|Уиткофф)|(Украин|Росси|США|Трамп|Путин|Зеленск|Байден|Уиткофф)[^.]{0,80}переговор",
     (r"(договор|согласов|подтвер|состоя|пройд|прогресс|готов|близ|прорыв)", r"(сорв|провал|отказ|прерв|отмен|тупик|не будет|невозмож)")),
    ("санкции: снятие / ослабление", True, r"(снят|ослаб|отмен|смягч)\w*[^.]{0,40}санкци|санкци\w*[^.]{0,40}(снят|ослаб|отмен|смягч)", 1),
    ("санкции: пакет / введение", True, r"пакет\w* санкц|санкци\w*[^.]{0,40}(введ|вступ|принят|утверд|расшир|ужесточ)|(введ|вступ|принят|утверд|расшир|ужесточ)\w*[^.]{0,40}санкци|SDN|OFAC", -1),
    ("ядерная угроза", True, r"ядерн\w+ (оруж|удар|угроз|эскалац)|ТЯО", -1),
    ("удары / атаки / обстрелы", True, r"(удар\w*|атак\w*|обстрел\w*|взрыв\w*)[^.]{0,60}(Москв|Крым|НПЗ|нефтебаз|инфраструктур|порт|Белгород|Курск|аэродром|завод)|БПЛА|беспилотник|дрон\w* атак", -1),
    ("нефть", True, r"\bBrent\b|нефт\w* (подорож|подешев|вырос|упал|обвал|рухнул|взлет|торгу|цен)|цен\w* на нефть",
     (r"(подорож|вырос|взлет|подскоч|рост|растут|дорожает)", r"(подешев|упал|обвал|рухнул|снижа|падает|дешевеет|паден)")),
    ("ОПЕК+", True, r"ОПЕК|OPEC", (r"(сокращ|сниж\w+ добыч|продл)", r"(увелич\w+ добыч|наращ|нарастит)")),
    ("курс рубля", True, r"рубл\w* (укреп|ослаб|упал|вырос|обвал|рухнул)|курс\w* (доллар|рубл|юан)[^.]{0,40}(выс|низ|вырос|упал|укреп|ослаб)|доллар[^.]{0,20}(выше|ниже|превысил|опустил)",
     (r"(ослаб\w* рубл|рубл\w* (ослаб|упал|обвал|рухнул)|доллар[^.]{0,20}(выше|превысил|вырос))", r"(укреп|рубл\w* вырос|доллар[^.]{0,20}(ниже|опустил|упал))")),
    ("инфляция", True, r"инфляци", (r"(замедл|сниз|снижен|ниже ожид)", r"(ускор|выросл|повыс|разогн|выше ожид)")),
    ("налоги / бюджет", True, r"повыш\w+ налог|налог\w+ (на сверхприбыл|на прибыл|для)|windfall|НДС|дефицит бюджета|бюджет\w* (дефицит|расход|доход)", (r"(снижен\w+ налог|льгот)", r"(повыш|увелич|рост|дефицит)")),
    ("нерезиденты / блокировки", True, r"нерезидент|заморо\w+ актив|блокиров\w+ актив|контрол\w+ за (движением )?капитал|обмен\w+ (заблокирован|заморожен)", -1),
    ("сбой / остановка торгов", True, r"торги[^.]{0,30}(приостанов|останов|не провод|отмен)|Мосбирж\w*[^.]{0,40}(сбой|приостанов)|дискретн\w+ аукцион", -1),
    ("COVID", True, r"коронавирус|COVID|карантин|локдаун|нерабочи\w+ дн", -1),
    ("ФРС", True, r"ФРС|FOMC|Пауэлл", (UP, DOWN)),
    ("Китай", True, r"Кита\w+|КНР|Си Цзиньпин", 0),
    ("Трамп", True, r"Трамп", 0),
    ("Путин", True, r"Путин", 0),
    ("Иран / Израиль / Ближний Восток", True, r"Иран|Израил|Ормуз|Хезболл|ХАМАС|Ближн\w+ Восток", 0),
    ("ОФЗ / долг", True, r"ОФЗ|гособлигац|аукцион\w* Минфин|RGBI", 0),
    ("макростатистика", True, r"\bВВП\b|безработиц|PMI|промпроизводств|розничн\w+ продаж|деловая активность", 0),
    ("газ / Газпром", False, r"Газпром|газ\w* (цен|поставк|транзит)|СПГ|Северн\w+ поток|Сила Сибири", 0),
    ("дивиденды", False, r"дивиденд", (r"(рекоменд|утверд|объяв|одобр|выплат\w+ \d)", r"(не выплач|отказ|без дивиденд|не рекоменд|не будет)")),
    ("выкуп / оферта", False, r"выкуп|оферт|buyback|байбэк", 1),
    ("отчётность", False, r"выручк|чист\w+ прибыл|EBITDA|МСФО|РСБУ|отчита|отчёт\w* за|отчет\w* за", (r"(вырос|рост|увелич|рекорд)", r"(сниз|упал|убыт|сокращ|паден)")),
    ("IPO / допэмиссия", False, r"\bIPO\b|\bSPO\b|допэмисси|размещени\w+ акци", -1),
    ("суд / арест / национализация", False, r"\bсуд\b|арест\w*|национализ|изъят|Генпрокурат|обыск|уголовн\w+ дел", -1),
    ("слияния и сделки", False, r"слияни|поглощен|приобрет\w+ (долю|пакет|акци|контрол)|покупк\w+ (доли|пакета|акций)", 1),
    ("золото", False, r"золот", 0),
]
RX = [(name, mw, re.compile(pat, re.I), sign if not isinstance(sign, tuple) else (re.compile(sign[0], re.I), re.compile(sign[1], re.I)))
      for name, mw, pat, sign in TOPICS]


def classify(text):
    t = (text or "")[:600]
    for name, mw, rx, sign in RX:
        if rx.search(t):
            if isinstance(sign, tuple):
                p, m = bool(sign[0].search(t)), bool(sign[1].search(t))
                s = 1 if p and not m else (-1 if m and not p else 0)
            else:
                s = sign
            return name, s
    return None, 0


def load_msgs(since_ts):
    c = sqlite3.connect(ROOT / "data" / "news_archive.db")
    rows = c.execute("SELECT channel, ts, text FROM msgs WHERE ts>=? ORDER BY ts", (since_ts,)).fetchall()
    out = []
    for ch, ts, text in rows:
        name, s = classify(text)
        if name:
            out.append((ts, ch, name, s))
    return out


def build_events(items, gap=1800):
    """Сообщения одной темы ближе gap секунд -> одно событие."""
    by = defaultdict(list)
    for ts, ch, name, s in items:
        by[name].append((ts, ch, s))
    events = []
    for name, rows in by.items():
        rows.sort()
        cur = None
        for ts, ch, s in rows:
            if cur and ts - cur["last"] <= gap:
                cur["last"] = ts
                cur["n"] += 1
                cur["channels"].add(ch)
                cur["signs"].append(s)
            else:
                if cur:
                    events.append(cur)
                cur = {"topic": name, "ts": ts, "last": ts, "n": 1, "channels": {ch}, "signs": [s]}
        if cur:
            events.append(cur)
    for e in events:
        nz = [s for s in e["signs"] if s]
        e["sign"] = (1 if sum(nz) > 0 else -1) if nz else 0
        e["channels"] = len(e["channels"])
    events.sort(key=lambda e: e["ts"])
    # новизна: сколько событий той же темы было за прошлые сутки и неделю
    last = defaultdict(list)
    for e in events:
        prev = [t for t in last[e["topic"]] if e["ts"] - t <= 7 * 86400]
        e["prev_24h"] = sum(1 for t in prev if e["ts"] - t <= 86400)
        e["prev_7d"] = len(prev)
        last[e["topic"]] = prev + [e["ts"]]
    return events


def load_index():
    df = pd.read_csv(ROOT / "data" / "history" / "IMOEX_60.csv")
    df = df.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    return df


def reactions(events, df, horizons=(1, 4, 9, 27)):
    ts = df["ts"].to_numpy()
    op, cl = df["open"].to_numpy(), df["close"].to_numpy()
    for e in events:
        i = int(np.searchsorted(ts, e["ts"], side="right"))    # первая свеча, начавшаяся после события
        if i + max(horizons) >= len(ts):
            e["r"] = None
            continue
        p0 = op[i]
        e["lag_h"] = (ts[i] - e["ts"]) / 3600
        e["r"] = {h: (cl[i + h - 1] / p0 - 1) * 100 for h in horizons}
    return [e for e in events if e.get("r")]


def baseline(df, horizons=(1, 4, 9, 27), step=3):
    op, cl = df["open"].to_numpy(), df["close"].to_numpy()
    out = {}
    for h in horizons:
        r = (cl[h - 1:] / op[:len(op) - h + 1] - 1) * 100
        r = r[::step]
        out[h] = {"mean_abs": float(np.mean(np.abs(r))), "p_gt1": float(np.mean(np.abs(r) > 1)), "std": float(np.std(r))}
    return out


def stats(evs, h=4):
    if not evs:
        return None
    r = np.array([e["r"][h] for e in evs])
    sgn = np.array([e["sign"] for e in evs])
    m = sgn != 0
    hit = float(np.mean((r[m] > 0) == (sgn[m] > 0))) if m.sum() >= 20 else None
    signed = float(np.mean(r[m] * sgn[m])) if m.sum() >= 20 else None
    t = float(np.mean(r) / (np.std(r) / math.sqrt(len(r)))) if len(r) > 5 and np.std(r) > 0 else 0.0
    return {"n": len(evs), "mean": round(float(np.mean(r)), 3), "mean_abs": round(float(np.mean(np.abs(r))), 3),
            "p_gt1": round(float(np.mean(np.abs(r) > 1)), 3), "t": round(t, 2), "hit": None if hit is None else round(hit, 2),
            "signed": None if signed is None else round(signed, 3), "n_sign": int(m.sum())}


def regime(ts):
    d = datetime.fromtimestamp(ts, timezone.utc).date()
    if d < datetime(2022, 2, 24).date():
        return "до 24.02.2022"
    if d < datetime(2024, 1, 1).date():
        return "2022–2023"
    return "2024+"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2019-01-01")
    ap.add_argument("--split", default="2025-01-01")
    a = ap.parse_args()
    since_ts = int(datetime.strptime(a.since, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    split_ts = int(datetime.strptime(a.split, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    items = load_msgs(since_ts)
    df = load_index()
    events = reactions(build_events(items), df)
    base = baseline(df)
    print(f"сообщений с темой: {len(items)}, событий: {len(events)}, свечей индекса: {len(df)} "
          f"({datetime.fromtimestamp(int(df.ts.iloc[0]), timezone.utc).date()} — {datetime.fromtimestamp(int(df.ts.iloc[-1]), timezone.utc).date()})")
    print(f"ФОН (любой час): |ход 4ч| {base[4]['mean_abs']:.2f}%, >1% в {base[4]['p_gt1']:.0%}; |ход 1д| {base[9]['mean_abs']:.2f}%, >1% в {base[9]['p_gt1']:.0%}")
    out = {"baseline": base, "topics": {}, "splits": {}}
    hdr = f"{'тема':34} {'n':>5} {'ср.4ч':>7} {'|4ч|':>6} {'>1%':>5} {'t':>5} {'знак%':>6} {'в знак':>7} {'|1д|':>6} {'|3д|':>6}"
    print("\nПО ТЕМАМ (индекс Мосбиржи, от открытия следующей часовой свечи):\n" + hdr)
    by = defaultdict(list)
    for e in events:
        by[e["topic"]].append(e)
    for name, _, _, _ in TOPICS:
        evs = by.get(name) or []
        s4 = stats(evs, 4)
        if not s4 or s4["n"] < 15:
            continue
        s9, s27 = stats(evs, 9), stats(evs, 27)
        out["topics"][name] = {"h4": s4, "h9": s9, "h27": s27}
        hit_s = "—" if s4["hit"] is None else "%.0f%%" % (s4["hit"] * 100)
        sgn_s = "—" if s4["signed"] is None else "%+.2f" % s4["signed"]
        print(f"{name:34} {s4['n']:>5} {s4['mean']:>+7.2f} {s4['mean_abs']:>6.2f} {s4['p_gt1']:>5.0%} {s4['t']:>5.1f} "
              f"{hit_s:>6} {sgn_s:>7} {s9['mean_abs']:>6.2f} {s27['mean_abs']:>6.2f}")
    print("\nСЕРИИ: размер события (сообщений за полчаса) -> |ход 4ч|, >1%  [все темы «на весь рынок»]")
    mw = [e for e in events if dict((n, m) for n, m, _, _ in TOPICS)[e["topic"]]]
    for lab, cond in (("1 сообщение", lambda e: e["n"] == 1), ("2–4", lambda e: 2 <= e["n"] <= 4), ("5–9", lambda e: 5 <= e["n"] <= 9), ("10+", lambda e: e["n"] >= 10)):
        s = stats([e for e in mw if cond(e)], 4)
        if s:
            out["splits"][f"серия {lab}"] = s
            print(f"  {lab:14} n={s['n']:>6} |4ч| {s['mean_abs']:.2f}% >1% {s['p_gt1']:.0%} знак {s['hit']} в знак {s['signed']}")
    print("\nНОВИЗНА (рынок в целом): первая за сутки vs повтор темы")
    for lab, cond in (("новая (0 за сутки)", lambda e: e["prev_24h"] == 0), ("повтор (1–3 за сутки)", lambda e: 1 <= e["prev_24h"] <= 3), ("фон темы (4+ за сутки)", lambda e: e["prev_24h"] >= 4)):
        s = stats([e for e in mw if cond(e)], 4)
        if s:
            out["splits"][lab] = s
            print(f"  {lab:24} n={s['n']:>6} |4ч| {s['mean_abs']:.2f}% >1% {s['p_gt1']:.0%} знак {s['hit']} в знак {s['signed']}")
    print("\nРЕЖИМ РЫНКА (рынок в целом):")
    for reg in ("до 24.02.2022", "2022–2023", "2024+"):
        s = stats([e for e in mw if regime(e["ts"]) == reg], 4)
        if s:
            out["splits"][reg] = s
            print(f"  {reg:14} n={s['n']:>6} |4ч| {s['mean_abs']:.2f}% >1% {s['p_gt1']:.0%} знак {s['hit']} в знак {s['signed']}")
    print(f"\nВНЕ ВЫБОРКИ: правило знака темы, значимое до {a.split} (|t|>2 или знак>=58% при n>=30), проверяется после:")
    out["oos"] = {}
    for name in out["topics"]:
        tr = [e for e in by[name] if e["ts"] < split_ts and e["sign"]]
        te = [e for e in by[name] if e["ts"] >= split_ts and e["sign"]]
        s_tr = stats(tr, 4)
        if not s_tr or s_tr["n_sign"] < 30 or s_tr["hit"] is None:
            continue
        good = s_tr["hit"] >= 0.58 or s_tr["hit"] <= 0.42
        s_te = stats(te, 4)
        if good:
            out["oos"][name] = {"train": s_tr, "test": s_te}
            print(f"  {name:34} до: знак {s_tr['hit']:.0%} (n={s_tr['n_sign']}), в знак {s_tr['signed']:+.2f}% | после: "
                  + (f"знак {s_te['hit']:.0%} (n={s_te['n_sign']}), в знак {s_te['signed']:+.2f}%" if s_te and s_te["hit"] is not None else "мало данных"))
    (ROOT / "data" / "news_event_study.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\nсохранено: data/news_event_study.json")


if __name__ == "__main__":
    main()
