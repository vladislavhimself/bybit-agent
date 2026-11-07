"""Ночной режим модели, тема рынка ночью, разбор убыточной сделки. Запуск из корня: python tests/test_night_postmortem.py"""
import sys
from datetime import datetime

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")
from bot.analyst import MSK, night_mode  # noqa: E402
from bot.postmortem import PostMortem, Verdict  # noqa: E402
from bot.theme import MarketTheme  # noqa: E402


class S:
    def __init__(self, d):
        self.d = d

    def get(self, k, default=None):
        return self.d.get(k, default)


s = S({"llm.night": {"enabled": True, "from": "00:00", "to": "07:00", "weekends": True}})
assert night_mode(s, datetime(2026, 11, 7, 10, 40, tzinfo=MSK)) is True, "суббота"
assert night_mode(s, datetime(2026, 11, 9, 3, 50, tzinfo=MSK)) is True, "понедельник ночью"
assert night_mode(s, datetime(2026, 11, 9, 7, 0, tzinfo=MSK)) is False, "07:00 — уже день"
assert night_mode(s, datetime(2026, 11, 9, 12, 0, tzinfo=MSK)) is False
assert night_mode(S({"llm.night": {"enabled": False}}), datetime(2026, 11, 7, 3, 0, tzinfo=MSK)) is False


class J:
    def __init__(self):
        self.kv, self.lessons_added = {}, []
        self.trades = [{"id": 36, "symbol": "RIZ6", "side": "Sell", "tier": "low", "score": -0.566, "news_driven": 0, "opened_ts": 1793980000,
                        "closed_ts": 1794034827, "entry": 93970.0, "stop_init": 94949.1, "exit": 94630.0, "pnl": -1238.82, "pnl_r": -0.77,
                        "close_reason": "стоп 94459.5 (программный)", "mfe_r": 0.3, "regime": "down", "reasons": ["крупные сделки за час: продают"]},
                       {"id": 30, "symbol": "GAZPF", "side": "Sell", "tier": "low", "score": -0.6, "news_driven": 0, "opened_ts": 1793700000,
                        "closed_ts": 1793800000, "entry": 110, "stop_init": 112, "exit": 108, "pnl": 500.0, "pnl_r": 1.0, "close_reason": "тейк",
                        "mfe_r": 1.5, "regime": "down", "reasons": []}]

    def kv_get(self, k, d=None):
        return self.kv.get(k, d)

    def kv_set(self, k, v):
        self.kv[k] = v

    def closed_trades(self, n=40):
        return self.trades

    def events_between(self, a, b, limit=80):
        return [{"ts": 1794000000, "importance": 8, "direction": "bullish", "market_wide": 1, "affected": [], "title": "Серия новостей: Трамп о звонках"}]

    def add_lesson(self, text, source="review"):
        self.lessons_added.append((text, source))


class A:
    model, cheap = "opus", "haiku"

    def __init__(self):
        self.calls = []

    def _parse(self, model, system, user, schema, max_tokens, effort):
        self.calls.append((model, effort, user))
        return Verdict(what_happened="шорт против ожидания переговоров", missed="тема рынка",
                       lesson_kind="prompt", lesson="Новости о дате мирных переговоров оценивать не ниже 8 и как общерыночные")


class TG:
    def __init__(self):
        self.sent = []

    def send(self, text, html=False):
        self.sent.append(text)


j, a, tg = J(), A(), TG()
pm = PostMortem(S({"llm.review_model": "opus", "postmortem": {"enabled": True, "hour_msk": 7}}), j, a, tg)
assert pm.due(datetime(2026, 11, 7, 6, 59, tzinfo=MSK)) is False and pm.due(datetime(2026, 11, 7, 7, 1, tzinfo=MSK)) is True
assert pm.run() == 1, "разбирается только убыточная сделка"
assert a.calls[0][0] == "opus" and a.calls[0][1] == "high" and "RIZ6" in a.calls[0][2] and "Трамп" in a.calls[0][2]
assert "РАЗБОР УБЫТКА" in tg.sent[0] and "/approve" in tg.sent[0] and "238.82" in tg.sent[0], tg.sent[0]
assert j.kv["postmortem_done"] == [36] and len(j.kv["pending_lessons"]) == 1
assert pm.run() == 0, "второй раз та же сделка не разбирается"
assert pm.run(force=True) == 0, "force — за 7 дней от текущего времени; сделка из прошлого в него не попадает"
msgs = pm.apply_pending()
assert len(msgs) == 1 and j.lessons_added[0][1] == "postmortem" and j.kv["pending_lessons"] == []
print(tg.sent[0])


class JT:
    def kv_get(self, k, d=None):
        return d


th = MarketTheme(S({"theme": {"enabled": True, "every_min": 60, "every_min_night": 180}, "llm.night": {"enabled": True, "weekends": True}}), JT(), None)
th.last_ts = 1000
import bot.analyst as an  # noqa: E402

real = an.night_mode
an.night_mode = lambda s_, now=None: True
assert th.due(1000 + 61 * 60) is False and th.due(1000 + 181 * 60) is True, "ночью тема раз в 3 часа"
an.night_mode = lambda s_, now=None: False
assert th.due(1000 + 61 * 60) is True
an.night_mode = real
print("ВСЁ ОК")
