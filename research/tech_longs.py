"""Технические лонги: проходят ли они вообще ворота бота и что дают на истории. Без вызовов модели.
Запуск: python research/tech_longs.py data/history"""
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
BLACK = set(cfg["universe"].get("blacklist") or [])
CORE = [s for s in ("SBER GAZP LKOH ROSN NVTK GMKN TATN SNGS VTBR MOEX AFLT YDEX OZON PLZL CHMF MGNT ALRS T X5 "
                    "VKCO SMLT SELG POSI AFKS SIBN MVID MAGN TRNFP RUAL MTLR TATNP SGZH IRAO UPRO NLMK SVCB RTKM FLOT SBERP RNFT FEES PIKK BSPB "
                    "NMTP SPBE WUSH").split() if s not in BLACK]
plk = R.get("daily_peak_lock") or {}
EXITS = dict(stop_mult=R["stop_atr_mult"], be_r=R["breakeven_r"], stall_h=R["stall_hours"], max_h=R["max_hours_in_trade"], daily_exit=True,
             pyramid=float(R.get("pyramid_part") or 0), pyr_be=True, peak_lock=(float(plk.get("min_peak_r", 3.0)), float(plk.get("keep", 0.5))),
             tps=tuple((float(x["r"]), float(x["part"])) for x in R["tp_levels"]))
f = ms.load(H, CORE, 1080)


def show(label, gates):
    t = ms.run(f, dict({"thresh": 0.55, "ext_long": float(SIG["max_extension_atr"]), "ext_short": 99.0, "range_short": True, "busy": 3}, **gates), **EXITS)
    for name, g in (("лонги", t[t.sg > 0]), ("шорты", t[t.sg < 0])):
        if not len(g):
            print(f"   {label:52s} {name}: 0 сделок")
            continue
        w, l = g.r[g.r > 0].sum(), -g.r[g.r <= 0].sum()
        yrs = {y: f"{x.r.sum():+.0f}R/{len(x)}" for y, x in g.groupby(g.t.dt.year)}
        print(f"   {label:52s} {name}: n={len(g):4d}  в среднем {g.r.mean():+.3f}R  PF {w / l if l else 9:.2f}  итог {g.r.sum():+.1f}R | {yrs}")


print("три года, часовые свечи, правила выхода бота:")
show("как в боте сейчас (ворота индекса, лонг x0.85)", {"index_gate": True, "long_mult": float(SIG.get("ta_long_mult") or 1.0)})
show("ворота индекса, лонг без понижения", {"index_gate": True, "long_mult": 1.0})
show("без ворот индекса, лонг без понижения", {"index_gate": False, "long_mult": 1.0})
