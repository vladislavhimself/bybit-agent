"""Проверка списка слов для отсева общих лент на реальных новостях из журнала бота (без вызовов модели):
сколько новостей каждого источника список отсеял бы и сколько из отсеянных модель на деле признала событием важности 5+
(это были бы потери). Запуск на сервере из каталога бота: python research/rulekeys_check.py [дней]"""
import importlib.util
import json
import os
import sqlite3
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("rulekeys", os.environ.get("RULEKEYS") or os.path.join(ROOT, "bot", "rulekeys.py"))
rk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rk)
days = float(sys.argv[1]) if len(sys.argv) > 1 else 3
c = sqlite3.connect(f"file:{os.path.join(ROOT, 'data', 'bot_moex.db')}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
rows = c.execute("""select n.source, n.title, n.summary, e.importance imp, e.direction dir, e.affected aff, e.market_wide mw
                    from news n left join events e on e.news_id=n.id where n.fetched_ts>=?""", (time.time() - days * 86400,)).fetchall()
st, lost = {}, []
for r in rows:
    text = (r["title"] or "") + " " + (r["summary"] or "")[:400]
    keep = bool(rk.RULE_KEYS.search(text))
    d = st.setdefault(r["source"], {"n": 0, "drop": 0, "ev5": 0, "lost5": 0, "lost7": 0})
    d["n"] += 1
    d["drop"] += not keep
    if r["imp"] is not None and r["imp"] >= 5:
        d["ev5"] += 1
        if not keep:
            d["lost5"] += 1
            d["lost7"] += r["imp"] >= 7
            lost.append((r["imp"], r["source"], r["dir"], (r["title"] or "")[:150].replace("\n", " ")))
print(f"слов в списке: {len(rk.WORDS)}; новостей за {days:g} дн.: {len(rows)}")
print(f"{'источник':26s} {'всего':>6s} {'отсеял бы':>10s} {'событий 5+':>11s} {'потерял бы 5+':>14s} {'из них 7+':>10s}")
tot = {"n": 0, "drop": 0, "ev5": 0, "lost5": 0, "lost7": 0}
for src, d in sorted(st.items(), key=lambda x: -x[1]["n"]):
    for k in tot:
        tot[k] += d[k]
    if d["n"] >= 15:
        print(f"{src:26s} {d['n']:6d} {d['drop']:6d} ({d['drop'] / d['n'] * 100:3.0f}%) {d['ev5']:11d} {d['lost5']:14d} {d['lost7']:10d}")
print(f"{'ИТОГО':26s} {tot['n']:6d} {tot['drop']:6d} ({tot['drop'] / tot['n'] * 100:3.0f}%) {tot['ev5']:11d} {tot['lost5']:14d} {tot['lost7']:10d}")
print("\nчто было бы потеряно (важность 5+), по убыванию важности:")
for imp, src, dr, title in sorted(lost, reverse=True)[:40]:
    print(f"   важн. {imp} {dr or '':8s} {src:18s} {title}")
