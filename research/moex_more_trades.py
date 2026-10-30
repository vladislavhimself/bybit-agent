"""Как получить больше сделок на Мосбирже, не теряя матожидания: перебор порога силы, фильтра растяжения,
режимов (флэт/высокая волатильность), контртренда 4ч и паузы между входами — на 7 годах часовой истории.
Запуск: DAYS=2500 python research/moex_more_trades.py data/history <файл со списком тикеров>"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "moex_trail.py"), encoding="utf-8").read()
src = src[:src.index("BASE = dict(")]
# параметризуем ворота в run()
I12, I16 = " " * 12, " " * 16
old = (I12 + "if rg[i] != \"trend\" or (t4[i] != 0 and (t4[i] > 0) != (sg > 0)) or sg * r24[i] > 15:\n" + I16 + "continue\n"
       + I12 + "if sg * r24[i] / ap[i] >= 2:\n" + I16 + "continue\n")
new = (I12 + "if rg[i] == \"range\" and not (G[\"range_long\"] if sg > 0 else G[\"range_short\"]):\n" + I16 + "continue\n"
       + I12 + "if rg[i] == \"high_vol\" and not G[\"high_vol\"]:\n" + I16 + "continue\n"
       + I12 + "if t4[i] != 0 and (t4[i] > 0) != (sg > 0) and not G[\"counter_4h\"]:\n" + I16 + "continue\n"
       + I12 + "if sg * r24[i] > 15:\n" + I16 + "continue\n"
       + I12 + "if sg * r24[i] / ap[i] >= G[\"ext\"]:\n" + I16 + "continue\n")
assert old in src, "ворота в run() не найдены"
src = src.replace(old, new)
src = src.replace("            if i <= busy or abs(0.6 * sc[i]) < THRESH:", "            if i <= busy or abs(0.6 * sc[i]) < G[\"thresh\"]:")
src = src.replace("            busy = out[1] + 12", "            busy = out[1] + G[\"busy\"]")
assert "G[\"thresh\"]" in src and "G[\"busy\"]" in src
G = {"thresh": 0.55, "ext": 2.0, "range_long": False, "range_short": True, "high_vol": False, "counter_4h": False, "busy": 12}
g = {"G": G, "__name__": "mx", "__file__": os.path.join(HERE, "moex_trail.py")}
exec(compile(src, "moex_trail_param", "exec"), g)
run, report, feats = g["run"], g["report"], g["feats"]
BASE = dict(stop_mult=2.0, tps=((1.5, 0.25), (2.5, 0.35)), tiers=[(1.5, 1.5), (3.0, 1.0), (5.0, 0.6)], max_h=96, be_r=0.7)


def go(label, **kw):
    G.update({"thresh": 0.55, "ext": 2.0, "range_long": False, "range_short": True, "high_vol": False, "counter_4h": False, "busy": 12})
    G.update(kw)
    t = run(feats, **BASE)
    report(t, label)
    return t


print("== База: как в боте сейчас (порог 0.55, растяжение 2 ATR, флэт только шорты, пауза 12 ч) ==")
base = go("база")
print("\n== Порог силы ==")
for th in (0.50, 0.45, 0.40):
    go(f"порог {th}", thresh=th)
print("\n== Фильтр растяжения ==")
for ext in (2.5, 3.0, 99):
    go(f"растяжение {ext if ext < 99 else 'выкл'} ATR", ext=ext)
print("\n== Режимы ==")
go("высокая волатильность разрешена", high_vol=True)
go("контртренд 4ч разрешён", counter_4h=True)
go("флэт: и лонги тоже", range_long=True)
print("\n== Пауза после сделки по бумаге ==")
for b in (6, 3, 0):
    go(f"пауза {b} ч", busy=b)
print("\n== Комбинации ==")
c1 = go("порог 0.50 + растяжение 2.5 + пауза 6", thresh=0.50, ext=2.5, busy=6)
c2 = go("порог 0.45 + растяжение 3.0 + пауза 6", thresh=0.45, ext=3.0, busy=6)
c3 = go("порог 0.50 + высокая волатильность + пауза 6", thresh=0.50, high_vol=True, busy=6)
print("\n== По годам: база vs комбинация 1 ==")
for y, gg in base.groupby(base.t.dt.year):
    report(gg, f"   {y} база")
for y, gg in c1.groupby(c1.t.dt.year):
    report(gg, f"   {y} комб.1")
print("\n== Кандидаты по годам ==")
for label, kw in (("растяж.3.0 + пауза 3", dict(ext=3.0, busy=3)), ("растяж.выкл + пауза 3", dict(ext=99, busy=3))):
    t = go(label, **kw)
    for y, gg in t.groupby(t.t.dt.year):
        report(gg, f"   {y} {label}")
