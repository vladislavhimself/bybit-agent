"""Настроение рынка как фильтр для технических сделок (случай 06.11: шорт банка за дни до мирных переговоров).
Технические сделки симулятора (правила бота, с воротами индекса) за период архива новостей; для каждой считаем фон:
1) поток общерыночных новостей за 24 часа до входа (разметка архива: направление и важность);
2) ход индекса за последние 1 и 3 торговых дня до входа.
Вопрос: хуже ли сделки ПРОТИВ фона, чем по фону? Без вызовов модели. Запуск: python research/market_mood.py data/history"""
import os
import sqlite3
import sys

import numpy as np
import pandas as pd
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
EXITS = dict(stop_mult=R["stop_atr_mult"], be_r=R["breakeven_r"], stall_h=R["stall_hours"], max_h=R["max_hours_in_trade"], daily_exit=bool(R["daily_trend_exit"]),
             pyramid=float(R.get("pyramid_part") or 0), pyr_be=True, peak_lock=(float(plk.get("min_peak_r", 3.0)), float(plk.get("keep", 0.5))),
             tps=tuple((float(x["r"]), float(x["part"])) for x in R["tp_levels"]))
GATES = {"thresh": 0.55, "ext_long": float(SIG["max_extension_atr"]), "ext_short": 99.0, "range_short": True, "busy": 3,
         "index_gate": True, "long_mult": float(SIG.get("ta_long_mult") or 1.0)}

c = sqlite3.connect(f"file:{os.path.join(ROOT, 'data', 'news_archive.db')}?mode=ro", uri=True)
model = c.execute("select model from events group by 1 order by count(*) desc limit 1").fetchone()[0]
ev = pd.DataFrame(c.execute("select ts, importance, direction from events where relevant=1 and market_wide=1 and model=? and direction!='neutral' and importance>=5",
                            (model,)).fetchall(), columns=["ts", "imp", "dir"]).sort_values("ts")
ev["s"] = np.where(ev["dir"] == "bullish", 1, -1) * ev.imp
t0, t1 = pd.Timestamp(ev.ts.min(), unit="s"), pd.Timestamp(ev.ts.max(), unit="s")
print(f"общерыночных новостей с направлением (важность 5+): {len(ev)}, период {t0.date()} — {t1.date()}")

feats = ms.load(H, CORE, int((pd.Timestamp.utcnow().tz_localize(None) - t0).days) + 60)
tr = ms.run(feats, GATES, **EXITS)
tr = tr[(tr.t >= t0 + pd.Timedelta(days=2)) & (tr.t <= t1)].reset_index(drop=True)
print(f"технических сделок симулятора за период архива: {len(tr)} (лонгов {(tr.sg > 0).sum()}, шортов {(tr.sg < 0).sum()}), в среднем {tr.r.mean():+.3f}R")

ets, es = ev.ts.values, ev.s.values
cum = np.concatenate([[0], np.cumsum(es)])
cnt_b = np.concatenate([[0], np.cumsum(es > 0)])


def mood(ts, hours):
    a, b = np.searchsorted(ets, ts - hours * 3600), np.searchsorted(ets, ts)
    n = b - a
    return (cum[b] - cum[a]), n, (cnt_b[b] - cnt_b[a]) / n if n else 0.5


tsec = (tr.t.values.astype("datetime64[s]").astype("int64"))
for hours in (24, 6):
    m = [mood(x, hours) for x in tsec]
    tr[f"net{hours}"], tr[f"n{hours}"], tr[f"share{hours}"] = [x[0] for x in m], [x[1] for x in m], [x[2] for x in m]

# ход индекса перед входом
idx = pd.read_csv(os.path.join(H, "IMOEXF_60.csv"))
its = idx.ts * 1000 if idx.ts.max() < 1e11 else idx.ts
ic = pd.Series(idx.close.values, index=pd.to_datetime(its, unit="ms")).sort_index()
ic = ic[~ic.index.duplicated()]


def idx_ret(t, hours):
    i1, i0 = ic.index.searchsorted(t, side="right") - 1, ic.index.searchsorted(t - pd.Timedelta(hours=hours), side="right") - 1
    return (ic.iloc[i1] / ic.iloc[i0] - 1) * 100 if i1 > 0 and i0 >= 0 else np.nan


tr["idx24"] = [idx_ret(t, 24) for t in tr.t]
tr["idx72"] = [idx_ret(t, 72) for t in tr.t]


def line(label, g):
    if len(g) < 25:
        print(f"   {label:62s} n={len(g):4d}  мало данных")
        return
    w, l = g.r[g.r > 0].sum(), -g.r[g.r <= 0].sum()
    print(f"   {label:62s} n={len(g):4d}  в среднем {g.r.mean():+.3f}R  PF {w / l if l else 9:.2f}  итог {g.r.sum():+.1f}R")


sh, lg = tr[tr.sg < 0], tr[tr.sg > 0]
print("\n== ШОРТЫ: поток общерыночных новостей за 24 ч до входа ==")
line("все шорты", sh)
line("новости явно бычьи (доля бычьих 65%+, не меньше 6 новостей)", sh[(sh.share24 >= 0.65) & (sh.n24 >= 6)])
line("новости смешанные", sh[(sh.share24 > 0.35) & (sh.share24 < 0.65) & (sh.n24 >= 6)])
line("новости явно медвежьи (доля бычьих 35% и меньше)", sh[(sh.share24 <= 0.35) & (sh.n24 >= 6)])
print("== ШОРТЫ: ход индекса перед входом ==")
line("индекс за сутки вырос на 0.7%+", sh[sh.idx24 >= 0.7])
line("индекс за сутки в пределах ±0.7%", sh[(sh.idx24 > -0.7) & (sh.idx24 < 0.7)])
line("индекс за сутки упал на 0.7%+", sh[sh.idx24 <= -0.7])
line("индекс за трое суток вырос на 1.5%+", sh[sh.idx72 >= 1.5])
line("индекс за трое суток упал на 1.5%+", sh[sh.idx72 <= -1.5])
print("== ШОРТЫ: оба признака против (бычьи новости И индекс за сутки +0.7%) ==")
both = (sh.share24 >= 0.65) & (sh.n24 >= 6) & (sh.idx24 >= 0.7)
line("против фона", sh[both])
line("остальные шорты", sh[~both])
print("\n== ЛОНГИ: то же зеркально ==")
line("все лонги", lg)
line("новости явно медвежьи", lg[(lg.share24 <= 0.35) & (lg.n24 >= 6)])
line("новости явно бычьи", lg[(lg.share24 >= 0.65) & (lg.n24 >= 6)])
line("индекс за сутки упал на 0.7%+", lg[lg.idx24 <= -0.7])
line("индекс за сутки вырос на 0.7%+", lg[lg.idx24 >= 0.7])
bothl = (lg.share24 <= 0.35) & (lg.n24 >= 6) & (lg.idx24 <= -0.7)
line("против фона (медвежьи новости И индекс −0.7%)", lg[bothl])
flt = pd.concat([sh[~both], lg[~bothl]])
print(f"\nитог: все сделки {tr.r.sum():+.1f}R ({len(tr)}), без сделок против фона {flt.r.sum():+.1f}R ({len(flt)})")

# ---------- устойчивость: пороги и годы ----------
print("\n== ШОРТЫ после роста индекса за сутки: пороги и годы ==")
for thr in (0.5, 0.7, 1.0, 1.5):
    line(f"индекс за сутки +{thr}% и больше", sh[sh.idx24 >= thr])
for y, g in sh.groupby(sh.t.dt.year):
    line(f"{y}: индекс за сутки +0.7%+", g[g.idx24 >= 0.7])
    line(f"{y}: остальные шорты", g[g.idx24 < 0.7])
print("== ШОРТЫ после падения индекса за трое суток: пороги и годы ==")
for thr in (1.0, 1.5, 2.5, 4.0):
    line(f"индекс за трое суток −{thr}% и глубже", sh[sh.idx72 <= -thr])
for y, g in sh.groupby(sh.t.dt.year):
    line(f"{y}: индекс за трое суток −1.5% и глубже", g[g.idx72 <= -1.5])
    line(f"{y}: остальные шорты", g[g.idx72 > -1.5])
keep = sh[(sh.idx24 < 0.7) & (sh.idx72 > -1.5)]
print(f"\nесли убрать шорты после роста индекса за сутки на 0.7%+ и после падения за трое суток на 1.5%+: осталось {len(keep)} из {len(sh)},"
      f" в среднем {keep.r.mean():+.3f}R, итог {keep.r.sum():+.1f}R (было {sh.r.sum():+.1f}R)")
