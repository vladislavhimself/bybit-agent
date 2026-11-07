"""Калибровка важности новостей по факту: что реально двигало индекс и бумаги.

Архив data/news_archive.db: 34 830 новостей 2024-10 — 2026-10, размеченных дешёвой моделью, и реакции цен (reactions: r1, r4, r24 — ход
в % за 1, 4 и 24 часовых бара после новости по индексу IMOEXF и затронутым бумагам). Считаем по темам (ключевые слова), по типам
событий и по важности модели: сколько новостей, средний ход, средний модуль хода, доля ходов больше 0.5% и 1%, точность стороны.
Результат — таблица для владельца и короткий текст для контекста модели (data/news_calibration.json).
Запуск из корня: python research/news_calibration.py. Модель не вызывается.
"""
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "news_archive.db"

TOPICS = [
    ("мирные переговоры / перемирие", r"мирн\w+ (переговор|план|соглашен|урегулирован)|перемири|прекращени\w+ огня|переговор\w*[^.]{0,80}(Украин|Росси|США|Трамп|Путин|Зеленск|Уиткофф)"),
    ("переговоры (любые)", r"переговор"),
    ("санкции: пакет / введение / снятие", r"пакет\w* санкц|санкци\w*[^.]{0,40}(введ|вступ|принят|утверд|снят|ослаб|отмен)"),
    ("санкции (слово)", r"санкци"),
    ("ключевая ставка ЦБ", r"ключев\w+ ставк|ставк\w+ ЦБ|Банк\w* России[^.]{0,60}ставк"),
    ("инфляция", r"инфляци"),
    ("нефть / Brent", r"\bBrent\b|нефт"),
    ("ОПЕК", r"ОПЕК|OPEC"),
    ("рубль / курс", r"курс\w* (рубл|доллар|юан)|рубл\w* (укреп|ослаб|упал|вырос)"),
    ("Трамп", r"Трамп"),
    ("Путин", r"Путин"),
    ("удары / атаки", r"удар\w*|атак\w*|дрон|БПЛА|ракет"),
    ("мобилизация", r"мобилизац"),
    ("ФРС", r"ФРС|FOMC"),
    ("дивиденды", r"дивиденд"),
    ("отчётность", r"отчет|отчёт|выручк|чист\w+ прибыл|EBITDA|МСФО|РСБУ"),
    ("бюджет / Минфин", r"бюджет|Минфин"),
    ("ОФЗ / долг", r"ОФЗ|гособлигац"),
    ("IPO / размещение", r"IPO|SPO|размещени"),
    ("выкуп / оферта / сделка M&A", r"выкуп|оферт|поглощен|приобрет\w+ (долю|акци|компани)"),
    ("суд / арест / национализация", r"\bсуд\b|арест|национализ|изъят"),
    ("Китай", r"Кита|КНР"),
    ("Иран / Израиль / Ближний Восток", r"Иран|Израил|Ормуз|Ближн\w+ Восток"),
    ("газ / Газпром / СПГ", r"\bгаз\b|газа\b|Газпром|СПГ"),
    ("золото", r"золот"),
]


def stats(rows):
    n = len(rows)
    if not n:
        return None
    r4 = [r["r4"] for r in rows]
    hits = [r for r in rows if r["direction"] in ("bullish", "bearish")]
    hit = sum(1 for r in hits if (r["r4"] > 0) == (r["direction"] == "bullish")) / len(hits) if hits else None
    return {"n": n, "mean_r4": round(sum(r4) / n, 3), "mean_abs_r4": round(sum(abs(x) for x in r4) / n, 3),
            "share_gt_05": round(sum(1 for x in r4 if abs(x) > 0.5) / n, 2), "share_gt_1": round(sum(1 for x in r4 if abs(x) > 1.0) / n, 2),
            "dir_hit": round(hit, 2) if hit is not None else None, "n_dir": len(hits),
            "mean_abs_r24": round(sum(abs(r["r24"] or 0) for r in rows) / n, 3)}


def main():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    rows = c.execute("""select e.ts, e.importance, e.direction, e.market_wide, e.event_type, e.title, r.symbol, r.r1, r.r4, r.r24
                        from events e join reactions r on r.channel=e.channel and r.id=e.id where e.relevant=1 and r.r4 is not null""").fetchall()
    rows = [dict(r) for r in rows]
    idx = [r for r in rows if r["symbol"] == "IMOEXF"]
    stocks = [r for r in rows if r["symbol"] != "IMOEXF"]
    print(f"пар событие–реакция: {len(rows)}, по индексу {len(idx)}, по бумагам {len(stocks)}")
    base_idx, base_st = stats(idx), stats(stocks)
    print(f"\nФОН (все новости): индекс |ход 4ч| {base_idx['mean_abs_r4']}%, >0.5% в {base_idx['share_gt_05']:.0%}; "
          f"бумаги |ход 4ч| {base_st['mean_abs_r4']}%, >1% в {base_st['share_gt_1']:.0%}")
    out = {"baseline": {"index": base_idx, "stocks": base_st}, "topics": {}, "by_importance": {}, "by_type": {}}
    print("\nПО ТЕМАМ (индекс IMOEXF, ход за 4 часа после новости):")
    print(f"{'тема':42} {'n':>5} {'ср.ход':>7} {'|ход|':>6} {'>0.5%':>6} {'>1%':>5} {'сторона':>8}  {'важн.модели':>12}")
    for name, pat in TOPICS:
        rx = re.compile(pat, re.I)
        sel = [r for r in idx if rx.search(r["title"] or "")]
        st = stats(sel)
        if not st or st["n"] < 15:
            continue
        imp = sum(r["importance"] for r in sel) / len(sel)
        out["topics"][name] = {**st, "model_importance_avg": round(imp, 1)}
        print(f"{name:42} {st['n']:>5} {st['mean_r4']:>+7.2f} {st['mean_abs_r4']:>6.2f} {st['share_gt_05']:>6.0%} {st['share_gt_1']:>5.0%} "
              f"{(str(st['dir_hit']) if st['dir_hit'] is not None else '—'):>8}  {imp:>12.1f}")
    print("\nПО ВАЖНОСТИ МОДЕЛИ (индекс):")
    for imp in range(2, 9):
        st = stats([r for r in idx if r["importance"] == imp])
        if st and st["n"] >= 20:
            out["by_importance"][imp] = st
            print(f"  важность {imp}: n={st['n']:>5}, |ход| {st['mean_abs_r4']:.2f}%, >0.5% в {st['share_gt_05']:.0%}, сторона {st['dir_hit']}")
    print("\nПО ТИПАМ (бумаги, ход 4ч):")
    by = defaultdict(list)
    for r in stocks:
        by[r["event_type"]].append(r)
    for k, v in sorted(by.items(), key=lambda kv: -len(kv[1])):
        st = stats(v)
        if st and st["n"] >= 30:
            out["by_type"][k] = st
            print(f"  {k:12} n={st['n']:>5}, ср.ход {st['mean_r4']:+.2f}%, |ход| {st['mean_abs_r4']:.2f}%, >1% в {st['share_gt_1']:.0%}, сторона {st['dir_hit']}")
    (ROOT / "data" / "news_calibration.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    # Короткий текст для контекста модели-аналитика (bot/analyst.py _context): темы по силе реального хода индекса
    top = sorted(((k, v) for k, v in out["topics"].items() if k not in ("переговоры (любые)", "санкции (слово)")), key=lambda kv: -kv[1]["mean_abs_r4"])
    lines = ["Калибровка по истории (архив 2024-10..2026-10, ход индекса Мосбиржи за 4 часа после новости; фон по всем новостям "
             f"{base_idx['mean_abs_r4']:.2f}%, больше 0.5% в {base_idx['share_gt_05']:.0%} случаев):"]
    for k, v in top:
        tag = "двигает рынок" if v["mean_abs_r4"] >= base_idx["mean_abs_r4"] * 1.1 else ("как фон" if v["mean_abs_r4"] >= base_idx["mean_abs_r4"] * 0.9 else "слабее фона")
        lines.append(f"- {k}: средний |ход| {v['mean_abs_r4']:.2f}%, больше 0.5% в {v['share_gt_05']:.0%}, n={v['n']} — {tag}")
    lines.append("Сторона хода по отдельной новости на истории угадывалась как монетка (50%), кроме решения ЦБ по ставке (64%) и отчётов (67%): "
                 "не делай уверенных выводов о стороне из одной новости, смотри на реакцию рынка и на поток.")
    (ROOT / "data" / "news_calibration.txt").write_text("\n".join(lines), encoding="utf-8")
    print("\nсохранено: data/news_calibration.json")


if __name__ == "__main__":
    main()
