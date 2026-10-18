"""Самообучение: периодический разбор закрытых сделок моделью, уроки и правки параметров в заданных границах."""
import json
import logging
from typing import List

import anthropic
from pydantic import BaseModel

from .config import TUNABLE_BOUNDS

log = logging.getLogger("review")


class ParamChange(BaseModel):
    name: str
    new_value: float
    reason: str


class Review(BaseModel):
    summary: str
    lessons: List[str]
    param_changes: List[ParamChange]


SYSTEM = """Ты проводишь разбор сделок автономного торгового бота (крипто-перпетуалы, Bybit).
Цель: найти системные ошибки и предложить правки, которые уменьшат просадку и повысят ожидание на сделку.
Правила:
- Не делай выводов по 1-3 сделкам: отмечай только закономерности, повторившиеся минимум 3 раза.
- Уроки формулируй как проверяемые правила ("не входить в лонг при фандинге выше 0.05%"), не общими словами.
- Правки параметров только из списка разрешённых и только в их границах. Меняй не больше 3 параметров за раз
  и не более чем на 20% от текущего значения. Если данных мало, param_changes оставь пустым.
- Всё на русском, кратко."""


class Reviewer:
    def __init__(self, settings, journal, tg):
        self.s, self.j, self.tg = settings, journal, tg
        self.client = anthropic.Anthropic(max_retries=3, timeout=180.0)
        self.model = settings.get("llm.review_model")

    def lessons_text(self, limit=15):
        rows = self.j.lessons(limit)
        return "\n".join(f"- {r['text']}" for r in rows)

    def run(self, apply: bool):
        # Разбираем только сделки, закрытые после прошлого разбора: одни и те же убытки нельзя наказывать дважды
        last_ts = self.j.kv_get("last_review_ts", 0)
        trades = [t for t in self.j.closed_trades(40) if t["closed_ts"] > last_ts]
        if len(trades) < 5:
            log.info("Разбор: новых закрытых сделок %d (нужно 5), пропуск", len(trades))
            return None
        stats = self.j.stats()
        prev = [t for t in self.j.closed_trades(40) if t["closed_ts"] <= last_ts][:15]
        current = {k: self.s.get(k) for k in TUNABLE_BOUNDS}
        bounds = {k: list(v) for k, v in TUNABLE_BOUNDS.items()}
        compact = [{
            "symbol": t["symbol"], "side": t["side"], "tier": t["tier"], "regime": t["regime"],
            "news_driven": bool(t["news_driven"]), "pnl_r": round(t["pnl_r"], 2), "pnl": round(t["pnl"], 2),
            "hours": round((t["closed_ts"] - t["opened_ts"]) / 3600, 1), "close_reason": t["close_reason"],
            "reasons": t["reasons"][:4],
        } for t in trades]
        prev_compact = [{"symbol": t["symbol"], "side": t["side"], "tier": t["tier"], "pnl_r": round(t["pnl_r"], 2),
                         "close_reason": (t["close_reason"] or "")[:40]} for t in prev]
        user = (f"Общая статистика за всё время: {json.dumps(stats, ensure_ascii=False)}\n"
                f"Текущие параметры: {json.dumps(current, ensure_ascii=False)}\n"
                f"Границы: {json.dumps(bounds, ensure_ascii=False)}\n"
                f"Прошлые уроки (по ним уже внесены правки в код и параметры, не повторяй их):\n{self.lessons_text()}\n\n"
                f"НОВЫЕ сделки с прошлого разбора, именно их разбираем ({len(compact)}):\n"
                f"{json.dumps(compact, ensure_ascii=False, indent=1)}\n\n"
                f"Старые сделки только для контекста, по ним выводы уже сделаны:\n"
                f"{json.dumps(prev_compact, ensure_ascii=False)}\n\n"
                f"Важно: если новых сделок мало или они не образуют закономерность, param_changes оставь пустым. "
                f"Повышение entry_threshold допустимо только если новые сделки показывают, что слабые сигналы "
                f"проигрывают, а сильные нет; иначе бот перестанет торговать и учиться.")
        try:
            resp = self.client.messages.parse(model=self.model, max_tokens=16000, output_config={"effort": "high"}, system=SYSTEM,
                                              messages=[{"role": "user", "content": user}], output_format=Review)
        except anthropic.APIError as e:
            log.error("review: %s", e)
            return None
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            return None
        rv = resp.parsed_output
        for l in rv.lessons:
            self.j.add_lesson(l)
        applied, lines = [], []
        for ch in rv.param_changes:
            if apply:
                ok, msg = self.s.set_learned(ch.name, ch.new_value)
                lines.append(("✔ " if ok else "✖ ") + msg + f" — {ch.reason}")
                if ok:
                    applied.append(ch.model_dump())
            else:
                lines.append(f"предложение: {ch.name} -> {ch.new_value} — {ch.reason}")
        self.j.add_review(rv.summary, [c.model_dump() for c in rv.param_changes], apply)
        self.j.kv_set("last_review_ts", max(t["closed_ts"] for t in trades))
        if not apply and rv.param_changes:
            self.j.kv_set("pending_changes", [c.model_dump() for c in rv.param_changes])
        text = ("🧠 Разбор сделок\n" + rv.summary + "\n\nУроки:\n" + "\n".join("• " + l for l in rv.lessons)
                + ("\n\nПараметры:\n" + "\n".join(lines) if lines else "")
                + ("" if apply or not rv.param_changes else "\n\nПрименить: /approve"))
        self.tg.send(text)
        return rv

    def apply_pending(self):
        pend = self.j.kv_get("pending_changes", [])
        msgs = []
        for ch in pend:
            ok, msg = self.s.set_learned(ch["name"], ch["new_value"])
            msgs.append(msg)
        self.j.kv_set("pending_changes", [])
        return msgs
