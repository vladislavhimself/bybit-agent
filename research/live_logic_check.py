"""Текущая ценовая логика бота тем же честным методом, что и всё 07–08.11: сделки на 7 годах через research/moex_sim.py
с боевыми параметрами из config.yaml (markets.moex), по годам, до/после 2024, доля прибыльных, просадка, в R и в % капитала
(риск на сделку 0.8%), и сравнение со случайными входами с теми же выходами. Запуск из корня: python research/live_logic_check.py."""
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research import moex_sim as ms  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
HIST = str(ROOT / "data" / "history")
# вселенная ~ top-40 по обороту минус blacklist из config.yaml (HYDR, LSRG, PHOR, SNGSP, MTSS)
SYMS = ["SBER", "GAZP", "LKOH", "ROSN", "NVTK", "GMKN", "PLZL", "YDEX", "TATN", "SNGS", "MGNT", "VTBR", "T", "ALRS", "CHMF", "NLMK", "MAGN", "RUAL", "AFLT",
        "MOEX", "SMLT", "PIKK", "SVCB", "POSI", "OZON", "VKCO", "SIBN", "IRAO", "MTLR", "TRNFP", "FLOT", "FEES", "AFKS", "X5", "SBERP", "TATNP",
        "RTKM", "BSPB", "ENPG", "UPRO", "SGZH", "SELG", "UGLD", "HEAD", "ASTR", "CBOM", "BELU", "LENT", "MVID", "NMTP"]
# боевые ворота и выходы (config.yaml markets.moex.signals / risk)
GATES = {"thresh": 0.55, "ext_long": 3.0, "ext_short": 99.0, "range_long": False, "range_short": True, "high_vol": False, "counter_4h": False,
         "busy": 3, "max_per_day": 2, "index_gate": True, "long_mult": 0.85}
EXITS = dict(stop_mult=2.0, tps=((3.0, 0.33),), be_r=0.7, tiers=((1.5, 1.5), (3.0, 1.0), (5.0, 0.6)), stall_h=24, trail_after=1.0, max_h=96,
             pyramid=0.5, pyr_r=1.5, daily_exit=True, max_h_daily=720, peak_lock=(3.0, 0.5), pyr_be=True)
RISK_PCT = 0.8
SPLIT = pd.Timestamp("2024-01-01")


def report(name, t):
    if t is None or not len(t):
        print(f"  {name}: сделок нет")
        return
    r = t.r.to_numpy()
    cum = np.cumsum(r)
    dd = float((np.maximum.accumulate(cum) - cum).max())
    bef, aft = t[t.t < SPLIT], t[t.t >= SPLIT]
    w, l = r[r > 0], r[r <= 0]
    pf = w.sum() / -l.sum() if len(l) and l.sum() < 0 else 9
    weeks = max(1, (t.t.max() - t.t.min()).days / 7)
    print(f"  {name}: сделок {len(r)} (~{len(r) / weeks:.1f}/нед), приб. {np.mean(r > 0):.0%}, средний {r.mean():+.3f}R, PF {pf:.2f}, итог {cum[-1]:+.0f}R "
          f"= {cum[-1] * RISK_PCT:+.0f}% капитала при риске {RISK_PCT}% (без реинвестирования), просадка {dd:.0f}R = {dd * RISK_PCT:.0f}%")
    def half(x):
        return f"{x.r.sum():+.0f}R ({len(x)}, приб. {np.mean(x.r > 0) if len(x) else 0:.0%}, PF {(x.r[x.r > 0].sum() / -x.r[x.r <= 0].sum()) if len(x) and x.r[x.r <= 0].sum() < 0 else 9:.2f})"
    print(f"     до 2024: {half(bef)} | после 2024: {half(aft)}")
    by = t.groupby(t.t.dt.year).r
    print("     по годам: " + ", ".join(f"{y}: {v.sum():+.0f}R ({len(v)}, {np.mean(v > 0):.0%})" for y, v in by))
    lon, sho = t[t.sg > 0], t[t.sg < 0]
    print(f"     лонги: {half(lon)} | шорты: {half(sho)}")
    hours = ((t.t_exit - t.t).dt.total_seconds() / 3600)
    print(f"     среднее время в сделке {hours.mean():.0f} ч, медиана {hours.median():.0f} ч")


def random_baseline(feats, n_trades, rng, **exit_kw):
    syms = list(feats)
    res = []
    tries = 0
    while len(res) < n_trades and tries < n_trades * 20:
        tries += 1
        sym = syms[rng.integers(0, len(syms))]
        f = feats[sym]
        i = int(rng.integers(0, len(f) - 800))
        sg = 1 if rng.random() < 0.5 else -1
        out = ms.simulate(f, i, sg, **exit_kw)
        if out is not None:
            res.append(out[0])
    return np.array(res)


def main():
    print("загрузка признаков по 7 годам…", flush=True)
    feats = ms.load(HIST, SYMS, days=2700)
    print(f"бумаг с историей: {len(feats)}")
    kw = dict(ms.DEFAULT_EXITS, **EXITS)
    t = ms.run(feats, gates=GATES, **kw)
    print("\nТЕКУЩАЯ ЛОГИКА (боевые ворота и выходы)")
    report("как в бою", t)
    # варианты: без ворот индекса; без пирамидинга и дневного выхода (как было до 05.11)
    report("без ворот по индексу", ms.run(feats, gates=dict(GATES, index_gate=False), **kw))
    report("старые выходы (без дневного тренда и пирамидинга)", ms.run(feats, gates=GATES, **dict(kw, daily_exit=False, pyramid=0.0, peak_lock=None)))
    # случайные входы с теми же выходами
    rng = np.random.default_rng(3)
    base = random_baseline(feats, len(t), rng, **kw)
    print(f"\nСЛУЧАЙНЫЕ ВХОДЫ с теми же выходами, n={len(base)}: средний {base.mean():+.3f}R, приб. {np.mean(base > 0):.0%}, итог {base.sum():+.0f}R")
    sums = []
    for _ in range(200):
        s = random_baseline(feats, min(300, len(t)), rng, **kw).sum() * len(t) / min(300, len(t))
        sums.append(s)
    sums = np.array(sums)
    print(f"  200 случайных наборов того же размера: средняя сумма {sums.mean():+.0f}R, 95-й процентиль {np.percentile(sums, 95):+.0f}R; наша {t.r.sum():+.0f}R; "
          f"случайные ≥ нашей в {np.mean(sums >= t.r.sum()):.0%}")
    t.to_csv(ROOT / "data" / "live_logic_trades.csv", index=False)
    print("\nсохранено: data/live_logic_trades.csv")


if __name__ == "__main__":
    main()
