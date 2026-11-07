"""Психология участников на истории: купили на ожидании и продали на факте, перебор и откат, ожидание и факт, время суток.

Продолжение research/news_event_study.py (те же темы и события, тот же архив). Здесь события по компаниям привязываются
к бумаге (хэштег #SBER или название компании в тексте) и считается ход бумаги сверх индекса; события всего рынка — по индексу.
1. «Купили на ожидании»: хорошая новость по компании (дивиденды рекомендованы, отчёт лучше, выкуп) после того, как бумага
   за 10 торговых дней обогнала индекс на 4%+, против той же новости без разгона. Случай X5 06.11.2026.
2. «Перебор и откат»: резкий первый час после новости -> куда идёт цена дальше (паника, эйфория).
3. «Ожидание и факт» по точечным событиям: решение ЦБ, рекомендация дивидендов, отчёт — ход за 3 дня до и после.
4. Время суток: вечерняя сессия (после 19:00 МСК) против дневной.
Запуск из корня: python research/news_psychology.py [--since 2019-01-01] [--split 2025-01-01]. Модель не вызывается."""
import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research.news_event_study import TOPICS, build_events, classify, load_index, stats  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
MSK = ZoneInfo("Europe/Moscow")
NAMES = {"SBER": r"Сбер\w*", "GAZP": r"Газпром\b|Газпрома\b|Газпрому\b", "LKOH": r"Лукойл", "ROSN": r"Роснефт", "NVTK": r"Новат[эе]к", "GMKN": r"Норникел|Норильск",
         "PLZL": r"Полюс", "X5": r"\bX5\b|Пятёрочк|Пятерочк", "YDEX": r"Яндекс", "TATN": r"Татнефт", "SNGS": r"Сургутнефтегаз", "MGNT": r"Магнит\b",
         "VTBR": r"\bВТБ\b", "T": r"Т-Банк|Тинькофф|Т-Технологи|TCS\b", "ALRS": r"Алроса|АЛРОСА", "CHMF": r"Северстал", "NLMK": r"\bНЛМК\b", "MAGN": r"\bММК\b",
         "RUAL": r"Русал|РУСАЛ", "AFLT": r"Аэрофлот", "MTSS": r"\bМТС\b", "RTKM": r"Ростелеком", "PHOR": r"Фосагро|ФосАгро", "MOEX": r"Мосбирж\w* (объяв|отчит|рекоменд|дивиденд)",
         "SMLT": r"Самолет|Самолёт", "PIKK": r"\bПИК\b", "SVCB": r"Совкомбанк", "POSI": r"Positive|Позитив", "ASTR": r"Астра\b", "HEAD": r"HeadHunter|Хедхантер|\bhh\.ru",
         "OZON": r"\bOzon\b|Озон", "VKCO": r"\bVK\b|ВКонтакте", "SIBN": r"Газпром нефт", "IRAO": r"Интер РАО", "HYDR": r"РусГидро|Русгидро", "MTLR": r"Мечел",
         "SGZH": r"Сегеж", "TRNFP": r"Транснефт", "BSPB": r"Банк Санкт-Петербург|БСПБ", "FLOT": r"Совкомфлот", "NMTP": r"\bНМТП\b", "FEES": r"Россети\b|ФСК",
         "ENPG": r"Эн\+|En\+", "UPRO": r"Юнипро", "AFKS": r"АФК Система|\bСистема\b", "RENI": r"Ренессанс", "LENT": r"Лента\b", "MVID": r"М\.Видео|М.Видео",
         "BELU": r"Белуга|Novabev", "ETLN": r"Эталон", "LSRG": r"\bЛСР\b", "SELG": r"Селигдар", "UGLD": r"ЮГК|Южуралзолото", "DATA": r"Аренадата", "IVAT": r"\bIVA\b",
         "OZPH": r"Озон Фармацевтик", "PRMD": r"Промомед", "CNRU": r"Циан|ЦИАН", "DOMRF": r"ДОМ\.РФ"}
NAME_RX = {k: re.compile(v, re.I) for k, v in NAMES.items()}
HASH = re.compile(r"#([A-Z]{3,5})\b")
COMPANY_TOPICS = {name for name, mw, _, _ in TOPICS if not mw}


def tickers_of(text, known):
    t = (text or "")[:400]
    out = [h for h in HASH.findall(t) if h in known]
    for k, rx in NAME_RX.items():
        if k in known and k not in out and rx.search(t):
            out.append(k)
    return out[:3]


def load_hist(symbols):
    out = {}
    for s in symbols:
        p = ROOT / "data" / "history" / f"{s}_60.csv"
        if p.exists():
            df = pd.read_csv(p).sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
            if df["ts"].iloc[0] > 1e11:
                df["ts"] = df["ts"] // 1000
            out[s] = (df["ts"].to_numpy(), df["open"].to_numpy(), df["close"].to_numpy())
    return out


def path(arr, ts0, horizons=(1, 4, 9, 27), back=(27, 90)):
    """Ход от открытия первой свечи после ts0 вперёд на horizons свечей и назад на back свечей (в %)."""
    ts, op, cl = arr
    i = int(np.searchsorted(ts, ts0, side="right"))
    if i + max(horizons) >= len(ts) or i - max(back) < 0 or ts[i] - ts0 > 3 * 86400:
        return None
    p0 = op[i]
    fw = {h: (cl[i + h - 1] / p0 - 1) * 100 for h in horizons}
    bk = {b: (p0 / cl[i - b] - 1) * 100 for b in back}
    return {"fw": fw, "bk": bk, "lag": (ts[i] - ts0) / 3600, "hour_msk": datetime.fromtimestamp(ts0, MSK).hour}


def hit_table(label, rows, key="x4"):
    if len(rows) < 15:
        print(f"  {label:48} мало данных (n={len(rows)})")
        return None
    x = np.array([r[key] for r in rows])
    res = {"n": len(rows), "mean": round(float(x.mean()), 3), "p_pos": round(float((x > 0).mean()), 2), "mean_abs": round(float(np.abs(x).mean()), 3)}
    print(f"  {label:48} n={res['n']:>5} средний {res['mean']:+.2f}% вверх в {res['p_pos']:.0%} |ход| {res['mean_abs']:.2f}%")
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2019-01-01")
    ap.add_argument("--split", default="2025-01-01")
    a = ap.parse_args()
    since_ts = int(datetime.strptime(a.since, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    split_ts = int(datetime.strptime(a.split, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    known = {p.name[:-7] for p in (ROOT / "data" / "history").glob("*_60.csv") if "USDT" not in p.name}
    hist = load_hist(known)
    idx = load_index()
    idx_arr = (idx["ts"].to_numpy(), idx["open"].to_numpy(), idx["close"].to_numpy())
    c = sqlite3.connect(ROOT / "data" / "news_archive.db")
    rows = c.execute("SELECT channel, ts, text FROM msgs WHERE ts>=? ORDER BY ts", (since_ts,)).fetchall()
    # события по компаниям: (тема, бумага) -> серия
    items, comp = [], []
    for ch, ts, text in rows:
        name, s = classify(text)
        if not name:
            continue
        items.append((ts, ch, name, s))
        if name in COMPANY_TOPICS:
            for tk in tickers_of(text, known):
                comp.append((ts, ch, f"{name}|{tk}", s))
    mk_events = build_events(items)
    co_events = build_events(comp)
    out = {}
    # ---- реакции
    rec = []
    for e in co_events:
        topic, tk = e["topic"].split("|")
        p = path(hist[tk], e["ts"])
        pi = path(idx_arr, e["ts"])
        if not p or not pi:
            continue
        rec.append({"topic": topic, "tk": tk, "ts": e["ts"], "sign": e["sign"], "n": e["n"], "hour": p["hour_msk"],
                    "x1": p["fw"][1] - pi["fw"][1], "x4": p["fw"][4] - pi["fw"][4], "x9": p["fw"][9] - pi["fw"][9], "x27": p["fw"][27] - pi["fw"][27],
                    "r1": p["fw"][1], "r4": p["fw"][4], "r9": p["fw"][9], "r27": p["fw"][27],
                    "runup10": p["bk"][90] - pi["bk"][90], "runup3": p["bk"][27] - pi["bk"][27]})
    mrec = []
    for e in mk_events:
        if e["topic"] in COMPANY_TOPICS:
            continue
        pi = path(idx_arr, e["ts"])
        if not pi:
            continue
        mrec.append({"topic": e["topic"], "ts": e["ts"], "sign": e["sign"], "n": e["n"], "hour": pi["hour_msk"], "bar": int(np.searchsorted(idx_arr[0], e["ts"], side="right")),
                     "r1": pi["fw"][1], "r4": pi["fw"][4], "r9": pi["fw"][9], "r27": pi["fw"][27], "pre3": pi["bk"][27], "pre10": pi["bk"][90]})
    # для блоков про реакцию индекса (2 и 4) один час рынка считается один раз: десять сообщений в один час — не десять наблюдений
    seen_bars, mrec_u = set(), []
    for r in mrec:
        if r["bar"] not in seen_bars:
            seen_bars.add(r["bar"])
            mrec_u.append(r)
    print(f"событий по компаниям с бумагой и свечами: {len(rec)}; событий по рынку: {len(mrec)} (уникальных часов {len(mrec_u)})")

    print("\n1. КУПИЛИ НА ОЖИДАНИИ, ПРОДАЛИ НА ФАКТЕ (бумага сверх индекса за 4 часа и за день после хорошей новости по компании)")
    good = [r for r in rec if r["sign"] > 0]
    out["runup"] = {}
    for lab, cond in (("без разгона (бумага отстала или ≤0% за 10 дн.)", lambda r: r["runup10"] <= 0), ("разгон 0–4% за 10 дней", lambda r: 0 < r["runup10"] < 4),
                      ("разгон 4%+ за 10 дней (фильтр бота)", lambda r: r["runup10"] >= 4), ("разгон 8%+ за 10 дней", lambda r: r["runup10"] >= 8)):
        sel = [r for r in good if cond(r)]
        out["runup"][lab] = {"h4": hit_table(lab + " → 4ч", sel, "x4"), "h9": hit_table(lab + " → день", sel, "x9"), "h27": hit_table(lab + " → 3 дня", sel, "x27")}
    print("  по темам, разгон 4%+ против без разгона (сверх индекса за день):")
    for topic in ("дивиденды", "отчётность", "выкуп / оферта", "слияния и сделки"):
        hi = [r for r in good if r["topic"] == topic and r["runup10"] >= 4]
        lo = [r for r in good if r["topic"] == topic and r["runup10"] <= 0]
        if len(hi) >= 15 and len(lo) >= 15:
            print(f"    {topic:20} разгон: {np.mean([r['x9'] for r in hi]):+.2f}% (n={len(hi)}) | без: {np.mean([r['x9'] for r in lo]):+.2f}% (n={len(lo)})")
    print("  плохая новость по компании после падения (капитуляция уже была?):")
    bad = [r for r in rec if r["sign"] < 0]
    out["capitulation"] = {}
    for lab, cond in (("бумага уже упала на 4%+ к индексу за 10 дн.", lambda r: r["runup10"] <= -4), ("бумага не падала", lambda r: r["runup10"] >= 0)):
        sel = [r for r in bad if cond(r)]
        out["capitulation"][lab] = {"h4": hit_table(lab + " → 4ч", sel, "x4"), "h9": hit_table(lab + " → день", sel, "x9")}

    print("\n2. ПЕРЕБОР И ОТКАТ: резкий первый час -> что дальше (ход после первого часа, в % )")
    out["overreaction"] = {}
    for lab, sel, key1, keys in (("бумаги, первый час +1.5%+ (эйфория)", [r for r in rec if r["x1"] >= 1.5], "x1", ("x4", "x9", "x27")),
                                 ("бумаги, первый час −1.5% и хуже (паника)", [r for r in rec if r["x1"] <= -1.5], "x1", ("x4", "x9", "x27")),
                                 ("индекс, первый час +0.8%+ (уник. часы)", [r for r in mrec_u if r["r1"] >= 0.8], "r1", ("r4", "r9", "r27")),
                                 ("индекс, первый час −0.8% и хуже (уник. часы)", [r for r in mrec_u if r["r1"] <= -0.8], "r1", ("r4", "r9", "r27"))):
        if len(sel) < 15:
            print(f"  {lab:48} мало данных (n={len(sel)})")
            continue
        d = {}
        for k in keys:
            cont = np.array([r[k] - r[key1] for r in sel])
            d[k] = {"n": len(sel), "mean_cont": round(float(cont.mean()), 3), "p_same_dir": round(float(((cont > 0) == (np.array([r[key1] for r in sel]) > 0)).mean()), 2)}
        out["overreaction"][lab] = d
        print(f"  {lab:48} n={len(sel):>5} дальше: до 4ч {d[keys[0]]['mean_cont']:+.2f}% (в ту же сторону {d[keys[0]]['p_same_dir']:.0%}), "
              f"до дня {d[keys[1]]['mean_cont']:+.2f}% ({d[keys[1]]['p_same_dir']:.0%}), до 3 дней {d[keys[2]]['mean_cont']:+.2f}% ({d[keys[2]]['p_same_dir']:.0%})")

    print("\n3. ОЖИДАНИЕ И ФАКТ: точечные события — ход индекса за 3 дня ДО и реакция ПОСЛЕ")
    out["pre_post"] = {}
    decision = re.compile(r"(сохранил|повысил|снизил|оставил|поднял|опустил)\w*[^.]{0,30}ключев\w+ ставк|ключев\w+ ставк\w*[^.]{0,40}(сохран|повыс|сниз|оставил|поднял|опустил)", re.I)
    cb = [r for r in mrec if r["topic"] == "ставка ЦБ" and r["n"] >= 3 and datetime.fromtimestamp(r["ts"], MSK).hour in (13, 14)]
    for lab, sel, keys in (("решение ЦБ (серия 3+ в 13–14 МСК)", cb, ("pre3", "r1", "r4", "r9", "r27")),
                           ("переговоры: событие из 5+ сообщений", [r for r in mrec if r["topic"] == "мирные переговоры / перемирие" and r["n"] >= 5], ("pre3", "r1", "r4", "r9", "r27")),
                           ("санкции: пакет, событие из 3+", [r for r in mrec if r["topic"] == "санкции: пакет / введение" and r["n"] >= 3], ("pre3", "r1", "r4", "r9", "r27"))):
        if len(sel) < 10:
            print(f"  {lab:48} мало данных (n={len(sel)})")
            continue
        d = {k: round(float(np.mean([r[k] for r in sel])), 3) for k in keys}
        up3 = [r for r in sel if r["pre3"] >= 1]
        dn3 = [r for r in sel if r["pre3"] <= -1]
        d["after_runup"] = round(float(np.mean([r["r9"] for r in up3])), 3) if len(up3) >= 8 else None
        d["after_drop"] = round(float(np.mean([r["r9"] for r in dn3])), 3) if len(dn3) >= 8 else None
        out["pre_post"][lab] = {**d, "n": len(sel)}
        print(f"  {lab:48} n={len(sel):>4} до (3 дня) {d['pre3']:+.2f}% | после: 1ч {d['r1']:+.2f}% 4ч {d['r4']:+.2f}% день {d['r9']:+.2f}% 3 дня {d['r27']:+.2f}%"
              + (f" | если рос ≥1% до: день {d['after_runup']:+.2f}%" if d["after_runup"] is not None else "")
              + (f" | если падал ≤−1% до: день {d['after_drop']:+.2f}%" if d["after_drop"] is not None else ""))
    print("  (решение ЦБ по тексту «сохранил/повысил/снизил»: сторона по словам)")
    cbd = [r for r in cb if r["sign"]]
    if len(cbd) >= 10:
        x = np.array([r["r4"] * r["sign"] for r in cbd])
        print(f"    в сторону слов (снижение = +): 4ч {x.mean():+.2f}%, угадано {((x > 0).mean()):.0%}, n={len(cbd)}")
        out["pre_post"]["cb_sign"] = {"n": len(cbd), "mean": round(float(x.mean()), 3), "hit": round(float((x > 0).mean()), 2)}

    print("\n4. ВРЕМЯ СУТОК (события всего рынка, |ход индекса за 4 часа| и доля >1%)")
    out["hours"] = {}
    for lab, cond in (("ночь/утро до 10 МСК", lambda h: h < 10), ("день 10–15", lambda h: 10 <= h < 15), ("день 15–19", lambda h: 15 <= h < 19), ("вечер 19–24 (розница)", lambda h: h >= 19)):
        sel = [r for r in mrec_u if cond(r["hour"])]
        if len(sel) >= 30:
            x = np.array([r["r4"] for r in sel])
            sg = np.array([r["sign"] for r in sel])
            m = sg != 0
            hit = float(((x[m] > 0) == (sg[m] > 0)).mean()) if m.sum() >= 30 else None
            out["hours"][lab] = {"n": len(sel), "mean_abs": round(float(np.abs(x).mean()), 3), "p_gt1": round(float((np.abs(x) > 1).mean()), 3), "hit": hit}
            print(f"  {lab:24} n={len(sel):>6} |4ч| {np.abs(x).mean():.2f}% >1% {(np.abs(x) > 1).mean():.0%} знак по словам {('%.0f%%' % (hit * 100)) if hit else '—'}")

    print(f"\n5. ВНЕ ВЫБОРКИ (граница {a.split}): правила психологии, найденные до границы, на данных после")
    out["oos"] = {}
    for lab, sel_fn, key in (("хорошая новость после разгона 4%+ → день (сверх индекса)", lambda r: r["sign"] > 0 and r["runup10"] >= 4, "x9"),
                             ("хорошая новость без разгона → день (сверх индекса)", lambda r: r["sign"] > 0 and r["runup10"] <= 0, "x9"),
                             ("плохая новость после падения 4%+ → день", lambda r: r["sign"] < 0 and r["runup10"] <= -4, "x9"),
                             ("эйфория: первый час +1.5%+ → дальше до дня", lambda r: r["x1"] >= 1.5, "cont9"),
                             ("паника: первый час −1.5% → дальше до дня", lambda r: r["x1"] <= -1.5, "cont9")):
        tr = [r for r in rec if sel_fn(r) and r["ts"] < split_ts]
        te = [r for r in rec if sel_fn(r) and r["ts"] >= split_ts]
        def val(r):
            return r["x9"] - r["x1"] if key == "cont9" else r[key]
        if len(tr) >= 15 and len(te) >= 10:
            a_, b_ = np.array([val(r) for r in tr]), np.array([val(r) for r in te])
            out["oos"][lab] = {"train": {"n": len(tr), "mean": round(float(a_.mean()), 3), "p_pos": round(float((a_ > 0).mean()), 2)},
                               "test": {"n": len(te), "mean": round(float(b_.mean()), 3), "p_pos": round(float((b_ > 0).mean()), 2)}}
            print(f"  {lab:52} до: {a_.mean():+.2f}% (вверх {((a_ > 0).mean()):.0%}, n={len(tr)}) | после: {b_.mean():+.2f}% (вверх {((b_ > 0).mean()):.0%}, n={len(te)})")
        else:
            print(f"  {lab:52} мало данных (до {len(tr)}, после {len(te)})")
    (ROOT / "data" / "news_psychology.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print("\nсохранено: data/news_psychology.json")


if __name__ == "__main__":
    main()
