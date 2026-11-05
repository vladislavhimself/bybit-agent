"""Больше сделок без потери качества: расширение списка бумаг и лимиты портфеля (позиций всего, на сектор, в одну сторону).
Правила входа и выхода — как в боте на 05.11.2026. Сделки симулятора проходят через портфельный фильтр по времени:
новая сделка принимается, только если есть свободный слот и не превышены лимиты.
Запуск: DAYS=2500 python research/moex_universe.py data/history"""
import os
import sys

import pandas as pd
import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import moex_sim as ms  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
H = sys.argv[1]
DAYS = int(os.environ.get("DAYS", "2500"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
cfg = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
SECTORS = cfg["markets"]["moex"]["risk"]["sectors"]
SEC_OF = {t: s for s, lst in SECTORS.items() for t in lst}

OLD = "SBER GAZP LKOH ROSN NVTK GMKN TATN SNGS VTBR MOEX AFLT YDEX OZON PLZL CHMF MGNT ALRS T X5 MTSS".split()
NEW = "VKCO SMLT SELG LSRG POSI AFKS SIBN SNGSP MVID MAGN TRNFP RUAL MTLR PHOR TATNP SGZH IRAO UPRO NLMK SVCB HYDR RTKM FLOT SBERP RNFT FEES PIKK BSPB".split()
EXITS = dict(stall_h=24, daily_exit=True, pyramid=0.5, pyr_be=True, peak_lock=(3.0, 0.5), tps=((3.0, 0.33),))
GATES = {"ext_short": 3.0}

f_old = ms.load(H, OLD, DAYS)
f_new = ms.load(H, NEW, DAYS)
print(f"старый список: {len(f_old)} бумаг; новые с историей: {len(f_new)} ({' '.join(sorted(f_new))})")
weeks = DAYS / 7.0
t_old = ms.run(f_old, GATES, **EXITS)
t_new = ms.run(f_new, GATES, **EXITS)
print("\n== Качество сигналов без портфельных лимитов ==")
print(f"{'старые 20 бумаг':28s}", ms.fmt_stats(ms.stats(t_old)), f"| {len(t_old) / weeks:.1f} в неделю")
print(f"{'новые бумаги':28s}", ms.fmt_stats(ms.stats(t_new)), f"| {len(t_new) / weeks:.1f} в неделю")
by = t_new.groupby("sym").r.agg(["count", "sum", "mean"]).sort_values("sum")
print("   новые по бумагам (сделок, итог R):", ", ".join(f"{s} {int(r['count'])}/{r['sum']:+.0f}" for s, r in by.iterrows()))
print("   новые по годам:", {y: round(ms.stats(g)["sum_r"]) for y, g in t_new.groupby(t_new.t.dt.year)})
# устойчивость к худшей ликвидности: проскальзывание вдвое больше
ms.SLIP = 0.001
t_new_slip = ms.run(f_new, GATES, **EXITS)
ms.SLIP = 0.0005
print(f"{'новые, проскальзывание x2':28s}", ms.fmt_stats(ms.stats(t_new_slip)))


def portfolio(trades, max_open, max_sector, max_side=4):
    """Принять сделки по времени с лимитами: всего позиций, на сектор, в одну сторону."""
    t = trades.sort_values("t").reset_index(drop=True)
    open_pos, keep, conc = [], [], []
    for row in t.itertuples():
        open_pos = [p for p in open_pos if p[0] > row.t]
        sec = SEC_OF.get(row.sym, "other_" + row.sym)
        if len(open_pos) >= max_open:
            continue
        if sum(1 for p in open_pos if p[1] == sec) >= max_sector:
            continue
        if sum(1 for p in open_pos if p[2] == row.sg) >= max_side:
            continue
        open_pos.append((row.t_exit, sec, row.sg))
        keep.append(row.Index)
        conc.append(len(open_pos))
    out = t.loc[keep]
    return out, (sum(conc) / len(conc) if conc else 0)


def show(label, trades, max_open, max_sector, max_side, risk_scale=1.0):
    p, avg_open = portfolio(trades, max_open, max_sector, max_side)
    s = ms.stats(p)
    yrs = {y: round(ms.stats(g)["sum_r"] * risk_scale) for y, g in p.groupby(p.t.dt.year)}
    print(f"{label:44s} {s['n']:5d} сд ({s['n'] / weeks:4.1f}/нед)  PF {s['pf']:.2f}  итог {s['sum_r']:+7.1f}R  "
          f"при равном риске портфеля {s['sum_r'] * risk_scale:+7.1f}  в среднем открыто {avg_open:.1f} | {yrs}")


both = pd.concat([t_old, t_new], ignore_index=True)
print("\n== Портфель с лимитами (позиций / на сектор / в одну сторону) ==")
show("старые 20, лимиты 5/2/4 (как сейчас)", t_old, 5, 2, 4)
show("старые 20, лимиты 8/3/6", t_old, 8, 3, 6, 5 / 8)
show("все бумаги, лимиты 5/2/4", both, 5, 2, 4)
show("все бумаги, лимиты 7/2/5", both, 7, 2, 5, 5 / 7)
show("все бумаги, лимиты 8/3/6", both, 8, 3, 6, 5 / 8)
show("все бумаги, лимиты 10/3/7", both, 10, 3, 7, 5 / 10)
show("все бумаги, лимиты 12/4/8", both, 12, 4, 8, 5 / 12)
show("все бумаги, без лимитов", both, 999, 999, 999, 0)


# ---------- устойчивость по бумагам: убыток в обеих половинах истории = свойство бумаги, а не случайность ----------
mid = both.t.min() + (both.t.max() - both.t.min()) / 2
print(f"\n== Новые бумаги по половинам истории (граница {mid.date()}) ==")
bad = []
for sym, g in t_new.groupby("sym"):
    a, b = g[g.t < mid].r.sum(), g[g.t >= mid].r.sum()
    flag = ""
    if a < 0 and b < 0:
        bad.append(sym)
        flag = "  <- минус в обеих половинах"
    print(f"   {sym:6s} сделок {len(g):4d}  первая {a:+6.1f}R  вторая {b:+6.1f}R  итого {a + b:+6.1f}R{flag}")
print("   кандидаты в стоп-лист:", bad)
for sym, g in t_old.groupby("sym"):
    a, b = g[g.t < mid].r.sum(), g[g.t >= mid].r.sum()
    if a < 0 and b < 0:
        print(f"   (старый список) {sym}: первая {a:+.1f}R, вторая {b:+.1f}R")

# одна компания = одна позиция: обычные и привилегированные акции считаем одной бумагой (как max_per_base в боте)
PREF = {"SBERP": "SBER", "TATNP": "TATN", "SNGSP": "SNGS"}


def portfolio2(trades, max_open, max_sector, max_side, skip=()):
    t = trades[~trades.sym.isin(skip)].sort_values("t").reset_index(drop=True)
    open_pos, keep = [], []
    for row in t.itertuples():
        open_pos = [p for p in open_pos if p[0] > row.t]
        base = PREF.get(row.sym, row.sym)
        sec = SEC_OF.get(row.sym, "other_" + row.sym)
        if len(open_pos) >= max_open or any(p[3] == base for p in open_pos):
            continue
        if sum(1 for p in open_pos if p[1] == sec) >= max_sector or sum(1 for p in open_pos if p[2] == row.sg) >= max_side:
            continue
        open_pos.append((row.t_exit, sec, row.sg, base))
        keep.append(row.Index)
    return t.loc[keep]


def show2(label, trades, lim, skip=()):
    p = portfolio2(trades, *lim, skip=skip)
    s = ms.stats(p)
    yrs = {y: round(ms.stats(g)["sum_r"]) for y, g in p.groupby(p.t.dt.year)}
    h1, h2 = p[p.t < mid].r.sum(), p[p.t >= mid].r.sum()
    print(f"{label:52s} {s['n']:5d} сд ({s['n'] / weeks:4.1f}/нед)  PF {s['pf']:.2f}  итог {s['sum_r']:+7.1f}R  половины {h1:+.0f}/{h2:+.0f} | {yrs}")


print("\n== Портфель 5/2/4 с секторами из настроек и правилом «одна компания — одна позиция» ==")
show2("старые 20", t_old, (5, 2, 4))
show2("все 48", both, (5, 2, 4))
show2("все 48 без кандидатов в стоп-лист", both, (5, 2, 4), skip=bad)
# честная проверка стоп-листа: выбрать его по первой половине, посмотреть результат на второй
bad1 = [sym for sym, g in t_new.groupby("sym") if g[g.t < mid].r.sum() < 0]
p_all = portfolio2(both, 5, 2, 4)
p_flt = portfolio2(both, 5, 2, 4, skip=bad1)
print(f"   стоп-лист только по первой половине: {bad1}")
print(f"   вторая половина: все 48 {p_all[p_all.t >= mid].r.sum():+.1f}R; без этого стоп-листа {p_flt[p_flt.t >= mid].r.sum():+.1f}R")
ms.SLIP = 0.001
t_all_slip = pd.concat([ms.run(f_old, GATES, **EXITS), ms.run(f_new, GATES, **EXITS)], ignore_index=True)
ms.SLIP = 0.0005
show2("все 48, проскальзывание x2", t_all_slip, (5, 2, 4))
show2("все 48 без стоп-листа, проскальзывание x2", t_all_slip, (5, 2, 4), skip=bad)
