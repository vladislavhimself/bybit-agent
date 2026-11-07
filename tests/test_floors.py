"""Полы важности по правилам. Запуск из корня: python tests/test_floors.py"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")
from bot.analyst import apply_floors  # noqa: E402


def ev(imp=5, mw=False, rel=True):
    return {"importance": imp, "market_wide": mw, "relevant": rel, "reasoning": "r"}


d = ev(6)
note = apply_floors(d, "Путин подтвердил проведение трехсторонних переговоров по Украине в ОАЭ 10 ноября")
assert d["importance"] == 8 and d["market_wide"] is True and "6 -> 8" in note, (d, note)
d = ev(4)
apply_floors(d, "Переговоры в Абу-Даби должны состояться во вторник, 10 ноября, подтвердил Путин.")
assert d["importance"] == 8
d = ev(5)
apply_floors(d, "Уиткофф: переговоры на рабочем уровне между Россией и Украиной пройдут в Абу-Даби")
assert d["importance"] == 8
d = ev(9)
apply_floors(d, "Путин: переговоры с Украиной сорваны")
assert d["importance"] == 9 and d["market_wide"] is True, "выше пола — важность не трогаем, флаг «на весь рынок» ставим"
d = ev(5)
apply_floors(d, "ЦБ сохранил ключевую ставку на уровне 16%")
assert d["importance"] == 9 and d["market_wide"]
d = ev(5)
apply_floors(d, "ЕС принял 21-й пакет санкций против России")
assert d["importance"] == 7
d = ev(3, rel=False)
apply_floors(d, "В России объявлена частичная мобилизация")
assert d["importance"] == 9 and d["relevant"] is True
for txt in ["Профсоюз ведёт переговоры о зарплате с заводом", "Теннисистка выиграла турнир в Абу-Даби", "Трамп поздравил чемпиона",
            "Санкции за нарушение ПДД ужесточат", "Ставка по ипотеке выросла до 20%", "Переговоры с Ираном продолжаются"]:
    d = ev(3)
    assert apply_floors(d, txt) == "" and d["importance"] == 3, txt
d = ev(5)
assert apply_floors(d, "ЦБ сохранил ключевую ставку", enabled=False) == "" and d["importance"] == 5
print("ВСЁ ОК")
