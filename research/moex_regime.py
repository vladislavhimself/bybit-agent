"""Фильтр режима рынка: помогает ли не торговать (или торговать половиной) в периоды, когда рынок в целом без тренда.
Рыночный индикатор — равновзвешенная корзина всех бумаг истории (7 лет; фьючерс на индекс есть только с конца 2023).
Признаки режима считаются по дневным данным корзины на момент входа каждой сделки: наклон SMA20, ADX(14),
реализованная волатильность 20 дн., ширина рынка (доля бумаг с дневным трендом в сторону сделки).
Запуск: DAYS=2500 python research/moex_regime.py data/history <файл со списком тикеров>"""
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

# ---- рыночная корзина по дневным закрытиям ----
closes = pd.DataFrame({s: f.close.resample("1D").last() for s, f in feats.items()}).dropna(how="all")
norm = closes / closes.bfill().iloc[0]
basket = norm.mean(axis=1).dropna()
sma20 = basket.rolling(20).mean()
sma50 = basket.rolling(50).mean()
slope20 = (sma20 / sma20.shift(10) - 1) * 100                # наклон SMA20 за 10 дней, %
rv20 = basket.pct_change().rolling(20).std() * np.sqrt(252) * 100
hi, lo = basket.rolling(14).max(), basket.rolling(14).min()
rng14 = (hi / lo - 1) * 100                                     # размах 14 дней, %
trend_d = pd.DataFrame({s: f.trend_d.resample("1D").last() for s, f in feats.items()})
breadth_up = (trend_d > 0).mean(axis=1)
breadth_dn = (trend_d < 0).mean(axis=1)
above = (basket > sma20).astype(int)
# ADX корзины
def adx(c, n=14):
    h, l = c.rolling(2).max(), c.rolling(2).min()
    up, dn = h.diff(), -l.diff()
    pdm = np.where((up > dn) & (up > 0), up, 0.0); mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = (h - l).abs().ewm(alpha=1 / n, adjust=False).mean().replace(0, np.nan)
    pdi = 100 * pd.Series(pdm, index=c.index).ewm(alpha=1 / n, adjust=False).mean() / tr
    mdi = 100 * pd.Series(mdm, index=c.index).ewm(alpha=1 / n, adjust=False).mean() / tr
    return (100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)).ewm(alpha=1 / n, adjust=False).mean()
adx14 = adx(basket)
mkt = pd.DataFrame({"slope20": slope20, "rv20": rv20, "rng14": rng14, "adx": adx14, "b_up": breadth_up, "b_dn": breadth_dn,
                    "above20": above, "above50": (basket > sma50).astype(int)}).shift(1)   # знаем на утро дня входа

base = ms.run(feats)
t = base.copy()
t["day"] = t.t.dt.normalize()
t = t.join(mkt, on="day")
t = t.dropna(subset=["slope20", "adx"])
print("база:", ms.fmt_stats(ms.stats(t)))


def rep(label, m):
    g = t[m]
    s = ms.stats(g)
    print(f"{label:52s} {ms.fmt_stats(s)}")
    return g


print("\n== По режиму корзины на момент входа ==")
for lab, m in (("ADX корзины < 15 (без тренда)", t.adx < 15), ("ADX 15–25", (t.adx >= 15) & (t.adx < 25)), ("ADX ≥ 25 (тренд)", t.adx >= 25)):
    rep(lab, m)
for lab, m in (("|наклон SMA20| < 0.5% за 10 дн.", t.slope20.abs() < 0.5), ("наклон 0.5–1.5%", (t.slope20.abs() >= 0.5) & (t.slope20.abs() < 1.5)), ("|наклон| ≥ 1.5%", t.slope20.abs() >= 1.5)):
    rep(lab, m)
for lab, m in (("размах 14 дн. < 3%", t.rng14 < 3), ("размах 3–6%", (t.rng14 >= 3) & (t.rng14 < 6)), ("размах ≥ 6%", t.rng14 >= 6)):
    rep(lab, m)
for lab, m in (("волатильность 20 дн. < 15% годовых", t.rv20 < 15), ("15–25%", (t.rv20 >= 15) & (t.rv20 < 25)), ("≥ 25%", t.rv20 >= 25)):
    rep(lab, m)

print("\n== Согласие сделки с рынком ==")
with_mkt = ((t.sg > 0) & (t.slope20 > 0)) | ((t.sg < 0) & (t.slope20 < 0))
rep("направление сделки = наклон SMA20 корзины", with_mkt)
rep("направление против наклона корзины", ~with_mkt)
with_b = ((t.sg > 0) & (t.b_up >= 0.5)) | ((t.sg < 0) & (t.b_dn >= 0.5))
rep("ширина: ≥50% бумаг в дневном тренде в сторону сделки", with_b)
rep("ширина против", ~with_b)
# текущие ворота бота: лонг только если корзина выше SMA20, шорт не против растущей корзины
gate = ((t.sg > 0) & (t.above20 == 1)) | ((t.sg < 0) & (t.above20 == 0))
rep("как ворота бота: лонг над SMA20 / шорт под SMA20", gate)
rep("что ворота бота отсеивают", ~gate)
print("   по годам (отсеянное воротами):")
for y, g in t[~gate].groupby(t[~gate].t.dt.year):
    print(f"      {y}", ms.fmt_stats(ms.stats(g)))

print("\n== Варианты фильтра (итог в R и в деньгах при 0.8% риска) ==")
def money(g):
    return (g.r * 0.8).sum()
variants = {
    "без фильтра": t.index == t.index,
    "только при ADX корзины ≥ 15": t.adx >= 15,
    "только при ADX корзины ≥ 20": t.adx >= 20,
    "только по направлению наклона SMA20": with_mkt,
    "только при ширине ≥ 50% в сторону сделки": with_b,
    "ворота бота (лонг над SMA20 / шорт под)": gate,
    "ADX ≥ 15 и по направлению наклона": (t.adx >= 15) & with_mkt,
}
for lab, m in variants.items():
    g = t[m]; s = ms.stats(g)
    print(f"{lab:48s} {ms.fmt_stats(s)}  деньги {money(g):+.0f}% (при 0.8% риска, без реинвеста)")
print("\n== Половинный размер вместо запрета ==")
for lab, m in (("половина при ADX < 15", t.adx < 15), ("половина против наклона SMA20", ~with_mkt), ("половина при ширине против", ~with_b)):
    w = np.where(m, 0.5, 1.0)
    print(f"{lab:48s} деньги {(t.r * 0.8 * w).sum():+.0f}%  (полный размер везде: {money(t):+.0f}%)")
print("\n== Ворота бота + добавки ==")
for lab, m in (("ворота + ширина ≥50%", gate & with_b), ("ворота + ADX ≥ 20", gate & (t.adx >= 20)), ("ворота + наклон в сторону", gate & with_mkt)):
    g = t[m]; print(f"{lab:48s} {ms.fmt_stats(ms.stats(g))}  деньги {money(g):+.0f}%")
print("   ворота бота по годам:")
for y, g in t[gate].groupby(t[gate].t.dt.year):
    print(f"      {y}", ms.fmt_stats(ms.stats(g)))
print("\n== Ширина как размер (полный при согласии, половина против), поверх ворот бота ==")
g = t[gate]
wb = ((g.sg > 0) & (g.b_up >= 0.5)) | ((g.sg < 0) & (g.b_dn >= 0.5))
print(f"{'ворота, полный размер везде':48s} деньги {(g.r * 0.8).sum():+.0f}%  сделок {len(g)}")
print(f"{'ворота, половина при ширине против':48s} деньги {(g.r * 0.8 * np.where(wb, 1.0, 0.5)).sum():+.0f}%")
print(f"{'ворота, 1.5x при ширине за, 0.5x против':48s} деньги {(g.r * 0.8 * np.where(wb, 1.5, 0.5)).sum():+.0f}%")
print(f"{'ворота, только при ширине за (запрет)':48s} деньги {(g.r * 0.8 * np.where(wb, 1.0, 0.0)).sum():+.0f}%  сделок {int(wb.sum())}")
print("   по годам (половина против / полный):")
for y, gg in g.groupby(g.t.dt.year):
    w = ((gg.sg > 0) & (gg.b_up >= 0.5)) | ((gg.sg < 0) & (gg.b_dn >= 0.5))
    print(f"      {y}: половина {(gg.r * 0.8 * np.where(w, 1.0, 0.5)).sum():+6.0f}%  полный {(gg.r * 0.8).sum():+6.0f}%")
