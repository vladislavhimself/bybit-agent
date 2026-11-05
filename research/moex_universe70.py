"""Пул из ~70 бумаг: что добавляют бумаги с оборотом 80-360 млн руб. в день к проверенным ~50.
У тонких бумаг шире спред, поэтому считаем их с проскальзыванием x1, x2 и x3 (0.05%, 0.10%, 0.15% на сторону).
Запуск: DAYS=2500 python research/moex_universe70.py data/history"""
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
cfg = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))["markets"]["moex"]
SEC_OF = {t: s for s, lst in cfg["risk"]["sectors"].items() for t in lst}
ALIAS = cfg["risk"].get("base_alias") or {}
BLACK = set(cfg["universe"].get("blacklist") or [])

CORE = ("SBER GAZP LKOH ROSN NVTK GMKN TATN SNGS VTBR MOEX AFLT YDEX OZON PLZL CHMF MGNT ALRS T X5 MTSS "
        "VKCO SMLT SELG LSRG POSI AFKS SIBN SNGSP MVID MAGN TRNFP RUAL MTLR PHOR TATNP SGZH IRAO UPRO NLMK SVCB HYDR RTKM FLOT SBERP RNFT FEES PIKK BSPB "
        "NMTP SPBE FIXR DOMRF WUSH RAGR UWGN").split()
CORE = [s for s in CORE if s not in BLACK]
THIN = "CNRU UGLD VSEH HEAD ASTR ETLN SFIN ENPG ROLO TRMK FESH BELU MSNG DATA CBOM UNAC MTLRP IRKT IVAT MRKV MDMG OZPH RENI PRMD LENT GLRX LPSB".split()
EXITS = dict(stall_h=24, daily_exit=True, pyramid=0.5, pyr_be=True, peak_lock=(3.0, 0.5), tps=((3.0, 0.33),))
GATES = {"ext_short": 3.0}
weeks = DAYS / 7.0

f_core = ms.load(H, CORE, DAYS)
f_thin = ms.load(H, THIN, DAYS)
print(f"основа: {len(f_core)} бумаг; тонкие: {len(f_thin)} ({' '.join(sorted(f_thin))})")
t_core = ms.run(f_core, GATES, **EXITS)
runs = {}
for k in (1, 2, 3):
    ms.SLIP = 0.0005 * k
    runs[k] = ms.run(f_thin, GATES, **EXITS)
ms.SLIP = 0.0005
print("\n== Тонкие бумаги сами по себе ==")
for k, t in runs.items():
    yrs = {y: round(g.r.sum()) for y, g in t.groupby(t.t.dt.year)}
    print(f"проскальзывание x{k}: ", ms.fmt_stats(ms.stats(t)), f"| {len(t) / weeks:.1f} в неделю | {yrs}")
t2 = runs[2]
mid = t_core.t.min() + (t_core.t.max() - t_core.t.min()) / 2
print("   по бумагам при x2 (сделок, первая половина бумаги / вторая, итог):")
for sym, g in sorted(t2.groupby("sym"), key=lambda x: x[1].r.sum()):
    m = g.t.min() + (g.t.max() - g.t.min()) / 2
    print(f"   {sym:6s} с {g.t.min().date()}  {len(g):4d}  {g[g.t < m].r.sum():+6.1f} / {g[g.t >= m].r.sum():+6.1f}  итого {g.r.sum():+6.1f}R")


def portfolio(trades, max_open, max_sector, max_side):
    t = trades.sort_values("t").reset_index(drop=True)
    open_pos, keep = [], []
    for row in t.itertuples():
        open_pos = [p for p in open_pos if p[0] > row.t]
        base = ALIAS.get(row.sym, row.sym)
        sec = SEC_OF.get(base, "other_" + base)
        if len(open_pos) >= max_open or any(p[3] == base for p in open_pos):
            continue
        if sum(1 for p in open_pos if p[1] == sec) >= max_sector or sum(1 for p in open_pos if p[2] == row.sg) >= max_side:
            continue
        open_pos.append((row.t_exit, sec, row.sg, base))
        keep.append(row.Index)
    return t.loc[keep]


def show(label, trades, lim):
    p = portfolio(trades, *lim)
    s = ms.stats(p)
    yrs = {y: round(g.r.sum()) for y, g in p.groupby(p.t.dt.year)}
    print(f"{label:50s} {s['n']:5d} сд ({s['n'] / weeks:4.1f}/нед)  PF {s['pf']:.2f}  итог {s['sum_r']:+7.1f}R  половины {p[p.t < mid].r.sum():+.0f}/{p[p.t >= mid].r.sum():+.0f} | {yrs}")
    return p


print("\n== Портфель (позиций / на сектор / в одну сторону) ==")
for lim in ((5, 2, 4), (8, 3, 6)):
    show(f"основа {len(f_core)} бумаг, лимиты {lim}", t_core, lim)
    for k in (1, 2, 3):
        p = show(f"основа + тонкие (x{k}), лимиты {lim}", pd.concat([t_core, runs[k]], ignore_index=True), lim)
    thin_part = p[p.sym.isin(THIN)]
    print(f"      из них сделок по тонким бумагам при x3: {len(thin_part)}, их вклад {thin_part.r.sum():+.1f}R")
