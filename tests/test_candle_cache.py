"""Свечи в памяти с докачкой хвоста: результат должен совпадать с полной перекачкой; запросов — в разы меньше.
И запрет шорта перед дивидендной отсечкой. Без сети. Запуск из корня проекта: python tests/test_candle_cache.py"""
import sys
import threading
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")
import bot.moex as moex_mod  # noqa: E402
from bot.divcal import DivCalendar  # noqa: E402
from bot.moex import INTERVALS, MoexExchange, f2q  # noqa: E402


class S:
    def __init__(self, d): self.d = d
    def get(self, k, d=None):
        cur = self.d
        for part in k.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return d
            cur = cur[part]
        return cur


clock = {"t": datetime(2026, 11, 5, 12, 7, 30, tzinfo=timezone.utc).timestamp()}
moex_mod.time.time = lambda: clock["t"]
STEP = {v[0]: v[1] * 60 for v in INTERVALS.values()}
calls = []


def fake_call(svc, method, body=None, retries=8):
    """Рынок без перерывов: свеча на каждом шаге. Закрытая свеча неизменна, незакрытая меняется вместе с «сейчас»."""
    assert method == "GetCandles"
    calls.append(body["interval"])
    step = STEP[body["interval"]]
    p = lambda x: datetime.strptime(x, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()  # noqa: E731
    a, b = p(body["from"]), p(body["to"])
    out, ts = [], int(a // step) * step
    while ts <= b and ts <= clock["t"]:
        if ts >= a:
            closed = ts + step <= clock["t"]
            px = 100 + (ts % 86400) / 3600 + (0 if closed else (clock["t"] - ts) / step)
            out.append({"time": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "open": f2q(round(px, 2)), "high": f2q(round(px + 1, 2)),
                        "low": f2q(round(px - 1, 2)), "close": f2q(round(px, 2)), "volume": "10"})
        ts += step
    return {"candles": out}


def make(cache=True):
    ex = MoexExchange.__new__(MoexExchange)
    ex.s = S({"moex": {"candle_cache": cache}})
    ex._ccache, ex._clock = {}, threading.Lock()
    ex.call = fake_call
    return ex


ex, ref = make(), make(False)


def same(interval, limit):
    a, b = ex.candles_uid("u1", interval, limit), ref.candles_uid("u1", interval, limit)
    assert len(a) == len(b) == limit and (a.values == b.values).all(), (interval, a.tail(3), b.tail(3))


for iv, lim in (("60", 300), ("15", 100), ("240", 120), ("D", 250)):
    same(iv, lim)
calls.clear()
n_scans = 0
for minutes in (5, 5, 5, 5, 20, 5, 5, 5, 5, 5, 5, 5):          # час работы: скан каждые пять минут
    clock["t"] += minutes * 60
    n_scans += 1
    before = len(calls)
    a = ex.candles_uid("u1", "60", 300)
    a15 = ex.candles_uid("u1", "15", 100)
    t30 = ex.candles_uid("u1", "60", 30)      # так цену и оборот берёт ticker(): сразу после скана той же серии — без запроса
    cached_calls = len(calls) - before
    b, b15 = ref.candles_uid("u1", "60", 300), ref.candles_uid("u1", "15", 100)
    calls[:] = calls[:before + cached_calls]
    assert (a.values == b.values).all() and (a15.values == b15.values).all() and (t30.values == b.tail(30).values).all()
cached_total = len(calls)
calls.clear()
ref.candles_uid("u1", "60", 300); ref.candles_uid("u1", "15", 100); ref.candles_uid("u1", "60", 30)
full_per_scan = len(calls)
print(f"час работы, {n_scans} сканов: с памятью {cached_total} запросов, без неё {full_per_scan * n_scans} ({full_per_scan} на скан); данные совпадают")
assert cached_total <= n_scans * 2 + 8 and full_per_scan >= 6      # +8: одна плановая полная перекачка серий за час

# ночь и выходные: разрыв больше допустимого периода одного запроса — докачка идёт кусками, данные те же
clock["t"] += 2.6 * 86400
same("15", 100); same("60", 300); same("D", 250)
# через 1.5 часа серия перекачивается целиком (сверка с брокером)
clock["t"] += 5400
calls.clear()
same("60", 300)
assert len([c for c in calls if c == "CANDLE_INTERVAL_HOUR"]) >= 8, calls      # полная перекачка с памятью + полная без неё
# запрос большей длины, чем лежит в памяти, и исторический запрос (end задан) — мимо памяти
same("60", 900)
h = ex.candles_uid("u1", "5", 50, end=int((clock["t"] - 86400) * 1000))
assert len(h) == 50 and h["ts"].iloc[-1] <= (clock["t"] - 86400) * 1000
print("разрыв в 2.6 суток, полная перекачка, длинный и исторический запросы: данные совпадают")

# ---------- запрет шорта перед отсечкой ----------
real_now = datetime.now(timezone.utc).date()


def bdays_ahead(n):
    d, left = real_now, n
    while left > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            left -= 1
    return d.isoformat()


dc = DivCalendar(S({"risk": {"div_short_block": {"days": 20, "min_yield": 5}}}), None)
inst = {"X5": {"kind": "share", "base": "X5"}, "X5Z6": {"kind": "future", "base": "X5"}, "SBER": {"kind": "share", "base": "SBER"}, "GAZP": {"kind": "share", "base": "GAZP"}}
dc.next = {"X5": {"last_buy": bdays_ahead(15), "div": 204.0, "yield": 8.68}, "SBER": {"last_buy": bdays_ahead(30), "div": 30.0, "yield": 10.0},
           "GAZP": {"last_buy": bdays_ahead(10), "div": 2.0, "yield": 1.5}}
assert "шорт против течения" in dc.short_block("X5", inst) and dc.short_block("X5Z6", inst), "действует и на фьючерс бумаги"
assert dc.short_block("SBER", inst) == "", "до отсечки больше 20 торговых дней"
assert dc.short_block("GAZP", inst) == "", "доходность меньше 5%"
assert DivCalendar(S({"risk": {}}), None).short_block("X5", inst) == ""
print("шорт перед отсечкой: X5 и её фьючерс закрыты за 15 торговых дней при 8.7%; дальняя отсечка и малый дивиденд не мешают")
print("ВСЁ ОК")
