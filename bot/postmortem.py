"""Разбор каждой убыточной сделки на следующее утро.

06.11.2026 три шорта подряд открылись против главной темы дня (ожидание переговоров) и все закрылись в минус. Еженедельный разбор
(bot/review.py) ищет закономерности от трёх повторов — такие потери он заметил бы через неделю. Здесь каждая убыточная сделка
разбирается отдельно: сильная модель получает сделку, новости за сутки до входа и во время неё, календарь и тему рынка и говорит,
чего не хватило. Урок для модели-аналитика владелец принимает командой /approve, правку кода согласуем отдельно."""
import logging
import time
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from . import fmt
from .corpcal import MSK

log = logging.getLogger("postmortem")

SYSTEM = """Ты разбираешь убыточную сделку автономного торгового бота на Мосбирже (сделки на часы и дни; вход по техническому сигналу,
новостям и факторам; стоп у брокера). Задача: честно сказать, что бот не учёл, и дать урок, который предотвратит такую же потерю.
- Смотри в комплексе: новости за сутки до входа и во время сделки, календарь событий, тема рынка, причины входа. Технический сигнал —
  только одна из причин; главный вопрос — не шла ли сделка против того, чем жил весь рынок.
- Если вход был разумным и потеря — обычный риск стратегии (стоп сработал без особых причин), так и скажи: lesson_kind = none.
- lesson_kind = prompt: урок для модели, которая оценивает новости (какую важность, сторону, флаг «на весь рынок» ставить таким
  новостям). Одна проверяемая фраза-правило.
- lesson_kind = code: нужно новое правило бота (запрет, условие, порог). Опиши так, чтобы программист реализовал без вопросов.
- Не выдумывай: опирайся только на переданное; если данных не хватает, скажи, каких. Всё на русском, коротко и по делу."""


class Verdict(BaseModel):
    what_happened: str
    missed: str
    lesson_kind: Literal["prompt", "code", "none"]
    lesson: str


def _t(ts):
    return datetime.fromtimestamp(int(ts), MSK).strftime("%d.%m %H:%M") if ts else "—"


class PostMortem:
    def __init__(self, settings, journal, analyst, tg, corpcal=None, theme=None):
        self.s, self.j, self.analyst, self.tg, self.corpcal, self.theme = settings, journal, analyst, tg, corpcal, theme

    def cfg(self):
        return self.s.get("postmortem") or {}

    def due(self, now=None):
        if not self.cfg().get("enabled", True):
            return False
        d = now or datetime.now(MSK)
        return d.hour >= int(self.cfg().get("hour_msk", 7)) and self.j.kv_get("postmortem_day") != d.strftime("%Y-%m-%d")

    def tick(self):
        try:
            self.j.kv_set("postmortem_day", datetime.now(MSK).strftime("%Y-%m-%d"))
            self.run()
        except Exception as e:  # noqa: BLE001
            log.warning("разбор убытков: %s", e)

    def run(self, force=False):
        """Разобрать убыточные сделки, ещё не разобранные (force — за 7 дней заново). Возвращает число разборов."""
        done = set(self.j.kv_get("postmortem_done", []) or [])
        since = time.time() - 7 * 86400
        # только сделки за 7 дней, новые первыми (07.11 первый запуск разобрал пять сделок октября вместо вчерашних)
        trades = [t for t in self.j.closed_trades(40) if t.get("pnl") is not None and t["pnl"] < 0
                  and (t["closed_ts"] or 0) >= since and (force or t["id"] not in done)]
        n = 0
        for t in sorted(trades, key=lambda x: -(x["closed_ts"] or 0))[:int(self.cfg().get("max_per_run", 5))]:
            v = self.analyze(t)
            done.add(t["id"])
            self.j.kv_set("postmortem_done", sorted(done)[-300:])
            if v:
                self.notify(t, v)
                n += 1
        return n

    def analyze(self, t):
        base = fmt.base_of(t["symbol"])
        a, b = int(t["opened_ts"]) - 24 * 3600, int(t["closed_ts"] or time.time())
        evs = self.j.events_between(a, b, 200)
        mine = [e for e in evs if base in (e.get("affected") or []) or e.get("market_wide") or int(e.get("importance") or 0) >= 6][:60]
        mine.sort(key=lambda e: e["ts"])
        news = "\n".join(f"- {_t(e['ts'])} важность {e['importance']}, {e['direction']}{', рынок' if e.get('market_wide') else ''}"
                         f"{', ' + ','.join(e['affected']) if e.get('affected') else ''}: {(e.get('title') or '')[:150]}" for e in mine) or "нет"
        cal = "\n".join(self.corpcal.text(14, 15)) if self.corpcal else ""
        theme = self.theme.text() if self.theme else ""
        side = "ЛОНГ" if t["side"] == "Buy" else "ШОРТ"
        user = (f"Сделка: {fmt.label(t['symbol'])} {side}, размер {t.get('tier')}, сила сигнала {t.get('score')}, по новости: {bool(t.get('news_driven'))}.\n"
                f"Открыта {_t(t['opened_ts'])} по {t['entry']:g}, стоп при входе {float(t.get('stop_init') or 0):g}, закрыта {_t(t['closed_ts'])} по {float(t.get('exit') or 0):g}: "
                f"{t['pnl']:+.0f} руб. ({float(t.get('pnl_r') or 0):+.2f}R), причина закрытия: {t.get('close_reason')}. "
                f"Лучший ход в пользу сделки: {float(t.get('mfe_r') or 0):+.2f}R. Режим индекса при входе: {t.get('regime')}.\n"
                f"Причины входа: {'; '.join(str(x) for x in (t.get('reasons') or [])[:8])}\n\n"
                f"Новости за сутки до входа и во время сделки (по времени):\n{news}\n\n"
                f"Календарь событий сейчас:\n{cal or 'нет'}\n\nТема рынка сейчас: {theme or 'не определена'}")
        model = self.s.get("llm.review_model") or self.analyst.model
        try:
            return self.analyst._parse(model, SYSTEM, user, Verdict, 6000, "high")
        except Exception as e:  # noqa: BLE001
            log.warning("разбор сделки %s: %s", t["id"], e)
            return None

    def notify(self, t, v):
        side = "ЛОНГ" if t["side"] == "Buy" else "ШОРТ"
        kind = {"prompt": "урок для модели-аналитика новостей", "code": "нужна правка правил бота", "none": "обычный риск стратегии, правило не нужно"}[v.lesson_kind]
        parts = [f"🔍 <b>РАЗБОР УБЫТКА</b>: {fmt.esc(fmt.label(t['symbol']))} {side} {fmt.signed(t['pnl'])} {fmt.CUR} ({float(t.get('pnl_r') or 0):+.2f}R)",
                 fmt.block("Что произошло", fmt.esc(v.what_happened)),
                 fmt.block("Чего не хватило", fmt.esc(v.missed))]
        if v.lesson_kind == "prompt":
            pend = self.j.kv_get("pending_lessons", []) or []
            pend.append({"text": v.lesson, "trade": t["id"]})
            self.j.kv_set("pending_lessons", pend[-20:])
            parts.append(fmt.block("Урок", fmt.esc(v.lesson), f"({kind}) Принять: /approve"))
        elif v.lesson_kind == "code":
            sug = self.j.kv_get("code_suggestions", []) or []
            sug.append({"ts": int(time.time()), "trade": t["id"], "text": v.lesson})
            self.j.kv_set("code_suggestions", sug[-30:])
            parts.append(fmt.block("Предложение", fmt.esc(v.lesson), f"({kind} — согласуем отдельно)"))
        else:
            parts.append(fmt.block("Вывод", fmt.esc(v.lesson or kind)))
        self.tg.send("\n\n".join(parts), html=True)

    def apply_pending(self):
        """Уроки, принятые владельцем (/approve), уходят в инструкции модели-аналитика."""
        msgs = []
        for l in self.j.kv_get("pending_lessons", []) or []:
            self.j.add_lesson(l["text"], "postmortem")
            msgs.append("урок принят: " + l["text"])
        if msgs:
            self.j.kv_set("pending_lessons", [])
        return msgs
