"""Ещё семь ликвидных бумаг, которые бот может подхватить по обороту: те же правила, что в moex_universe.py.
Запуск: DAYS=2500 python research/moex_universe_extra.py data/history"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import moex_sim as ms  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
H = sys.argv[1]
DAYS = int(os.environ.get("DAYS", "2500"))
EXTRA = "NMTP SPBE FIXR DOMRF WUSH RAGR UWGN".split()
EXITS = dict(stall_h=24, daily_exit=True, pyramid=0.5, pyr_be=True, peak_lock=(3.0, 0.5), tps=((3.0, 0.33),))
f = ms.load(H, EXTRA, DAYS)
t = ms.run(f, {"ext_short": 3.0}, **EXITS)
print("всего:", ms.fmt_stats(ms.stats(t)))
for sym, g in t.groupby("sym"):
    mid = g.t.min() + (g.t.max() - g.t.min()) / 2
    a, b = g[g.t < mid].r.sum(), g[g.t >= mid].r.sum()
    print(f"   {sym:6s} с {g.t.min().date()}  сделок {len(g):4d}  первая половина {a:+6.1f}R  вторая {b:+6.1f}R  итого {a + b:+6.1f}R  PF {ms.stats(g)['pf']:.2f}")
