"""Аналитик на Claude: классификация новостей, второе мнение по сетапам, учёт расходов и дневной бюджет."""
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import List, Literal

import anthropic
from pydantic import BaseModel

log = logging.getLogger("analyst")

# USD за 1M токенов (вход, выход). Источник: прайс Anthropic на октябрь 2026.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50),
    "claude-opus-5": (5.0, 25.0), "claude-sonnet-5": (2.0, 10.0),
}

EventType = Literal["listing", "delisting", "hack", "collapse", "regulation", "macro", "etf", "partnership", "whale",
                    "upgrade", "outage", "legal", "token_unlock", "earnings", "other"]


class NewsEvent(BaseModel):
    relevant: bool
    event_type: EventType
    importance: int          # 0..10
    direction: Literal["bullish", "bearish", "neutral"]
    confidence: float        # 0..1
    affected_symbols: List[str]   # базовые монеты: BTC, ETH, SOL ...
    market_wide: bool
    horizon_hours: int       # на каком горизонте ожидается отработка
    already_priced: bool     # новость старая/ожидаемая, рынок уже отыграл
    is_rumor: bool           # слух, не подтверждено первоисточником
    reasoning: str


class Prefilter(BaseModel):
    market_relevant: bool


class SetupVerdict(BaseModel):
    agree: bool
    direction: Literal["long", "short", "none"]
    confidence: float        # 0..1
    key_risk: str
    reasoning: str


SYSTEM_NEWS = """Ты аналитик крипторынка внутри автономного торгового бота на бирже Bybit (USDT-перпетуалы, сделки на часы).
Задача: разобрать новость и оценить её торговое значение. Будь скептичен:
- большинство новостей не двигают рынок; importance >= 7 только для событий уровня взлома крупной биржи/протокола,
  решения регулятора или суда, одобрения/отказа ETF, крупного листинга на Binance/Coinbase/Bybit, делистинга,
  макро-сюрприза (CPI, ФРС, тарифы), краха крупного игрока, разблокирования >5% предложения токена;
- если новость пересказывает уже известное или ожидаемое событие, ставь already_priced = true;
- если это слух, скриншот, неподтверждённое сообщение или источник ненадёжен, ставь is_rumor = true и снижай confidence;
- affected_symbols: только базовые тикеры монет (BTC, ETH, SOL, ...), которые реально затронуты. Не выдумывай тикеры;
- market_wide = true только для событий на весь рынок (макро, регуляторика США, крупные взломы, ETF на BTC/ETH);
- horizon_hours: 2-6 для импульсных событий, 12-48 для фундаментальных;
- учитывай историческую статистику реакции на такие типы событий, если она передана, но помни, что это медианы по небольшой выборке;
- reasoning: 1-2 предложения на русском."""

SYSTEM_SETUP = """Ты опытный трейдер-риск-менеджер. Бот собрал сетап с высокой уверенностью и хочет войти увеличенным
размером. Твоя роль: искать причины НЕ входить. Учитывай: режим рынка, перегретость (RSI, ставка финансирования,
открытый интерес), свежесть новостей (не отыграно ли уже, не слух ли), ликвидность, близость уровней,
запланированные события (CPI, FOMC, разблокировки) в ближайшие часы. Соглашайся только если не нашёл серьёзных
контраргументов. confidence: твоя оценка вероятности, что сделка дойдёт до первого тейка раньше стопа.
reasoning и key_risk на русском, коротко."""


class CostTracker:
    """Считает расходы на модель за сутки (UTC). Сохраняется в журнале."""

    def __init__(self, journal):
        self.j = journal
        self.lock = threading.Lock()

    def _key(self):
        return "llm_cost_" + datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def add(self, model, usage):
        if not usage:
            return
        p_in, p_out = PRICES.get(model, (5.0, 25.0))
        cost = (getattr(usage, "input_tokens", 0) or 0) * p_in / 1e6 + (getattr(usage, "output_tokens", 0) or 0) * p_out / 1e6
        cost += (getattr(usage, "cache_read_input_tokens", 0) or 0) * p_in * 0.1 / 1e6
        with self.lock:
            cur = self.j.kv_get(self._key(), {"usd": 0.0, "calls": 0})
            cur["usd"] += cost
            cur["calls"] += 1
            self.j.kv_set(self._key(), cur)

    def today(self):
        return self.j.kv_get(self._key(), {"usd": 0.0, "calls": 0})


class Analyst:
    def __init__(self, settings, journal, playbook=None, calendar=None):
        self.s, self.j = settings, journal
        self.playbook, self.calendar = playbook, calendar
        self.client = anthropic.Anthropic(max_retries=3, timeout=90.0)
        self.model = settings.get("llm.model")
        self.cheap = settings.get("llm.cheap_model")
        self.budget = float(settings.get("llm.daily_budget_usd") or 0)
        self.cost = CostTracker(journal)
        self._budget_warned = False

    def _main_model(self):
        """Основная модель, пока укладываемся в дневной бюджет, иначе дешёвая."""
        if self.budget and self.cost.today()["usd"] >= self.budget:
            if not self._budget_warned:
                log.warning("Дневной бюджет модели %.2f$ исчерпан, до конца суток работает %s", self.budget, self.cheap)
                self._budget_warned = True
            return self.cheap
        self._budget_warned = False
        return self.model

    def over_budget(self):
        return bool(self.budget) and self.cost.today()["usd"] >= self.budget

    def _parse(self, model, system, user, schema, max_tokens=1500):
        resp = self.client.messages.parse(
            model=model, max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}], output_format=schema,
        )
        self.cost.add(model, getattr(resp, "usage", None))
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            log.warning("Модель отказала или не дала структурный ответ: stop_reason=%s", resp.stop_reason)
            return None
        return resp.parsed_output

    def _context(self):
        parts = []
        if self.playbook:
            pb = self.playbook.text()
            if pb:
                parts.append(pb)
        if self.calendar:
            parts.append(self.calendar.context_text(96))
        return "\n\n".join(parts)

    def prefilter(self, title, summary):
        """Дешёвый фильтр: имеет ли новость отношение к движению цен."""
        user = (f"Заголовок: {title}\nТекст: {summary[:600]}\n\n"
                "market_relevant = true, если новость может повлиять на цену какой-либо криптовалюты в ближайшие "
                "двое суток (взломы, регуляторика, листинги/делистинги, макро, крупные сделки и переводы, ETF, "
                "разблокировки, сбои, банкротства). Реклама, интервью, мнения, обзоры, конкурсы биржи, "
                "рекламные акции = false.")
        try:
            r = self._parse(self.cheap, "Ты фильтр новостей для торгового бота. Отвечай строго по схеме.", user,
                            Prefilter, 200)
            return bool(r and r.market_relevant)
        except anthropic.APIError as e:
            log.warning("prefilter: %s", e)
            return True

    def classify(self, title, summary, source, lessons_text=""):
        user = f"Источник: {source}\nЗаголовок: {title}\nТекст: {summary}\n"
        ctx = self._context()
        if ctx:
            user += f"\n{ctx}\n"
        if lessons_text:
            user += f"\nУроки из прошлых сделок:\n{lessons_text}\n"
        try:
            ev = self._parse(self._main_model(), SYSTEM_NEWS, user, NewsEvent)
        except anthropic.RateLimitError as e:
            log.warning("classify rate limit: %s", e)
            time.sleep(5)
            return None
        except anthropic.APIStatusError as e:
            log.error("classify API %s: %s", e.status_code, e.message)
            return None
        except anthropic.APIConnectionError as e:
            log.error("classify connection: %s", e)
            return None
        if ev is None:
            return None
        d = ev.model_dump()
        d["importance"] = max(0, min(10, int(d["importance"])))
        d["confidence"] = max(0.0, min(1.0, float(d["confidence"])))
        if d["is_rumor"]:
            d["confidence"] *= 0.6
        d["horizon_hours"] = max(1, min(96, int(d["horizon_hours"])))
        d["affected_symbols"] = [s.upper().strip() for s in d["affected_symbols"] if s.strip()][:10]
        return d

    def judge_setup(self, symbol, direction, ta, ticker, events, lessons_text=""):
        if self.over_budget():
            return None
        keys = ("close", "regime", "trend_1h", "trend_4h", "timing_15m", "breakout", "rsi", "adx", "atr_pct",
                "vol_ratio", "ret_24h", "ret_4h", "macd_sign", "bb_pct_b", "bb_squeeze", "rsi_divergence",
                "res_dist_atr", "sup_dist_atr")
        payload = {
            "symbol": symbol, "proposed_direction": direction,
            "ta": {k: ta.get(k) for k in keys},
            "positioning": {"funding_rate_8h": ticker["funding"], "oi_value_usd": ticker["oi_value"],
                            "pct_24h": ticker["pct24h"], "turnover_24h_usd": ticker["turnover24h"]},
            "recent_events": [{"type": e["event_type"], "importance": e["importance"], "direction": e["direction"],
                               "age_hours": e.get("age_h"), "title": e["title"]} for e in events[:8]],
        }
        user = "Сетап:\n" + json.dumps(payload, ensure_ascii=False, indent=1)
        ctx = self._context()
        if ctx:
            user += f"\n\n{ctx}"
        if lessons_text:
            user += f"\n\nУроки из прошлых сделок:\n{lessons_text}"
        try:
            v = self._parse(self.model, SYSTEM_SETUP, user, SetupVerdict)
        except anthropic.APIError as e:
            log.error("judge_setup: %s", e)
            return None
        return v.model_dump() if v else None
