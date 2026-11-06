"""Психология вокруг дивидендов на истории (без вызовов модели):
1) «купили на ожидании, продали на факте»: ход бумаги против рынка ДО рекомендации совета директоров, в день факта и после.
   Даты рекомендаций — из архива новостей 2024–2026 (заголовки про рекомендацию дивидендов).
2) «дивидендный ход до отсечки»: ход против рынка в последние дни перед последним днём покупки под дивиденд.
   Даты отсечек — из справочника брокера (data/dividends_history.json), 2019–2026.
Рынок = равновзвешенная корзина всех бумаг с дневной историей. Запуск: python research/moex_dividend_events.py data/history"""
import glob
import json
import os
import re
import sqlite3
import sys

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
H = sys.argv[1]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP = {"BTC", "DXY", "GOLD", "USDRUBF", "CNYRUBF", "IMOEXF"}
closes = {}
for p in glob.glob(os.path.join(H, "*_D.csv")):
    sym = os.path.basename(p)[:-6]
    if sym in SKIP or sym.endswith("F"):
        continue
    df = pd.read_csv(p)
    if len(df) < 200 or "ts" not in df.columns or "close" not in df.columns:
        continue
    ts = df.ts * 1000 if df.ts.max() < 1e11 else df.ts
    closes[sym] = pd.Series(df.close.values, index=pd.DatetimeIndex(pd.to_datetime(ts + 3 * 3600 * 1000, unit="ms")).normalize()).groupby(level=0).last()   # дата по Москве
px = pd.DataFrame(closes).sort_index()
px = px[px.index.dayofweek < 5]
ret = px.pct_change()
days = px.index
# Корзина «рынок» должна быть с учётом дивидендов: в сезон отсечек десятки бумаг падают на размер дивиденда, и ценовая
# корзина проседает сама по себе — ход отдельной бумаги «сверх рынка» получался завышенным. В день гэпа каждой бумаге
# возвращаем её дивиденд (полная доходность).
_divs = json.load(open(os.path.join(ROOT, "data", "dividends_history.json"), encoding="utf-8"))
tr = ret.copy()
for _sym, _lst in _divs.items():
    if _sym not in tr.columns:
        continue
    for _x in _lst:
        if not _x["last_buy"] or not _x["yield"]:
            continue
        _d = pd.Timestamp(_x["last_buy"])
        _i = days.searchsorted(_d)
        if _i >= len(days) or days[_i] != _d:
            continue
        _g = _i if _d < pd.Timestamp("2023-07-31") else _i + 1     # день гэпа (до Т+1 дата в справочнике на день позже)
        if _g < len(days):
            tr.iloc[_g, tr.columns.get_loc(_sym)] += _x["yield"] / 100
mkt = tr.mean(axis=1)            # равновзвешенная корзина полной доходности
exc = ret.sub(mkt, axis=0)       # ход бумаги сверх рынка за день
print(f"бумаг с дневной историей: {px.shape[1]}, дней: {len(days)} ({days[0].date()} — {days[-1].date()})")


def window(sym, i0, i1):
    """Сумма дневных ходов сверх рынка за дни с номерами i0..i1 включительно, %."""
    if sym not in exc or i0 < 1 or i1 >= len(days) or i0 > i1:
        return np.nan
    v = exc[sym].iloc[i0:i1 + 1]
    return float(v.sum() * 100) if v.notna().all() else np.nan


def show(label, vals):
    v = pd.Series(vals).dropna()
    if len(v) < 12:
        print(f"   {label:58s} n={len(v):4d}  мало данных")
        return
    t = v.mean() / (v.std() / np.sqrt(len(v))) if v.std() > 0 else 0
    print(f"   {label:58s} n={len(v):4d}  в среднем {v.mean():+.2f}%  медиана {v.median():+.2f}%  в плюс {(v > 0).mean() * 100:.0f}%  t={t:+.1f}")


# ---------- 1. рекомендация дивидендов: до факта, факт, после ----------
c = sqlite3.connect(f"file:{os.path.join(ROOT, 'data', 'news_archive.db')}?mode=ro", uri=True)
rows = c.execute("""select m.ts, e.affected, m.text from msgs m join events e on e.channel=m.channel and e.id=m.id
                    where e.relevant=1""").fetchall()
rows = [(ts, aff, text) for ts, aff, text in rows if text and re.search(r"дивиденд", text[:400], re.I)]
pat = re.compile(r"рекоменд|совет директоров|СД |набсовет|наблюдательн", re.I)
neg = re.compile(r"не выплачивать|отказ|не рекоменд|без дивиденд", re.I)
ann = {}
for ts, aff, title in rows:
    if not pat.search((title or "")[:400]) or neg.search((title or "")[:400]):
        continue
    try:
        syms = [s for s in json.loads(aff or "[]") if s in px.columns]
    except ValueError:
        continue
    if len(syms) != 1:
        continue
    d = pd.Timestamp(ts + 3 * 3600, unit="s").normalize()
    ann.setdefault(syms[0], []).append(d)
events = []
for sym, ds in ann.items():
    last = None
    for d in sorted(set(ds)):
        if last is None or (d - last).days > 25:     # первая новость в серии = день факта
            events.append((sym, d))
        last = d
print(f"\n== 1. Рекомендации дивидендов из архива новостей: {len(events)} событий по {len(ann)} бумагам ==")
pre10, pre5, day0, post5, post10 = [], [], [], [], []
hot_day0, hot_post5, cold_day0 = [], [], []
for sym, d in events:
    i = days.searchsorted(d)
    if i >= len(days) or days[i] != d:
        continue
    a, b, z, p5, p10 = window(sym, i - 10, i - 1), window(sym, i - 5, i - 1), window(sym, i, i), window(sym, i + 1, i + 5), window(sym, i + 1, i + 10)
    pre10.append(a); pre5.append(b); day0.append(z); post5.append(p5); post10.append(p10)
    if not np.isnan(a):
        (hot_day0 if a >= 3 else cold_day0).append(z)
        if a >= 3:
            hot_post5.append(p5)
show("10 дней ДО факта (сделка «на ожидании»)", pre10)
show("5 дней ДО факта", pre5)
show("день факта", day0)
show("5 дней ПОСЛЕ факта", post5)
show("10 дней ПОСЛЕ факта", post10)
show("день факта, если до него бумага обогнала рынок на 3%+", hot_day0)
show("5 дней после, если до факта обогнала на 3%+", hot_post5)
show("день факта, если до него НЕ обгоняла", cold_day0)

# ---------- 2. ход до отсечки ----------
divs = json.load(open(os.path.join(ROOT, "data", "dividends_history.json"), encoding="utf-8"))
print("\n== 2. Последние дни перед отсечкой (бумага сверх рынка), дивидендная доходность от 3% ==")
res = {5: [], 10: [], 20: []}
big = {5: [], 10: [], 20: []}
gap = []
for sym, lst in divs.items():
    if sym not in px.columns:
        continue
    for x in lst:
        if not x["last_buy"] or x["yield"] < 3:
            continue
        i = days.searchsorted(pd.Timestamp(x["last_buy"]))
        if i >= len(days) - 1 or days[i] != pd.Timestamp(x["last_buy"]):
            continue
        if pd.Timestamp(x["last_buy"]) < pd.Timestamp("2023-07-31"):
            i -= 1     # до перехода на расчёты Т+1 дата в справочнике брокера на день позже настоящего последнего дня покупки (см. проверку ниже)
        for k in res:
            v = window(sym, i - k + 1, i)
            res[k].append(v)
            if x["yield"] >= 7:
                big[k].append(v)
        if not np.isnan(ret[sym].iloc[i + 1]):
            gap.append(float(ret[sym].iloc[i + 1] * 100 + x["yield"]))     # гэп на отсечке плюс дивиденд: сколько осталось держателю
for k in res:
    show(f"последние {k} дней до отсечки, продажа в последний день", res[k])
for k in big:
    show(f"то же при доходности от 7%: {k} дней", big[k])
show("пересидеть отсечку: дивиденд минус гэп (до налога 13%)", gap)

# ---------- диагностика дат: в какой день относительно «последнего дня покупки» из справочника происходит гэп ----------
print("\n== Проверка дат: средний ход сверх рынка по дням относительно даты last_buy из справочника (доходность от 5%) ==")
for label, cond in (("отсечки до 31.07.2023 (расчёты Т+2)", lambda d: d < pd.Timestamp("2023-07-31")), ("отсечки после (расчёты Т+1)", lambda d: d >= pd.Timestamp("2023-07-31"))):
    acc = {k: [] for k in range(-3, 4)}
    for sym, lst in divs.items():
        if sym not in px.columns:
            continue
        for x in lst:
            if not x["last_buy"] or x["yield"] < 5 or not cond(pd.Timestamp(x["last_buy"])):
                continue
            i = days.searchsorted(pd.Timestamp(x["last_buy"]))
            if i < 5 or i >= len(days) - 4 or days[i] != pd.Timestamp(x["last_buy"]):
                continue
            for k in acc:
                v = exc[sym].iloc[i + k]
                if not np.isnan(v):
                    acc[k].append(v * 100)
    print("  ", label, "n =", len(acc[0]), "|", "  ".join(f"{k:+d}: {np.mean(v):+.2f}%" for k, v in acc.items() if v))

# ---------- устойчивость хода до отсечки: по годам, в абсолюте и с вычетом рынка ----------
print("\n== Ход за 20 и 10 торговых дней до отсечки по годам (доходность от 5%): сверх рынка / сама бумага ==")
rows_y = []
for sym, lst in divs.items():
    if sym not in px.columns:
        continue
    for x in lst:
        if not x["last_buy"] or x["yield"] < 5:
            continue
        d0 = pd.Timestamp(x["last_buy"])
        i = days.searchsorted(d0)
        if i >= len(days) - 1 or days[i] != d0:
            continue
        if d0 < pd.Timestamp("2023-07-31"):
            i -= 1
        if i < 21 or px[sym].iloc[i - 20:i + 1].isna().any():
            continue
        rows_y.append({"year": d0.year, "sym": sym, "y": x["yield"], "e20": window(sym, i - 19, i), "e10": window(sym, i - 9, i),
                       "a20": (px[sym].iloc[i] / px[sym].iloc[i - 20] - 1) * 100, "a10": (px[sym].iloc[i] / px[sym].iloc[i - 10] - 1) * 100})
y = pd.DataFrame(rows_y)
for yr, g in y.groupby("year"):
    print(f"   {yr}: n={len(g):3d}  20 дней: сверх рынка {g.e20.mean():+.1f}% (в плюс {(g.e20 > 0).mean() * 100:.0f}%), бумага {g.a20.mean():+.1f}% (в плюс {(g.a20 > 0).mean() * 100:.0f}%)"
          f"  |  10 дней: сверх рынка {g.e10.mean():+.1f}%, бумага {g.a10.mean():+.1f}% (в плюс {(g.a10 > 0).mean() * 100:.0f}%)")
print(f"   всего: n={len(y)}  20 дней: сверх рынка {y.e20.mean():+.2f}%, бумага {y.a20.mean():+.2f}% (в плюс {(y.a20 > 0).mean() * 100:.0f}%), худшая {y.a20.min():+.1f}%"
      f"  |  10 дней: бумага {y.a10.mean():+.2f}% (в плюс {(y.a10 > 0).mean() * 100:.0f}%), худшая {y.a10.min():+.1f}%")
