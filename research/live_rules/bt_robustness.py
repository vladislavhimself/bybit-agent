"""Итоговая комбинация и проверка на устойчивость: половины истории, половины списка монет, один вход на сторону в час."""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bt_entries as bt2  # noqa: E402
import bt_params as bt3  # noqa: E402  (загружает данные при импорте: feats)

sys.stdout.reconfigure(encoding="utf-8")
feats = bt3.feats


def run_c(fs, ext24_max=2.0, ext4_max=None, pull=0.5, stop_mult=2.5, wait=2, one_per_hour=False):
    res = []
    for sym, f in fs.items():
        sc, rg, t4 = f.score.values, f.regime.values, f.trend_4h.values
        r24, r4, ap = f.ret24.values, f.ret4.values, f.atr_pct.values
        busy = -1
        for i in range(len(f) - 6):
            if i <= busy or abs(0.6 * sc[i]) < 0.55:
                continue
            sg = 1 if sc[i] > 0 else -1
            if rg[i] != "trend" or (t4[i] != 0 and (t4[i] > 0) != (sg > 0)) or sg * r24[i] > 15:
                continue
            if sg * r24[i] / ap[i] >= ext24_max:
                continue
            if ext4_max is not None and sg * r4[i] / ap[i] >= ext4_max:
                continue
            out = bt3.simulate_p(f, i, sg, pull=pull, stop_mult=stop_mult, wait=wait)
            if out is None:
                continue
            res.append((f.index[i], sym, sg, out[0], abs(sc[i])))
            busy = out[1] + 12
    t = pd.DataFrame(res, columns=["t", "sym", "sg", "r", "score"]).sort_values("t").reset_index(drop=True)
    if one_per_hour and not t.empty:
        t["hour"] = t.t.dt.floor("h")
        t = t.sort_values("score", ascending=False).groupby(["hour", "sg"]).head(1).sort_values("t").reset_index(drop=True)
    return t


print("== Глубина отката 0.5-0.8 и ожидание (стоп 2.5, растяжение <2) ==")
for pull in (0.4, 0.5, 0.6, 0.8):
    for wait in (1, 2):
        bt2.report(run_c(feats, pull=pull, wait=wait), f"откат {pull} ATR, ждать {wait} ч")
print("\n== Порог растяжения (откат 0.5, стоп 2.5) ==")
for e in (1.5, 2.0, 2.5, 3.0, 99):
    bt2.report(run_c(feats, ext24_max=e), f"растяжение за 24ч < {e} ATR")
bt2.report(run_c(feats, ext4_max=2.0), "растяжение <2 ATR за 24ч и <2 ATR за 4ч")
bt2.report(run_c(feats, one_per_hour=True), "+ не больше одного входа на сторону в час")
print("\n== Устойчивость итоговой схемы (откат 0.5, стоп 2.5, растяжение <2, один вход на сторону в час) ==")
syms = sorted(feats)
h1 = {s: feats[s] for s in syms[::2]}
h2 = {s: feats[s] for s in syms[1::2]}
bt2.report(run_c(h1, one_per_hour=True), "монеты набор 1 (каждая вторая)")
bt2.report(run_c(h2, one_per_hour=True), "монеты набор 2 (остальные)")
final = run_c(feats, one_per_hour=True)
bt2.report(final, "все монеты")
old = run_c(feats, ext24_max=99, pull=0.25, stop_mult=2.0)
bt2.report(old, "для сравнения: как работает бот сейчас")
for name, t in (("итоговая", final), ("сейчас", old)):
    m = t.groupby(t.t.dt.to_period("M")).r.sum()
    print(f"  {name}: месяцев в плюсе {int((m > 0).sum())} из {len(m)}, худший месяц {m.min():+.1f}R, лучший {m.max():+.1f}R, "
          f"сделок в месяц в среднем {len(t) / len(m):.0f}")
