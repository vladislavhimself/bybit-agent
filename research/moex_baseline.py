"""Опорный прогон после аудита 05.11.2026: симулятор с комиссией 0.052%, временем в часах и (где есть история индекса)
воротами бота по тренду рынка и множителем технического лонга. Бумаги, стоп-лист, секторы и лимиты портфеля — из config.yaml.
Запуск: python research/moex_baseline.py data/history"""
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import moex_sim as ms  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
H = sys.argv[1]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
cfg = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))["markets"]["moex"]
R, SIG = cfg["risk"], cfg["signals"]
SEC_OF = {t: s for s, lst in R["sectors"].items() for t in lst}
ALIAS = R.get("base_alias") or {}
BLACK = set(cfg["universe"].get("blacklist") or [])
CORE = ("SBER GAZP LKOH ROSN NVTK GMKN TATN SNGS VTBR MOEX AFLT YDEX OZON PLZL CHMF MGNT ALRS T X5 MTSS "
        "VKCO SMLT SELG LSRG POSI AFKS SIBN SNGSP MVID MAGN TRNFP RUAL MTLR PHOR TATNP SGZH IRAO UPRO NLMK SVCB HYDR RTKM FLOT SBERP RNFT FEES PIKK BSPB "
        "NMTP SPBE FIXR DOMRF WUSH RAGR UWGN").split()
CORE = [s for s in CORE if s not in BLACK]
plk = R.get("daily_peak_lock") or {}
EXITS = dict(stop_mult=R["stop_atr_mult"], be_r=R["breakeven_r"], stall_h=R["stall_hours"], max_h=R["max_hours_in_trade"],
             daily_exit=bool(R["daily_trend_exit"]), pyramid=float(R.get("pyramid_part") or 0), pyr_be=bool(R.get("pyramid_stop_breakeven", True)),
             peak_lock=(float(plk.get("min_peak_r", 3.0)), float(plk.get("keep", 0.5))) if plk else None,
             tps=tuple((float(x["r"]), float(x["part"])) for x in R["tp_levels"]))
BASE_GATES = {"thresh": float(SIG["entry_threshold"]), "ext_long": float(SIG["max_extension_atr"]),
              "ext_short": float(SIG.get("max_extension_atr_short") or SIG["max_extension_atr"]), "range_short": bool(SIG.get("range_shorts")),
              "busy": int(R["symbol_cooldown_hours"])}
BOT_GATES = dict(BASE_GATES, index_gate=bool(SIG.get("trend_gate")), long_mult=float(SIG.get("ta_long_mult") or 1.0))
LIM = (int(R["max_open_positions"]), int(R["max_per_sector"]), int(R["max_same_direction"]))


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


def show(label, days, gates):
    f = ms.load(H, CORE, days)
    p = portfolio(ms.run(f, gates, **EXITS), *LIM)
    s = ms.stats(p)
    yrs = {y: round(g.r.sum()) for y, g in p.groupby(p.t.dt.year)}
    q = p[p.t >= p.t.max() - __import__("pandas").Timedelta(days=90)]
    print(f"{label:58s} {s['n']:5d} сд ({s['n'] / (days / 7):4.1f}/нед)  PF {s['pf']:.2f}  средняя {s['avg_r']:+.3f}R  итог {s['sum_r']:+7.1f}R"
          f" | последние 90 дней {q.r.sum():+.1f}R ({len(q)} сд) | {yrs}")


print(f"бумаг {len(CORE)}, лимиты {LIM}, комиссия {ms.FEE * 100:.3f}%, проскальзывание {ms.SLIP * 100:.2f}%")
show("7 лет, без ворот индекса (истории индекса нет)", 2500, BASE_GATES)
show("820 дней, без ворот индекса", 820, BASE_GATES)
show("820 дней, с воротами бота (индекс, множитель лонга)", 820, BOT_GATES)
