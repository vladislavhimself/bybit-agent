"""Проверка «правил» из news_expectations / news_psychology как ТОРГОВЛИ на истории, а не как средних ходов.

Вопрос пользователя 07.11.2026: «Точно ли уверен? Проверил на истории как сделки? Приносит плюс?»
Правило A — сюрприз ЦБ по облигациям: через 4 часа после решения смотрим RGBI; упал ≥ порога → шорт индекса, вырос → лонг.
  Вход по открытию следующей свечи (18:00 МСК), удержание H свечей (45 ≈ неделя, 90 ≈ 2 недели, 180 ≈ месяц) или стоп по часовому закрытию.
Правило B — моментум индекса: часовая свеча индекса +0.8% и больше (с новостью рядом или без) → лонг по открытию следующей свечи,
  выход в конце дня / через 3 дня / по стопу.
Комиссия и проскальзывание: 0.2% на круг. Без плеча, без реинвестирования: результат = сумма % по сделкам.
Сравнение с фоном: средний ход индекса за ту же длину по всем свечам периода (дрейф рынка) и случайные входы той же длины.
Запуск из корня: python research/backtest_expectations.py [--with-news]. Модель не вызывается."""
import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
MSK = ZoneInfo("Europe/Moscow")
COST = 0.2  # % на круг
SPLIT = int(datetime(2024, 1, 1, tzinfo=MSK).timestamp())


def load(sym):
    df = pd.read_csv(ROOT / "data" / "history" / f"{sym}_60.csv").sort_values("ts").drop_duplicates("ts").reset_index(drop=True)
    return df["ts"].to_numpy(), df["open"].to_numpy(), df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy()


def day_of(ts):
    return datetime.fromtimestamp(ts, MSK).date()


def simulate(arr, i_entry, side, hold, stop, to_day_end=False, trailing=None, target=None, thesis=None, opp=None):
    """Вход по открытию свечи i_entry. Выходы (по часовому закрытию): стоп stop%; трейлинг trailing% от лучшего закрытия;
    цель target%; thesis(j) -> True = тезис сломался; opp(j) -> True = противоположное событие; иначе через hold свечей / в конце дня.
    Возвращает (% с учётом комиссии, индекс свечи выхода, причина)."""
    ts, op, hi, lo, cl = arr
    if i_entry >= len(ts):
        return None
    p0 = op[i_entry]
    last = i_entry + hold - 1
    if to_day_end:
        d = day_of(ts[i_entry])
        last = i_entry
        while last + 1 < len(ts) and day_of(ts[last + 1]) == d:
            last += 1
    if last >= len(ts):
        return None
    best = -1e9
    for j in range(i_entry, last + 1):
        r = side * (cl[j] / p0 - 1) * 100
        best = max(best, r)
        if stop is not None and r <= -stop:
            return r - COST, j, "стоп"
        if target is not None and r >= target:
            return r - COST, j, "цель"
        if trailing is not None and best > 0 and best - r >= trailing:
            return r - COST, j, "трейлинг"
        if thesis is not None and j > i_entry and thesis(j):
            return r - COST, j, "тезис"
        if opp is not None and j > i_entry and opp(j):
            return r - COST, j, "против"
    return side * (cl[last] / p0 - 1) * 100 - COST, last, "время"


def report(name, trades, arr, hold, reasons=None):
    """trades: list of (ts_entry, pnl%). Печать: n, win, средний, сумма, t, просадка, по годам, до/после 2024, фон."""
    if not trades:
        print(f"  {name}: сделок нет")
        return
    ts, op, hi, lo, cl = arr
    pn = np.array([p for _, p in trades])
    if reasons:
        cnt = defaultdict(list)
        for (t0, p), why in zip(trades, reasons):
            cnt[why].append(p)
        name += "  [выходы: " + ", ".join(f"{w} {len(v)} ({np.mean(v):+.1f}%)" for w, v in sorted(cnt.items(), key=lambda kv: -len(kv[1]))) + "]"
    cum = np.cumsum(pn)
    dd = float((np.maximum.accumulate(cum) - cum).max())
    t = pn.mean() / (pn.std(ddof=1) / np.sqrt(len(pn))) if len(pn) > 2 and pn.std() > 0 else 0.0
    # фон: средний ход индекса за hold свечей по всем точкам периода (лонг), и то же с комиссией
    n = len(ts)
    fw = (cl[hold - 1:] / op[: n - hold + 1] - 1) * 100
    base = float(fw.mean()) - COST
    # случайные входы той же длины и того же количества, 2000 раз: доля случаев, когда случайная сумма >= наша
    rng = np.random.default_rng(7)
    rand_sums = np.array([fw[rng.integers(0, len(fw), len(pn))].sum() - COST * len(pn) for _ in range(2000)])
    p_rand = float((rand_sums >= cum[-1]).mean())
    by_year = defaultdict(list)
    for t0, p in trades:
        by_year[datetime.fromtimestamp(t0, MSK).year].append(p)
    before = [p for t0, p in trades if t0 < SPLIT]
    after = [p for t0, p in trades if t0 >= SPLIT]
    print(f"  {name}")
    print(f"    сделок {len(pn)}, прибыльных {np.mean(pn > 0):.0%}, средняя {pn.mean():+.2f}%, медиана {np.median(pn):+.2f}%, сумма {cum[-1]:+.1f}%, "
          f"t={t:.2f}, макс. просадка {dd:.1f}%")
    print(f"    до 2024: n={len(before)} сумма {sum(before):+.1f}% (приб. {np.mean(np.array(before) > 0) if before else 0:.0%}) | "
          f"после 2024: n={len(after)} сумма {sum(after):+.1f}% (приб. {np.mean(np.array(after) > 0) if after else 0:.0%})")
    print(f"    фон: средний лонг на ту же длину по всем свечам {base:+.2f}% за сделку; случайные входы дают сумму ≥ нашей в {p_rand:.0%} случаев")
    print("    по годам: " + ", ".join(f"{y}: {sum(v):+.1f}% ({len(v)})" for y, v in sorted(by_year.items())))
    worst = sorted(trades, key=lambda x: x[1])[:3]
    best = sorted(trades, key=lambda x: -x[1])[:3]
    print("    лучшие: " + "; ".join(f"{datetime.fromtimestamp(t0, MSK).date()} {p:+.1f}%" for t0, p in best) +
          " | худшие: " + "; ".join(f"{datetime.fromtimestamp(t0, MSK).date()} {p:+.1f}%" for t0, p in worst))


def opp_fn(idx, news_hours, side, thr=0.8):
    """Противоположное событие: свеча с новостью «на весь рынок», чей ход против позиции ≥ thr%."""
    ts, op, hi, lo, cl = idx
    r1 = (cl / op - 1) * 100
    if news_hours is None:
        return None
    return lambda j: int(ts[j]) in news_hours and side * r1[j] <= -thr


def rule_a2(idx, rgbi, decisions, news_hours):
    """Правило A с выходами по причине, горизонт ≤ 2 недель."""
    print("\nПРАВИЛО A (≤ 2 недель, выход по причине). Сюрприз ЦБ по облигациям |RGBI 4ч| ≥ 0.15% → индекс в ту же сторону")
    ts, op, hi, lo, cl = idx
    rts, rop, rhi, rlo, rcl = rgbi
    rclose = dict(zip(rts.tolist(), rcl.tolist()))
    ropen = dict(zip(rts.tolist(), rop.tolist()))
    variants = [("неделя по времени", dict(hold=45)), ("2 недели по времени", dict(hold=90)),
                ("2 нед + стоп 2%", dict(hold=90, stop=2.0)), ("2 нед + трейлинг 2%", dict(hold=90, trailing=2.0)),
                ("2 нед + цель +3% / стоп −2%", dict(hold=90, target=3.0, stop=2.0)),
                ("2 нед + тезис (RGBI вернулся к уровню до решения)", dict(hold=90, thesis=True)),
                ("2 нед + противоположный шок (новость, час ≥0.8% против)", dict(hold=90, opp=True)),
                ("2 нед + тезис + трейлинг 2% + стоп 2%", dict(hold=90, thesis=True, trailing=2.0, stop=2.0)),
                ("неделя + тезис + стоп 2%", dict(hold=45, thesis=True, stop=2.0))]
    for name, kw in variants:
        trades, reasons = [], []
        for d in decisions:
            g = d.get("rgbi_4h")
            if g is None or abs(g) < 0.15:
                continue
            side = 1 if g > 0 else -1
            i = int(np.searchsorted(ts, d["ts"] + 4 * 3600 + 600, side="right"))
            # уровень RGBI до решения: открытие свечи 13:00 в день решения
            i13 = int(np.searchsorted(rts, d["ts"] - 1800, side="right")) - 1
            pre = rop[i13] if 0 <= i13 < len(rts) else None
            thesis = None
            if kw.get("thesis") and pre is not None:
                thesis = lambda j, side=side, pre=pre: (lambda v: v is not None and side * (v / pre - 1) <= 0)(rclose.get(int(ts[j])))
            opp = opp_fn(idx, news_hours, side) if kw.get("opp") else None
            r = simulate(idx, i, side, kw["hold"], kw.get("stop"), trailing=kw.get("trailing"), target=kw.get("target"), thesis=thesis, opp=opp)
            if r is not None:
                trades.append((ts[i], r[0]))
                reasons.append(r[2])
        report(name, trades, idx, kw["hold"], reasons)


def rule_b2(idx, news_hours):
    """Правило B с выходами по причине, горизонт ≤ 2 недель."""
    print("\nПРАВИЛО B (≤ 2 недель, выход по причине). Час индекса +0.8% → лонг по открытию следующей свечи")
    ts, op, hi, lo, cl = idx
    r1 = (cl / op - 1) * 100
    variants = [("до конца дня", dict(hold=9, day_end=True)), ("3 дня по времени", dict(hold=27)), ("неделя по времени", dict(hold=45)),
                ("2 недели по времени", dict(hold=90)), ("2 нед + стоп 1.5%", dict(hold=90, stop=1.5)),
                ("2 нед + трейлинг 1.5%", dict(hold=90, trailing=1.5)), ("2 нед + цель +2% / стоп −1.5%", dict(hold=90, target=2.0, stop=1.5)),
                ("2 нед + тезис (закрытие ниже минимума свечи-импульса)", dict(hold=90, thesis=True)),
                ("2 нед + противоположный шок", dict(hold=90, opp=True)),
                ("2 нед + тезис + трейлинг 1.5%", dict(hold=90, thesis=True, trailing=1.5)),
                ("неделя + тезис + трейлинг 1.5%", dict(hold=45, thesis=True, trailing=1.5))]
    for cond_name in ("любой час", "только с новостью «на весь рынок»"):
        if "новост" in cond_name and news_hours is None:
            continue
        print(f"\n  --- {cond_name} ---")
        for name, kw in variants:
            trades, reasons = [], []
            last_day = None
            for i in range(1, len(ts) - 200):
                h = datetime.fromtimestamp(ts[i], MSK).hour
                if r1[i] < 0.8 or h < 10 or h > 17:
                    continue
                if "новост" in cond_name and int(ts[i]) not in news_hours:
                    continue
                d = day_of(ts[i])
                if d == last_day:
                    continue
                low_imp = lo[i]
                thesis = (lambda j, low_imp=low_imp: cl[j] < low_imp) if kw.get("thesis") else None
                opp = opp_fn(idx, news_hours, 1) if kw.get("opp") else None
                r = simulate(idx, i + 1, 1, kw["hold"], kw.get("stop"), to_day_end=kw.get("day_end", False), trailing=kw.get("trailing"),
                             target=kw.get("target"), thesis=thesis, opp=opp)
                if r is not None:
                    trades.append((ts[i + 1], r[0]))
                    reasons.append(r[2])
                    last_day = d
            report(name, trades, idx, kw["hold"], reasons)


def rule_a(idx, rgbi, decisions):
    print("\nПРАВИЛО A. Сюрприз ЦБ по облигациям (RGBI за 4 ч после решения) → индекс в ту же сторону")
    ts, op, hi, lo, cl = idx
    for thr in (0.10, 0.15, 0.25):
        for hold, lab in ((45, "неделя"), (90, "2 недели"), (180, "месяц")):
            for stop in (None, 2.0, 3.0):
                trades = []
                for d in decisions:
                    g = d.get("rgbi_4h")
                    if g is None or abs(g) < thr:
                        continue
                    side = 1 if g > 0 else -1
                    i = int(np.searchsorted(ts, d["ts"] + 4 * 3600 + 600, side="right"))  # первая свеча после окна 4 ч (18:00)
                    r = simulate(idx, i, side, hold, stop)
                    if r is not None:
                        trades.append((ts[i], r[0]))
                report(f"порог {thr:.2f}%, {lab}, стоп {stop or 'нет'}", trades, idx, hold)
    # контроль: тот же вход, но сторона по СЛОВАМ (снизил → лонг, повысил → шорт), без облигаций
    print("\n  контроль: сторона по решению (снизил → лонг, повысил → шорт), неделя, без стопа")
    trades = []
    for d in decisions:
        if d.get("prev") is None or d["actual"] == d["prev"]:
            continue
        side = 1 if d["actual"] < d["prev"] else -1
        i = int(np.searchsorted(ts, d["ts"] + 4 * 3600 + 600, side="right"))
        r = simulate(idx, i, side, 45, None)
        if r is not None:
            trades.append((ts[i], r[0]))
    report("по словам, неделя", trades, idx, 45)
    print("\n  контроль: сторона по консенсусу из архива (факт ниже ожиданий → лонг, выше → шорт), неделя")
    trades = []
    for d in decisions:
        sp = d.get("surprise_pp")
        if sp is None or abs(sp) < 0.25:
            continue
        side = 1 if sp < 0 else -1
        i = int(np.searchsorted(ts, d["ts"] + 4 * 3600 + 600, side="right"))
        r = simulate(idx, i, side, 45, None)
        if r is not None:
            trades.append((ts[i], r[0]))
    report("по консенсусу, неделя", trades, idx, 45)


def rule_b(idx, news_hours):
    print("\nПРАВИЛО B. Моментум индекса: часовая свеча +0.8% → лонг по открытию следующей")
    ts, op, hi, lo, cl = idx
    r1 = (cl / op - 1) * 100
    for cond_name, cond in (("любой час (без новостей)", lambda i: True),
                            ("только если была новость «на весь рынок» в этот час", lambda i: news_hours is not None and int(ts[i]) in news_hours)):
        if "новость" in cond_name and news_hours is None:
            print(f"  {cond_name}: пропущено (запусти с --with-news)")
            continue
        for thr in (0.8, 1.2):
            for exit_name, hold, stop, day_end in (("до конца дня", 9, None, True), ("3 дня, без стопа", 27, None, False), ("3 дня, стоп 1%", 27, 1.0, False),
                                                   ("неделя, стоп 1.5%", 45, 1.5, False)):
                trades = []
                last_day = None
                for i in range(1, len(ts) - 200):
                    h = datetime.fromtimestamp(ts[i], MSK).hour
                    if r1[i] < thr or h < 10 or h > 17 or not cond(i):
                        continue
                    d = day_of(ts[i])
                    if d == last_day:
                        continue  # одна сделка в день
                    r = simulate(idx, i + 1, 1, hold, stop, to_day_end=day_end)
                    if r is not None:
                        trades.append((ts[i + 1], r[0]))
                        last_day = d
                report(f"{cond_name}; порог {thr}%; {exit_name}", trades, idx, hold)
        # зеркало: −0.8% → шорт
        trades = []
        last_day = None
        for i in range(1, len(ts) - 200):
            h = datetime.fromtimestamp(ts[i], MSK).hour
            if r1[i] > -0.8 or h < 10 or h > 17 or not cond(i):
                continue
            d = day_of(ts[i])
            if d == last_day:
                continue
            r = simulate(idx, i + 1, -1, 27, None)
            if r is not None:
                trades.append((ts[i + 1], r[0]))
                last_day = d
        report(f"{cond_name}; зеркало: −0.8% → шорт, 3 дня", trades, idx, 27)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-news", action="store_true")
    ap.add_argument("--exits", action="store_true", help="вариант ≤ 2 недель с выходами по причине (вместо простых удержаний)")
    a = ap.parse_args()
    idx = load("IMOEX")
    rgbi = load("RGBI")
    decisions = json.load(open(ROOT / "data" / "news_expectations.json", encoding="utf-8"))["cb"]
    print(f"индекс: {len(idx[0])} свечей, решений ЦБ: {len(decisions)}, комиссия {COST}% на круг")
    if not a.exits:
        rule_a(idx, rgbi, decisions)
    news_hours = None
    if a.with_news:
        from research.news_event_study import TOPICS, build_events, load_msgs
        mw = {name for name, m, _, _ in TOPICS if m}
        events = build_events(load_msgs(int(datetime(2019, 1, 1, tzinfo=MSK).timestamp())))
        ts = idx[0]
        news_hours = set()
        for e in events:
            if e["topic"] in mw:
                i = int(np.searchsorted(ts, e["ts"], side="right"))
                if i < len(ts) and ts[i] - e["ts"] <= 3 * 3600:
                    news_hours.add(int(ts[i]))  # свеча, первая после новости — её ход и есть «первый час»
        print(f"часов с новостью на весь рынок: {len(news_hours)}")
    if a.exits:
        rule_a2(idx, rgbi, decisions, news_hours)
        rule_b2(idx, news_hours)
    else:
        rule_b(idx, news_hours)


if __name__ == "__main__":
    main()
