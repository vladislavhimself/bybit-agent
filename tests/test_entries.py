"""Входы без сети: свежесть новости, утренний список бумаг, слишком дорогие контракты, бумаги из новостей.
Запуск из корня проекта: python tests/test_entries.py"""
import json
import sys
import tempfile
import time
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8")
import bot.moex as moex_mod  # noqa: E402
from bot.main import Bot  # noqa: E402
from bot.moex import MoexExchange  # noqa: E402


class S:
    def __init__(self, d): self.d = d
    def get(self, k, d=None):
        cur = self.d
        for part in k.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return d
            cur = cur[part]
        return cur


# ---------- 1. свежесть новости ----------
now = time.time()
b = types.SimpleNamespace(s=S({"signals": {"news_entry_min_importance": 7, "news_entry_max_age_min": 15}}), paper_mode=False)
ev = lambda imp, age_min, d="bullish": {"importance": imp, "confidence": 0.9, "ts": now - age_min * 60, "direction": d}  # noqa: E731
sig = lambda events, direction="long": types.SimpleNamespace(news_driven=True, events=events, direction=direction, tier="medium", reasons=[])  # noqa: E731
assert Bot._gate(b, sig([ev(9, 240), ev(8, 1)]), {}) is None, "свежая важности 8 рядом со старой важности 9 — вход разрешён"
assert Bot._gate(b, sig([ev(9, 240)]), {}) == "stale_news"
assert Bot._gate(b, sig([ev(6, 1)]), {}) == "stale_news", "свежая, но слабая"
assert Bot._gate(b, sig([ev(9, 1, "bearish")]), {}) == "stale_news", "свежая, но против сделки"
assert Bot._gate(b, sig([ev(9, 1, "bearish")], "short"), {}) is None
print("свежесть новости: по свежей и достаточно важной новости в сторону сделки")

# ---------- 2. оборот: неполный день не затирает полный ----------
tmp = Path(tempfile.mkdtemp())
moex_mod.DATA = tmp
ex = MoexExchange.__new__(MoexExchange)
ex.s, ex.mode = S({}), "demo"
clock = {}
ex._msk_now = lambda: clock["t"]
mon = datetime(2026, 11, 9, tzinfo=timezone.utc)      # понедельник


def at(day_off, hh, mm):
    clock["t"] = mon + timedelta(days=day_off, hours=hh, minutes=mm)


at(0, 9, 0)
assert ex._with_full_day({"SBER": 1e8}) == {"SBER": 1e8}, "среза ещё нет: как раньше"
at(0, 23, 0)
ex._with_full_day({"SBER": 6e9, "GAZP": 5e9, "VKCO": 3e9})
assert json.loads((tmp / "moex_turnover_full.json").read_text(encoding="utf-8"))["date"] == "2026-11-09"
at(1, 7, 30)      # вторник, утренняя сессия: сегодняшний оборот — копейки
out = ex._with_full_day({"SBER": 5e7, "GAZP": 4e7, "VKCO": 1e7})
assert out == {"SBER": 6e9, "GAZP": 5e9, "VKCO": 3e9}, out
at(1, 11, 0)      # 11:00: оценке ещё не верим
assert ex._with_full_day({"SBER": 9e9})["SBER"] == 6e9
at(1, 14, 0)      # днём: максимум из вчера и сегодняшней оценки
out = ex._with_full_day({"SBER": 9e9, "GAZP": 1e9, "NEWCO": 2e9})
assert out["SBER"] == 9e9 and out["GAZP"] == 5e9 and out["NEWCO"] == 2e9
at(5, 12, 0)      # суббота: только сегодняшний оборот, как раньше
assert ex._with_full_day({"SBER": 3e8}) == {"SBER": 3e8}
at(8, 9, 0)       # срезу 8 дней: не используем
assert ex._with_full_day({"SBER": 5e7}) == {"SBER": 5e7}
print("оборот: утром список строится по вчерашнему полному дню, днём — по максимуму; выходные и старый срез — как раньше")

# ---------- 3. список бумаг и бумаги из новостей ----------
exp_far = (datetime.now(timezone.utc) + timedelta(days=120)).strftime("%Y-%m-%d")
exp_near = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")
inst = {
    "LKOH": {"kind": "share", "base": "LKOH", "expiration": None, "mult": 1.0},
    "LKH7": {"kind": "future", "base": "LKOH", "expiration": exp_far, "mult": 10.0},
    "RIZ6": {"kind": "future", "base": "RTS", "expiration": exp_far, "mult": 1.5},
    "MTSS": {"kind": "share", "base": "MTSS", "expiration": None, "mult": 10.0},
    "MTZ6": {"kind": "future", "base": "MTSS", "expiration": exp_far, "mult": 100.0},
    "SBER": {"kind": "share", "base": "SBER", "expiration": None, "mult": 10.0},
    "SRZ6": {"kind": "future", "base": "SBER", "expiration": exp_near, "mult": 100.0},
    "SBERF": {"kind": "future", "base": "SBER", "expiration": None, "mult": 100.0, "perpetual": True},
    "TINY": {"kind": "share", "base": "TINY", "expiration": None, "mult": 1.0},
    "FLOT": {"kind": "share", "base": "FLOT", "expiration": None, "mult": 10.0},
    "FLZ6": {"kind": "future", "base": "FLOT", "expiration": exp_near, "mult": 100.0},
}
cfg = {"universe": {"top_n_by_turnover": 40, "min_turnover_24h_usd": 500e6, "always": [], "blacklist": ["MTSS"]},
       "moex": {"allowed_future_bases": ["IMOEX", "RTS"]}, "risk": {"capital_cap_usd": 100000, "max_margin_share": 0.6}}
ex = MoexExchange.__new__(MoexExchange)
ex.s, ex.mode = S(cfg), "demo"
ex.instruments = lambda: inst
turn = {"LKOH": 7e9, "LKH7": 2e9, "RIZ6": 9e9, "MTSS": 2e9, "MTZ6": 1e9, "SBER": 6e9, "SRZ6": 5e9, "SBERF": 3e9, "TINY": 2e7, "FLOT": 4e8, "FLZ6": 3e8}
ex._turn_cache = turn
ex._turnover_today = lambda: turn
ex._px = {"LKH7": 6170.0, "RIZ6": 101000.0, "SBERF": 310.0}     # контракт Лукойла 61 700, РТС 151 500 при пороге 60 000
u = ex.universe()
print("список:", u)
assert "LKOH" in u and "LKH7" not in u, "дорогой фьючерс не занимает место: берётся акция"
assert "RIZ6" not in u and "MTSS" not in u and "MTZ6" not in u
assert "SBERF" in u and "SRZ6" not in u and "SBER" not in u
assert ex.news_pick("MTSS") is None, "стоп-лист действует и для бумаг из новостей"
assert ex.news_pick("TINY") is None, "неликвид из новости не берём"
assert ex.news_pick("SBER") == "SBERF"
assert ex.news_pick("FLOT") == "FLOT", "контракт за 2 дня до экспирации не берём: акция"
assert ex.news_pick("LKOH") == "LKOH"
ex.mode = "live"
assert "LKH7" in ex.universe(), "на реальном счёте нужно только ГО: ограничение не действует"
print("бумаги из новостей: стоп-лист, ликвидность и срок до экспирации учитываются")
print("ВСЁ ОК")
