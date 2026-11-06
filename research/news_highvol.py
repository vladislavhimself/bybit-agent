"""Стоит ли пропускать свежую бычью новость важности 7+ по бумаге через фильтр «резкая волатильность» (случай Циана 06.11)?
Архив новостей 2024–2026 с разметкой и реакциями цены + часовые свечи: режим бумаги по последней закрытой свече ДО новости.
Без вызовов модели. Запуск: python research/news_highvol.py data/history"""
import json
import os
import sqlite3
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import moex_sim as ms  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")
H = sys.argv[1]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
c = sqlite3.connect(f"file:{os.path.join(ROOT, 'data', 'news_archive.db')}?mode=ro", uri=True)
model = c.execute("select model from events group by 1 order by count(*) desc limit 1").fetchone()[0]
q = """SELECT e.ts, e.importance, e.direction, e.confidence, e.market_wide, e.affected, r.symbol, r.r_bar, r.r1, r.r4, r.r24, r.late1_r4, r.late1_r24
       FROM events e JOIN reactions r ON e.channel=r.channel AND e.id=r.id AND e.model=r.model WHERE e.relevant=1 AND e.model=?"""
d = pd.DataFrame(c.execute(q, (model,)).fetchall(), columns=["ts", "imp", "dir", "conf", "mw", "aff", "sym", "r_bar", "r1", "r4", "r24", "l4", "l24"])
d["n_aff"] = d.aff.map(lambda a: len(json.loads(a or "[]")))
d = d[(d.mw == 0) & (d.n_aff <= 4) & (d["dir"] == "bullish")].sort_values("ts").drop_duplicates(["ts", "sym"])
feats = ms.load(H, sorted(d.sym.unique()), 1100, index=None)
print(f"бычьих новостей по конкретным бумагам с реакцией: {len(d)}; бумаг с часовой историей: {len(feats)}")


def regime_before(sym, ts):
    f = feats.get(sym)
    if f is None:
        return None
    i = f.index.searchsorted(pd.Timestamp(ts - 3600, unit="s"), side="right") - 1     # последняя закрытая часовая свеча до новости
    return f.regime.iloc[i] if i >= 0 else None


d["regime"] = [regime_before(s, t) for s, t in zip(d.sym, d.ts)]
d = d[d.regime.notna()]
# «первая новость»: за предыдущие 2 часа по бумаге не было другой бычьей новости важности 6+ (иначе балл и так набрался бы)
d["first"] = True
last = {}
for i, row in d.iterrows():
    prev = last.get(row.sym)
    if prev is not None and row.ts - prev <= 7200:
        d.at[i, "first"] = False
    if row.imp >= 6:
        last[row.sym] = row.ts
d["score"] = d.imp / 10 * d.conf                      # новостной балл одной новости (порог фильтра 0.3)


def line(label, g):
    if len(g) < 12:
        print(f"   {label:66s} n={len(g):4d}  мало данных")
        return
    a4, a24, l24 = g.r4.dropna(), g.r24.dropna(), g.l24.dropna()
    print(f"   {label:66s} n={len(g):4d}  +1ч {g.r1.mean():+.2f}%  +4ч {a4.mean():+.2f}%  +24ч {a24.mean():+.2f}% (в плюс {(a24 > 0).mean() * 100:.0f}%)"
          f"  вход часом позже +24ч {l24.mean():+.2f}%")


imp7 = d[d.imp >= 7]
print("\n== Бычьи новости важности 7+ по бумаге: что было с ценой после (вход по цене закрытия часа новости) ==")
line("все режимы", imp7)
line("режим «резкая волатильность» до новости", imp7[imp7.regime == "high_vol"])
line("  из них первая новость (то, что фильтр сейчас отсекает)", imp7[(imp7.regime == "high_vol") & imp7["first"]])
line("  из них первая и балл одной новости ниже 0.3", imp7[(imp7.regime == "high_vol") & imp7["first"] & (imp7.score < 0.3)])
line("обычные режимы (тренд, флэт)", imp7[imp7.regime != "high_vol"])
hv = imp7[(imp7.regime == "high_vol") & imp7["first"]]
print("\n== Те же первые новости в резкой волатильности: по ходу самого часа новости ==")
line("час новости вырос на 2%+ (уже убежала)", hv[hv.r_bar >= 2])
line("час новости вырос меньше чем на 2%", hv[hv.r_bar < 2])
if len(hv):
    print(f"\n   таких случаев за период: {len(hv)} (~{len(hv) / max(1, (d.ts.max() - d.ts.min()) / 86400 / 30):.1f} в месяц);"
          f" худший за сутки {hv.r24.min():+.1f}%, лучший {hv.r24.max():+.1f}%; с комиссией и проскальзыванием ~0.2% средний итог {hv.r24.mean() - 0.2:+.2f}%")
