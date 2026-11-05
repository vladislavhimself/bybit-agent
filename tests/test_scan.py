"""Офлайн-проверка: новость посреди скана обрабатывается сразу, скан доходит до конца; позиции ведутся и во время скана."""
import queue
import sys
import time
import types
from collections import Counter

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")
from bot.main import Bot  # noqa: E402

syms = [f"S{i:02d}" for i in range(43)]
calls = Counter()
visited = []
b = types.SimpleNamespace()
b.risk = types.SimpleNamespace(check=lambda eq: (True, ""), halted=False)
b.eventplay = None
b.j = types.SimpleNamespace(recent_events=lambda h: [], open_trades=lambda: [], pendings=lambda: [])
b.s = types.SimpleNamespace(get=lambda k, d=None: {"signals": {}, "intervals": {"manage_sec": 30}, "signals.entry_threshold": 0.55}.get(k, d))
b.ex = types.SimpleNamespace(instruments=lambda: {s: {"base": s} for s in syms}, positions=lambda: [], equity=lambda: (100000.0, 100000.0))
b.tg = types.SimpleNamespace(commands=[])
b.events_q = queue.Queue()
b.last = {"manage": time.time()}
b.cache, b.factors, b.divcal = {}, None, None
b.closed_since_review = 0
b.funnel_since_report, b.best_weak_since_report = Counter(), None
b._universe = []


def market_data(sym, max_age):
    visited.append(sym)
    if sym == "S10":
        b.events_q.put({"importance": 3})       # новость пришла посреди скана
    if sym == "S20":
        b.last["manage"] = time.time() - 31     # подошло время вести позиции
    return None, {}, None


def handle_events(equity, positions):
    calls["events"] += 1
    while not b.events_q.empty():
        b.events_q.get_nowait()
    b._universe_seen = list(b._universe)


b.market_data = market_data
b.handle_events = handle_events
b.exe = types.SimpleNamespace(check_pendings=lambda eq: calls.update(["pendings"]), manage=lambda: (calls.update(["manage"]), [])[1])
n = Bot.evaluate(b, syms, 100000.0, [], tag="скан")
print("бумаг в списке", len(syms), "| просмотрено", len(visited), "| обработок новости", calls["events"], "| ведений позиций", calls["manage"], "| открыто", n)
assert visited == syms and calls["events"] == 1 and calls["manage"] == 1 and calls["pendings"] == 1
assert b._universe == syms
# реакция на новость по трём бумагам не затирает список скана
Bot.evaluate(b, syms[:3], 100000.0, [], tag="реакция на новость")
assert b._universe == syms
print("ВСЁ ОК")
