"""Комиссионная мясорубка: 1) правило «застряла» 12 ч против 24 и выкл; 2) шорт акций через ночь платит за заём
(~13% годовых от стоимости позиции), фьючерс нет — сравнить шорты с платой за заём, шорты только внутри дня и без шортов.
Запуск: DAYS=2500 python research/moex_stall_short.py data/history <файл со списком тикеров>"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import moex_sim as ms  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
H, UNI = sys.argv[1], open(sys.argv[2], encoding="utf-8").read().split()
DAYS = int(os.environ.get("DAYS", "2500"))
feats = ms.load(H, UNI, DAYS)
print(f"бумаг {len(feats)}, дней {DAYS}")
FUT = {"GAZPF", "SBERF", "IMOEXF"}
BORROW_DAY = 0.13 / 365   # плата за заём акций в шорте, доля стоимости позиции в день

print("== Правило «застряла» ==")
for st in (12, 24, 48, 0):
    t = ms.run(feats, stall_h=st)
    print(f"{'застряла ' + (str(st) + ' ч' if st else 'выкл'):22s}", ms.fmt_stats(ms.stats(t)))
print("   по годам, 12 ч против 24 ч:")
t12, t24 = ms.run(feats, stall_h=12), ms.run(feats, stall_h=24)
for y in sorted(set(t12.t.dt.year)):
    a, b = ms.stats(t12[t12.t.dt.year == y]), ms.stats(t24[t24.t.dt.year == y])
    print(f"      {y}: 12ч {a['sum_r']:+7.1f}R ({a['n']})   24ч {b['sum_r']:+7.1f}R ({b['n']})")

print("\n== Шорты акций: плата за заём через ночь ==")
base = ms.run(feats)
sh = base[base.sg < 0].copy()
share_sh = sh[~sh.sym.isin(FUT)].copy()
# стоимость позиции в R: цена / (2 ATR) ≈ 1 / (2·atr_pct); atr_pct берём по бумаге на момент входа
atrp = {}
for s, f in feats.items():
    atrp[s] = f.atr_pct
share_sh["atr_pct"] = [float(atrp[s].asof(t)) for s, t in zip(share_sh.sym, share_sh.t)]
share_sh["nights"] = ((share_sh.t_exit.dt.normalize() - share_sh.t.dt.normalize()).dt.days).clip(lower=0)
share_sh["borrow_r"] = share_sh.nights * BORROW_DAY * 100 / (2 * share_sh.atr_pct)
print(f"шортов акций {len(share_sh)}, из них через ночь {(share_sh.nights > 0).sum()}, средняя плата за заём {share_sh.borrow_r.mean():.3f}R на сделку, "
      f"всего {share_sh.borrow_r.sum():.1f}R из {share_sh.r.sum():+.1f}R")
print(f"{'шорты акций как есть (без платы)':40s}", ms.fmt_stats(ms.stats(share_sh)))
adj = share_sh.copy(); adj["r"] = adj.r - adj.borrow_r
print(f"{'шорты акций с платой за заём':40s}", ms.fmt_stats(ms.stats(adj)))
intra = share_sh[share_sh.nights == 0]
print(f"{'только внутридневные (nights=0)':40s}", ms.fmt_stats(ms.stats(intra)))
overn = adj[adj.nights > 0]
print(f"{'только ночные, с платой':40s}", ms.fmt_stats(ms.stats(overn)))
print("   ночные с платой по годам:")
for y, g in overn.groupby(overn.t.dt.year):
    print(f"      {y}", ms.fmt_stats(ms.stats(g)))
print("   вход после 12:00 UTC (15:00 МСК), ночные с платой:")
late = overn[overn.t.dt.hour >= 12]
print(f"      {ms.fmt_stats(ms.stats(late))}")
early = overn[overn.t.dt.hour < 12]
print(f"   вход до 12:00 UTC, ночные с платой: {ms.fmt_stats(ms.stats(early))}")
