"""Ожидания по правилам и журнал расхождений; news_mode off. Запуск из корня: python tests/test_expect_rules.py"""
import os
import sys
import tempfile
import time

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")
from bot.expect_rules import ExpectRules, company_of  # noqa: E402
from bot.journal import Journal  # noqa: E402
from bot.signals import news_score  # noqa: E402


class S:
    def __init__(self, d):
        self.d = d

    def get(self, k, default=None):
        return self.d.get(k, default)


tmp = os.path.join(tempfile.mkdtemp(), "t.db")
j = Journal(tmp)
er = ExpectRules(S({"moex.cbr_current_rate": 14.0}), j)
now = int(time.time())

# ставка: консенсус из ленты; чужие ЦБ и размер шага не считаются
assert er.ingest({"id": "n1", "title": "Согласно опросу Reuters, 26 из 26 аналитиков ожидают сохранения ключевой ставки на уровне 14% на заседании в пятницу",
                  "summary": "", "published_ts": now - 3600}) == 1
assert er.ingest({"id": "n2", "title": "ЦБ Турции, как и ожидалось, сохранил ключевую ставку на уровне 40%", "summary": "", "published_ts": now - 3600}) == 0
assert er.ingest({"id": "n3", "title": "Аналитики ожидают снижения ключевой ставки на 0,5 п.п. до 13,5% — консенсус РБК", "summary": "", "published_ts": now - 7200}) == 1
v, n, age = er.expectation("cbr", "CBR", now)
assert v in (14.0, 13.5) and n >= 1, (v, n)
cons, src, strong = er.cbr_consensus(now)
assert cons in (14.0, 13.5) and "ленты" in src, (cons, src)

# дивиденд: прогноз по компании, потом рекомендация → расхождение
assert er.ingest({"id": "d1", "title": "Аналитики SberCIB ожидают дивиденд Сбера за 2026 год около 36 руб. на акцию", "summary": "", "published_ts": now - 86400}) == 1
info = er.on_event({"event_type": "earnings", "affected_symbols": ["SBER"]}, "Набсовет Сбербанка рекомендовал дивиденды за 2026 год в размере 36 руб. на акцию", "d2", now, "disclosure")
assert info and info["kind"] == "div" and abs(info["surprise"]) < 0.01, info
info = er.on_event({"event_type": "earnings", "affected_symbols": ["SBER"]}, "Набсовет Сбербанка рекомендовал дивиденды в размере 50 руб. на акцию", "d3", now, "disclosure")
assert info and info["surprise"] > 0.35 and info["size"] > 2, info

# отчёт: сторона из формулировки ленты
info = er.on_event({"event_type": "earnings", "affected_symbols": ["PLZL"]}, "«Полюс» отчитался за полугодие: показатели хуже ожиданий аналитиков", "e1", now, "tg:x")
assert info and info["kind"] == "earn" and info["surprise"] == -1.0, info
info = er.on_event({"event_type": "earnings", "affected_symbols": []}, "ВТБ отчитался о прибыли: результаты лучше консенсус-прогноза", "e2", now, "rss")
assert info and info["base"] == "VTBR" and info["surprise"] == 1.0, info
assert er.on_event({"event_type": "macro", "affected_symbols": []}, "Нефть Brent выросла на 2%", "e3", now, "rss") is None

# недельная инфляция — факт как ожидание на следующую среду
assert er.ingest({"id": "c1", "title": "С 20 по 26 октября инфляция в России составила 0,08%, неделей ранее — 0,05%", "summary": "", "published_ts": now - 3 * 86400}) == 1
cpi, _, _ = er.expectation("cpi", "CPI", now, days=10)
assert cpi == 0.08, cpi

# журнал и текст
rows = j._run("SELECT * FROM surprises", (), "all")
assert len(rows) == 4, len(rows)
er.confirm(rows[0]["id"], -0.2, True, "short: IMOEXF")
er.set_result(rows[0]["id"], 1.5)
txt = "\n".join(er.text(14))
assert "Расхождения" in txt and "результат +1.5%" in txt, txt
assert company_of("#GAZP #дивиденды СД рекомендовал...") == "GAZP"
assert company_of("Сбер и ВТБ отчитались") is None

# news_mode off: новостной балл всегда ноль
ev = [{"importance": 9, "direction": "bullish", "affected": ["SBER"], "market_wide": False, "ts": now - 60, "horizon_hours": 24, "confidence": 0.9, "event_type": "earnings", "title": "x"}]
assert news_score("SBER", ev, 5, now, mode="off") == (0.0, [], [])
assert news_score("SBER", ev, 5, now, mode="stock_long_only")[0] > 0
print("ВСЁ ОК: ожидания по правилам, расхождения, news_mode off")
