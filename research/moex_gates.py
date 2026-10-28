"""Проверка на 7 годах Мосбиржи двух вопросов из живых сделок (28.10.2026):
1) безубыток на 0.7R выбивается шумом (три сделки дошли до +0.8R и закрылись в ноль) — сравнить 0.7 / 1.0 / 1.5 / без;
2) ворота «флэт» отсеяли 29 теней с +0.42R — проверить входы во флэте на истории.
Запуск: DAYS=2500 python research/moex_gates.py data/history <файл со списком тикеров>"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("mx", os.path.join(os.path.dirname(os.path.abspath(__file__)), "moex_trail.py"))
src = open(spec.origin, encoding="utf-8").read()
src = src[:src.index("BASE = dict(")]            # берём функции и загрузку данных, без сценария
src = src.replace("        if rg[i] != \"trend\" or (t4[i] != 0 and (t4[i] > 0) != (sg > 0)) or sg * r24[i] > 15:",
                  "        if (rg[i] != \"trend\" and not ALLOW_RANGE) or (rg[i] == \"high_vol\") or (t4[i] != 0 and (t4[i] > 0) != (sg > 0)) or sg * r24[i] > 15:")
assert "ALLOW_RANGE" in src
ALLOW_RANGE = False
g = {"ALLOW_RANGE": False, "__name__": "mx", "__file__": spec.origin}
exec(compile(src, spec.origin, "exec"), g)
run, report, feats = g["run"], g["report"], g["feats"]

BASE = dict(stop_mult=2.0, tps=((1.5, 0.25), (2.5, 0.35)), tiers=[(1.5, 1.5), (3.0, 1.0), (5.0, 0.6)], max_h=96)
print("== Безубыток (стоп 2.0, тейки 1.5/2.5, ступени защиты, 96ч) ==")
for be in (0.7, 1.0, 1.5, 0):
    report(run(feats, **BASE, be_r=be), f"безубыток после {be}R" if be else "без безубытка")
print("\n== Входы во флэте (regime=range) против только тренда ==")
g["ALLOW_RANGE"] = False
t_only = run(feats, **BASE, be_r=0.7)
report(t_only, "только тренд (как в боте)")
g["ALLOW_RANGE"] = True
both = run(feats, **BASE, be_r=0.7)
report(both, "тренд + флэт")
key = set(zip(t_only.t, t_only.sym))
rng = both[[(a, b) not in key for a, b in zip(both.t, both.sym)]]
report(rng, "   только флэтовые входы (добавка)")
for sg, gg in rng.groupby("sg"):
    report(gg, f"      {'лонг' if sg > 0 else 'шорт'} во флэте")
for y, gg in rng.groupby(rng.t.dt.year):
    report(gg, f"      {y} флэт")
print("\n== Флэтовые шорты по годам ==")
for y, gg in rng[rng.sg < 0].groupby(rng[rng.sg < 0].t.dt.year):
    report(gg, f"      {y} шорт во флэте")
print("== Флэтовые лонги по годам ==")
for y, gg in rng[rng.sg > 0].groupby(rng[rng.sg > 0].t.dt.year):
    report(gg, f"      {y} лонг во флэте")
