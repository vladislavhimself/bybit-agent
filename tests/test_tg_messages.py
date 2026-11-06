"""Сообщения в Telegram: формат блока «Позиции» и защита от повторов одной новости.
Запуск из корня проекта: python tests/test_tg_messages.py"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")
from bot import fmt  # noqa: E402
from bot.theme import SentNews  # noqa: E402

# --- формат позиций ---
fmt.NAMES.update({"PXZ6": "Полюс, фьючерс до 18.12.2026", "RIZ6": "индекс РТС, фьючерс до 18.12.2026"}) if hasattr(fmt, "NAMES") else None
m = {"tradable": 294000, "reserve": 0, "day_pct": -0.3, "week_pct": -1.0, "drawdown_pct": 2.0, "halted": False, "halt_reason": ""}
pos = [{"symbol": "PXZ6", "side": "Sell", "qty": 6, "entry": 10812, "mult": 1, "upnl": -288.0, "mark": 10860, "leverage": 0.2, "sl": 10987},
       {"symbol": "RIZ6", "side": "Sell", "qty": 1, "entry": 93970, "mult": 1.6, "upnl": -343.54, "mark": 94180, "leverage": 0.5, "sl": 94950}]
txt = fmt.balance_message(294000, m, pos, {}, 300000, pendings=[{"symbol": "SBER", "side": "Buy", "price": 300.5}])
blk = txt.split("<b>Позиции</b>\n")[1].split("\n\n")[0].split("\n")
print("\n".join(blk))
assert "@" not in txt, "знака @ в отчёте быть не должно"
assert blk[0].startswith("1. PXZ6") and blk[1] == "ШОРТ", blk[:2]
assert blk[2].startswith("-288.00") and "(-0.44%)" in blk[2], blk[2]
assert blk[3] == "Цена 10860, плечо 0.2x, стоп 10987", blk[3]
assert blk[4].strip() == "" and blk[5].startswith("2. RIZ6") and blk[6] == "ШОРТ", blk[4:7]
assert "1. " in txt.split("<b>Ждут отката</b>\n")[1].split("\n")[0] and "ордер по цене 300.5" in txt


# --- повторы ---
class J:
    def __init__(self):
        self.kv = {}

    def kv_get(self, k, d=None):
        return self.kv.get(k, d)

    def kv_set(self, k, v):
        self.kv[k] = v


class S:
    def get(self, k, d=None):
        return d


j = J()
sn = SentNews(S(), j)
t0 = 1_800_000_000
cian = ["«Т-Технологии» планируют выкупить до 100% «Циана»",
        "«Т-Технологии» хотят купить ЦИАН. Разбираемся в деталях",
        "⚡️ Т-Технологии объявляют о плане купить до 100% компании ЦИАН Оферта по ЦИАН будет выставлена",
        "Т-Технологии планируют приобрести до 100% акций Циан. На фоне новости котировки площадки выросли",
        "🗞 «Т-Технологии» предложили акционерам ЦИАН выкупить до 100% акций платформы по 993,05 руб. за бумагу"]
res = [sn.repeat(["CNRU", "T"] if i == 0 else ["CNRU"], "partnership", "bullish", x, t0 + i * 600) for i, x in enumerate(cian)]
assert res == [False, True, True, True, True], res
assert sn.repeat(["CNRU"], "partnership", "bearish", "Т-Технологии отказались от покупки ЦИАН", t0 + 4000) is False, "обратная новость — не повтор"
assert sn.repeat(["ROSN"], "earnings", "bullish", "СД РОСНЕФТЬ: ДИВИДЕНДЫ 9М 2026Г 9.44 РУБ/АКЦ", t0 + 4100) is False, "другая бумага"
assert sn.repeat(["CNRU"], "partnership", "bullish", cian[0], t0 + 13 * 3600) is False, "через 12 часов можно снова"
assert sn.repeat([], "geopolitics", "bullish", "Путин подтвердил проведение трехсторонних переговоров по Украине в ОАЭ 10 ноября", t0) is False
assert sn.repeat([], "cluster", "bullish", "Путин назвал дату трехсторонних переговоров в Абу-Даби", t0 + 300) is True, "серия о том же — повтор"
assert sn.repeat([], "geopolitics", "bullish", "ЦБ неожиданно снизил ключевую ставку", t0 + 400) is False
assert SentNews(S(), j).repeat(["ROSN"], "earnings", "bullish", "Роснефть: дивиденды 9.44 руб.", t0 + 4200) is True, "память переживает перезапуск"
print("ВСЁ ОК")
