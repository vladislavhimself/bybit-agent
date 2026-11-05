"""Чувствительность к комиссии: в симуляторе 0.04% на сторону, по журналу песочницы фактически 0.052%.
Основа из настроек (без стоп-листа), лимиты портфеля из настроек. Запуск: DAYS=2500 python research/moex_fee_check.py data/history"""
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import moex_sim as ms  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
H = sys.argv[1]
DAYS = int(os.environ.get("DAYS", "2500"))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
cfg = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))["markets"]["moex"]
R = cfg["risk"]
SEC_OF = {t: s for s, lst in R["sectors"].items() for t in lst}
ALIAS = R.get("base_alias") or {}
BLACK = set(cfg["universe"].get("blacklist") or []) - set(os.environ.get("KEEP", "").split())
CORE = ("SBER GAZP LKOH ROSN NVTK GMKN TATN SNGS VTBR MOEX AFLT YDEX OZON PLZL CHMF MGNT ALRS T X5 MTSS "
        "VKCO SMLT SELG LSRG POSI AFKS SIBN SNGSP MVID MAGN TRNFP RUAL MTLR PHOR TATNP SGZH IRAO UPRO NLMK SVCB HYDR RTKM FLOT SBERP RNFT FEES PIKK BSPB "
        "NMTP SPBE FIXR DOMRF WUSH RAGR UWGN").split()
CORE = [s for s in CORE if s not in BLACK]
EXITS = dict(stall_h=24, daily_exit=True, pyramid=0.5, pyr_be=True, peak_lock=(3.0, 0.5), tps=((3.0, 0.33),))
LIM = (int(R["max_open_positions"]), int(R["max_per_sector"]), int(R["max_same_direction"]))
weeks = DAYS / 7.0
f = ms.load(H, CORE, DAYS)


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


print(f"бумаг {len(f)}, лимиты {LIM}")
for fee, slip, label in ((0.0004, 0.0005, "как в симуляторе: комиссия 0.04%, проскальзывание 0.05%"),
                         (0.00052, 0.0005, "комиссия как в журнале 0.052%"),
                         (0.00052, 0.001, "комиссия 0.052% и проскальзывание вдвое хуже")):
    ms.FEE, ms.SLIP = fee, slip
    p = portfolio(ms.run(f, {"ext_short": 3.0}, **EXITS), *LIM)
    s = ms.stats(p)
    yrs = {y: round(g.r.sum()) for y, g in p.groupby(p.t.dt.year)}
    print(f"{label:58s} {s['n']:5d} сд ({s['n'] / weeks:4.1f}/нед)  PF {s['pf']:.2f}  средняя {s['sum_r'] / max(s['n'], 1):+.3f}R  итог {s['sum_r']:+7.1f}R | {yrs}")
