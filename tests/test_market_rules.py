"""Случай 06.11 (шорт банка и индекса перед мирными переговорами) без сети: календарь рыночных событий, правила перед
событием, тема рынка, серии новостей, технические лонги в тенях, окно статистики, растягивание лимита модели.
Запуск из корня проекта: python tests/test_market_rules.py"""
import sys
import time
import types
from datetime import date, datetime, timedelta

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")
import bot.analyst as analyst_mod  # noqa: E402
from bot.analyst import Analyst  # noqa: E402
from bot.corpcal import MARKET, MSK, CorpCalendar, extract_market, trading_days_until  # noqa: E402
from bot.journal import Journal  # noqa: E402
from bot.main import Bot  # noqa: E402
from bot.theme import ClusterWatch, MarketTheme, ThemeView  # noqa: E402

# ---------- 1. рыночные события из текста ----------
t = date(2026, 11, 6)
assert extract_market("❗️ Переговоры в Абу-Даби должны состояться во вторник, 10 ноября, подтвердил Путин.", t) == [(date(2026, 11, 10), "geo")]
assert extract_market("ЕС планирует принять 21-й пакет санкций 20 ноября", t) == [(date(2026, 11, 20), "sanctions")]
assert extract_market("Заседание ОПЕК+ состоится 30 ноября", t) == [(date(2026, 11, 30), "opec")]
assert extract_market("Ближайшее заседание Банка России по ключевой ставке пройдёт 18 декабря", t) == [(date(2026, 12, 18), "cb")]
assert extract_market("Трамп может возобновить операцию против Ирана уже 9 ноября", t) == [], "нет слов про переговоры, санкции, заседания"
assert extract_market("Переговоры прошли 3 ноября и закончились ничем", t) == [], "прошедшая дата"
assert trading_days_until(date(2026, 11, 10), t) == 2 and trading_days_until(date(2026, 11, 9), t) == 1 and trading_days_until(t, t) == 0
print("рыночные события из текста: переговоры, санкции, ОПЕК+, ЦБ; прошедшее и постороннее не берётся")


# ---------- 2. правила перед событием ----------
class S:
    def __init__(self, d): self.d = d
    def get(self, k, d=None):
        cur = self.d
        for part in k.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return d
            cur = cur[part]
        return cur


CFG = {"corpcal": {"enabled": True, "pre_days": 3, "side_cap": 2, "unknown_before_min": 30, "unknown_after_min": 15},
       "risk": {"sectors": {"banks": ["SBER", "SVCB"], "oil_gas": ["GAZP"]}}}


class Ex:
    def instruments(self): return {b: {"base": b, "kind": "share"} for b in ("SBER", "SVCB", "GAZP", "X5")}


def bdays(n):
    d = datetime.now(MSK).date()
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


cc = CorpCalendar(S(CFG), Journal(":memory:"), Ex())
assert cc.add(MARKET, bdays(2), "geo", "Переговоры в Абу-Даби", "tg:test"), "переговоры: ожидание по умолчанию — надежда"
assert "шорт против течения" in cc.entry_block("SVCB", "banks", "short") and cc.entry_block("GAZP", "oil_gas", "short"), "шорт закрыт по всему рынку"
assert cc.entry_block("SVCB", "banks", "long") == ""
assert cc.side_cap("short") == 2 and cc.side_cap("long") is None
assert cc.tighten_for("SVCB", "banks", "Sell") is None, "до события два торговых дня: стоп ещё не трогаем"
print("за два дня до переговоров:", cc.entry_block("SVCB", "banks", "short"))
cc2 = CorpCalendar(S(CFG), Journal(":memory:"), Ex())
cc2.add(MARKET, bdays(1), "geo", "Переговоры", "t")
assert cc2.tighten_for("SVCB", "banks", "Sell")["kind"] == "geo" and cc2.tighten_for("SVCB", "banks", "Buy") is None
cc2.add(MARKET, bdays(6), "sanctions", "Пакет санкций", "t")
assert cc2.entry_block("SVCB", "banks", "long") == "", "до события «ждут плохого» больше трёх торговых дней"
cc3 = CorpCalendar(S(CFG), Journal(":memory:"), Ex())
cc3.add(MARKET, bdays(2), "sanctions", "Пакет санкций", "t")
assert "лонг против течения" in cc3.entry_block("GAZP", "oil_gas", "long") and cc3.entry_block("GAZP", "oil_gas", "short") == "" and cc3.side_cap("long") == 2
# событие по сектору действует только на сектор, по компании — только на компанию; в день события по компании лонг закрыт
cc4 = CorpCalendar(S(CFG), Journal(":memory:"), Ex())
cc4.add("SECTOR:banks", bdays(1), "sector", "Решение по льготной ипотеке", "t", "hope")
assert cc4.entry_block("SVCB", "banks", "short") and cc4.entry_block("GAZP", "oil_gas", "short") == ""
cc4.add("X5", datetime.now(MSK).date(), "dividend", "СД по дивидендам", "t")
assert "покупать поздно" in cc4.entry_block("X5", None, "long") and "шорт против течения" in cc4.entry_block("X5", None, "short")
# событие с неизвестным исходом и временем: окно −30…+15 минут
now_msk = datetime.now(MSK)
cc5 = CorpCalendar(S(CFG), Journal(":memory:"), Ex())
cc5.add(MARKET, now_msk.date(), "macro", "Недельная инфляция", "расписание", "unknown", (now_msk + timedelta(minutes=10)).strftime("%H:%M"))
if (now_msk + timedelta(minutes=10)).date() == now_msk.date():
    assert "окно события" in cc5.entry_block("SBER", "banks", "long")
    assert cc5.entry_block("SBER", "banks", "long", now=time.time() - 3600) == ""
assert any("РЫНОК" in x and "ждут хорошего" in x for x in cc.text(10)), cc.text(10)
print("правила: надежда закрывает шорты, опаска — лонги, область (рынок/сектор/компания) соблюдается, окно неизвестного события работает")

# ---------- 3. серии новостей ----------
T0 = 1_800_000_000
cw = ClusterWatch(S({"clusters": {"enabled": True, "window_min": 30, "min_items": 5, "quiet_min": 120}}))
titles = ["Путин подтвердил проведение трехсторонних переговоров по Украине в ОАЭ 10 ноября", "Путин назвал дату трехсторонних переговоров в Абу-Даби",
          "Трехсторонние переговоры России, США и Украины пройдут 10 ноября в Абу-Даби", "Путин обсудил с Совбезом предстоящие переговоры по Украине в ОАЭ",
          "Переговоры в Абу-Даби должны состояться 10 ноября, подтвердил Путин", "Путин: трехсторонние переговоры состоятся 10 ноября"]
dirs = ["neutral", "neutral", "bullish", "neutral", "neutral", "bullish"]
out = [cw.add({"market_wide": True, "title": x, "direction": d, "event_type": "regulation", "affected": []}, now=T0 + i * 120) for i, (x, d) in enumerate(zip(titles, dirs))]
fired = [x for x in out if x]
assert len(fired) == 1 and out[4] is fired[0], [bool(x) for x in out]
assert fired[0]["importance"] == 8 and fired[0]["direction"] == "bullish" and fired[0]["market_wide"] and fired[0]["n"] == 5
print("серия:", fired[0]["title"][:70], "| важность", fired[0]["importance"], fired[0]["direction"], "| на пятом сообщении, через", 4 * 2, "мин")
other = ["Brent обвалилась на рекордные 16% за неделю", "Безработица в США упала до 4,1%", "Иран пригрозил нефтью по 150 долларов", "ЦБ установил курс доллара", "Минфин разместил ОФЗ"]
cw2 = ClusterWatch(S({"clusters": {"enabled": True}}))
assert not any(cw2.add({"market_wide": True, "title": x, "direction": "neutral", "affected": []}, now=T0 + i) for i, x in enumerate(other)), "разные темы не склеиваются"
cw3 = ClusterWatch(S({"clusters": {"enabled": True}}), index_move=lambda m: 0.5)
r = [cw3.add({"market_wide": True, "title": x, "direction": "neutral", "affected": []}, now=T0 + i) for i, x in enumerate(titles)]
assert [x for x in r if x][0]["direction"] == "bullish", "все «нейтрально» — сторону показывает ход индекса"
cw4 = ClusterWatch(S({"clusters": {"enabled": True}}))
assert not any(cw4.add({"market_wide": True, "title": x, "direction": "neutral", "affected": []}, now=T0 + i * 1200) for i, x in enumerate(titles)), "за два часа — не серия"

# ---------- 4. тема рынка ----------
j = Journal(":memory:")
for i, x in enumerate(titles):
    j.add_event(f"n{i}", int(time.time()) - 600 - i, {"event_type": "regulation", "importance": 6, "direction": "bullish", "confidence": 0.6, "affected_symbols": [],
                "market_wide": True, "horizon_hours": 24, "reasoning": ""}, x)
view = {"v": ThemeView(theme="ожидание мирных переговоров", direction="bullish", confidence=0.8, horizon_days=2, reasoning="индекс растёт на новостях о переговорах")}
an = types.SimpleNamespace(model="main", cheap="cheap", spend_tier=lambda: "normal", _parse=lambda model, system, user, schema, mt, eff: view["v"])
th = MarketTheme(S({"theme": {"enabled": True, "min_confidence": 0.6, "max_age_min": 180}}), j, an, cc, index_move=lambda m: 0.4)
assert th.active() is None and th.blocks("short") == ""
th.refresh()
assert "против темы рынка" in th.blocks("short") and th.blocks("long") == "" and th.blocks("short", news_driven=True) == ""
print("тема:", th.text())
view["v"] = ThemeView(theme="смешанный фон", direction="bullish", confidence=0.5, horizon_days=1, reasoning="")
th.refresh()
assert th.blocks("short") == "", "уверенность ниже порога — тема не действует"
th.state = dict(th.state, confidence=0.9, ts=time.time() - 4 * 3600)
assert th.blocks("short") == "", "тема определена слишком давно — не действует"

# ---------- 5. бот: отказ из-за фона, технический лонг в тени, окно статистики ----------
recorded = []
b = types.SimpleNamespace(
    s=S({"signals": {"index_macro_window": {"before_min": 30, "after_min": 15}}, "risk": CFG["risk"]}), corpcal=cc,
    theme=types.SimpleNamespace(blocks=lambda d, nd=False: "", active=lambda: {"direction": "bullish"}),
    risk=types.SimpleNamespace(base_of=lambda s: {"SRZ6": "SBER", "RIZ6": "RTS"}.get(s, s)),
    cal=types.SimpleNamespace(blackout=lambda a, b: (False, None)),
    shadow=types.SimpleNamespace(record=lambda *a, **k: recorded.append(a)))
b._sector_of = lambda base: Bot._sector_of(b, base)
sg = lambda sym, d, nd=False: types.SimpleNamespace(symbol=sym, direction=d, news_driven=nd)  # noqa: E731
assert Bot._context_block(b, sg("SRZ6", "short"), {"base": "SBER"})[1] == "event", "фьючерс на Сбер перед переговорами: шорт закрыт"
assert Bot._context_block(b, sg("SRZ6", "long"), {"base": "SBER"}) == ("", "")
b.corpcal = None
b.theme = types.SimpleNamespace(blocks=lambda d, nd=False: "против темы рынка" if d == "short" and not nd else "", active=lambda: {"direction": "bullish"})
assert Bot._context_block(b, sg("SVCB", "short"), {"base": "SVCB"})[1] == "theme" and Bot._context_block(b, sg("SVCB", "short", True), {"base": "SVCB"}) == ("", "")
b.theme = None
b.cal = types.SimpleNamespace(blackout=lambda a, bb: (True, {"title": "Non-Farm Employment Change"}))
assert Bot._context_block(b, sg("RIZ6", "short"), {"base": "RTS"})[1] == "macro_window" and Bot._context_block(b, sg("SVCB", "short"), {"base": "SVCB"}) == ("", "")
b.theme = types.SimpleNamespace(active=lambda: {"direction": "bullish"})
ta = {"price_now": 300.0, "close": 300.0, "atr": 3.0, "regime": "trend_up"}
Bot._tech_long_shadow(b, "SBER", {"base": "SBER"}, {"reason": "weak", "direction": "long", "strength": 0.50, "news_driven": False}, ta, {"ta_long_mult": 0.85, "entry_threshold": 0.55})
assert len(recorded) == 1 and recorded[0][2] == "tech_long_theme" and abs(recorded[0][3] - 0.50 / 0.85) < 1e-9
Bot._tech_long_shadow(b, "SBER", {"base": "SBER"}, {"reason": "weak", "direction": "long", "strength": 0.40, "news_driven": False}, ta, {"ta_long_mult": 0.85, "entry_threshold": 0.55})
b.theme = types.SimpleNamespace(active=lambda: None)
Bot._tech_long_shadow(b, "SBER", {"base": "SBER"}, {"reason": "weak", "direction": "long", "strength": 0.50, "news_driven": False}, ta, {"ta_long_mult": 0.85, "entry_threshold": 0.55})
assert len(recorded) == 1, "слабый сигнал и рынок без позитивной темы — тени нет"
print("бот: шорт перед событием и против темы отклоняется; индексный фьючерс не входит в окно статистики; технический лонг по теме пишется в тень")

# ---------- 6. лимит модели растянут на день ----------
def tier(usd, hh, mm=0, pacing=True):
    analyst_mod.time.gmtime = lambda *a: time.struct_time((2026, 11, 9, hh, mm, 0, 0, 313, 0))
    a = types.SimpleNamespace(cost=types.SimpleNamespace(today=lambda: {"usd": usd}, month=lambda: 30.0), budget=6.0,
                              s=S({"llm": {"budget_soft_usd": 4.5, "budget_hard_usd": 6.0, "monthly_budget_usd": 180, "pacing": pacing}}))
    return Analyst.spend_tier(a)


assert tier(1.0, 4) == "normal" and tier(1.6, 4) == "economy", "к 07:00 МСК можно потратить четверть лимита (1.5 $)"
assert tier(3.0, 9) == "economy" and tier(3.0, 13) == "normal", "3 $ к полудню МСК — рано, к 16:00 — в графике"
assert tier(3.0, 9, pacing=False) == "normal" and tier(4.6, 20) == "economy" and tier(6.1, 12) == "minimal"
print("лимит модели: 3 $ к 12:00 МСК включает экономный режим, к 16:00 — нет; жёсткий порог прежний")
print("ВСЁ ОК")
