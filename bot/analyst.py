"""Аналитик на Claude: классификация новостей, второе мнение по сетапам, учёт расходов и дневной бюджет."""
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import List, Literal

import anthropic

from .config import DATA
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
    crowd_reaction: Literal["panic", "euphoria", "sell_the_news", "capitulation_done", "neutral"]
    contrarian_risk: float   # 0..1: вероятность, что толпа отреагирует ПРОТИВ очевидного смысла новости
    expected: str            # чего ждал рынок/консенсус до новости (из текста: "аналитики ожидали", "консенсус"), иначе ""
    surprise: float          # -1..1: факт относительно ожиданий (+ лучше ожиданий, − хуже, 0 совпало или ожиданий нет)
    reasoning: str


class Prefilter(BaseModel):
    market_relevant: bool


class PrefilterBatch(BaseModel):
    relevant_ids: list[int]   # номера новостей из списка, которые могут двинуть цену


class SetupVerdict(BaseModel):
    agree: bool
    direction: Literal["long", "short", "none"]
    confidence: float        # 0..1
    key_risk: str
    reasoning: str


SYSTEM_NEWS_MOEX = """Ты аналитик российского фондового рынка внутри автономного торгового бота на Московской бирже
(акции и фьючерсы в рублях, сделки на часы и дни). Задача: разобрать новость и оценить её торговое значение. Будь скептичен:
- большинство новостей не двигают рынок; importance >= 7 только для событий уровня решения ЦБ по ставке или резкой смены
  его риторики, крупных санкций или их снятия, реального прогресса или срыва переговоров, отчёта или дивидендов компании
  с сильным расхождением с ожиданиями, сделки M&A, допэмиссии, выкупа, решения суда или регулятора по конкретной компании,
  аварии на производстве, резкого хода нефти или рубля;
- если новость пересказывает уже известное или ожидаемое событие, ставь already_priced = true;
- если это слух, скриншот, неподтверждённое сообщение или источник ненадёжен, ставь is_rumor = true и снижай confidence;
- affected_symbols: только тикеры Мосбиржи (SBER, GAZP, LKOH, ROSN, NVTK, GMKN, YDEX, TATN, VTBR, MGNT, PLZL, CHMF, NLMK,
  MTSS, ALRS, MOEX, OZON, T, AFLT, SNGS, X5, RUAL, PIKK, SMLT, NMTP, FLOT, IRAO, FEES, HYDR, UPRO, PHOR и другие),
  которые реально затронуты, не больше четырёх. Не выдумывай тикеры и не перечисляй весь сектор ради полноты;
- market_wide = true для событий на весь рынок: ставка и риторика ЦБ, инфляция, бюджет, санкции против страны, геополитика,
  курс рубля, нефть. Такие новости не называй по бумагам, ставь флаг;
- типы: решение ЦБ, инфляция, бюджет, нефть, рубль = macro; санкции, переговоры, законы, решения регуляторов = regulation;
  отчёты, дивиденды, прогнозы менеджмента = earnings; сделки и партнёрства = partnership; суд = legal; авария = outage;
  крупные сделки на бирже = whale; остальное = other;
- horizon_hours: 2-6 для импульсных событий, 12-48 для фундаментальных;
- учитывай историческую статистику реакции на такие типы событий, если она передана, но помни, что это медианы
  и каждый случай отличается;
- expected / surprise: если в тексте или контексте есть ожидания (консенсус, прогноз, «ожидали»), сравни факт с ними:
  surprise от -1 (намного хуже ожиданий) до +1 (намного лучше), 0 если совпало или ожиданий нет. Сами ожидания — НЕ причина
  для входа, причина — расхождение факта с ними.
Отвечай строго по схеме, reasoning — одна-две фразы по делу."""

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
- посты политиков и бизнесменов (Трамп, Маск, Белый дом): relevant только если пост прямо касается крипты, тарифов,
  торговых войн, ФРС, налогов, санкций, конкретных компаний или монет. Шутки, политика без рынка, мемы = relevant false;
Психология участников (обязательно учитывай):
- «покупай слух, продавай новость»: ожидаемое позитивное событие (одобрение, листинг, халвинг, отчёт в рамках
  ожиданий) часто вызывает фиксацию прибыли и падение. Если событие ждали и цена к нему росла, ставь
  crowd_reaction = sell_the_news и contrarian_risk 0.5-0.8;
- паника и капитуляция: на страшной новости толпа продаёт сразу и с перебором, через часы часть движения
  выкупается. Если событие известно уже несколько часов и цена сильно упала, ставь capitulation_done;
- эйфория: на фоне жадности и роста хорошая новость даёт короткий всплеск и откат (euphoria);
- отчёт или данные «лучше ожиданий» при перегретом рынке могут продаваться, «хуже ожиданий» при перепроданном
  выкупаться. Смотри на настроение и ход цены, а не только на знак новости;
- contrarian_risk: честная оценка, что рынок пойдёт против очевидного прочтения новости. 0.1-0.2 для
  неожиданных сильных событий, 0.5+ для ожидаемых и уже отыгранных;
- ОЖИДАНИЯ: рынок двигает не сама новость, а её отличие от ожиданий. Ожидания бери из текста (консенсус-прогноз,
  «аналитики ожидали», «рынок закладывал», прогноз компании, прошлый квартал как база) И из контекста ниже
  («Ожидания рынка», «Розница на форумах… ждут») — то, что толпа и аналитики заранее ждали и под что уже встали.
  Заполняй expected (коротко, чего ждали) и surprise: +0.5..+1 факт заметно лучше ожиданий, −0.5..−1 заметно хуже,
  около 0 — совпало или ожиданий нет. ВАЖНО: сами ожидания (оптимизм толпы, «все ждут роста») — НЕ причина для
  входа и не повышают importance; причина — расхождение факта с ними. Если факт совпал с ожиданиями, событие в
  основном уже в цене: already_priced или низкая importance. Если факт расходится, direction задаётся знаком
  surprise, даже если заголовок звучит иначе («прибыль выросла на 10%» при ожидании +30% = bearish; «ставка 14%»
  когда толпа ждала 13.5% и стояла в лонге = bearish, толпе придётся разгружаться);
- reasoning: 1-2 предложения на русском, включая психологический довод, если он есть."""

SYSTEM_SETUP_MOEX = """Ты опытный трейдер-риск-менеджер на Московской бирже. Бот собрал сетап с высокой уверенностью и хочет
войти увеличенным размером. Твоя роль: искать причины НЕ входить. Учитывай: режим рынка (индекс выше/ниже 20-дневной
средней, ширина рынка), перегретость (RSI, растяжение от средних), свежесть новостей (не отыграно ли уже, не слух ли),
ликвидность бумаги, близость уровней, запланированные события в ближайшие часы (решение ЦБ по ставке, недельная
инфляция, дивидендные отсечки, отчёты). Психология: кто ещё не в позиции и будет покупать? Если толпа уже в лонге,
лонг слаб даже на хорошей новости. Если цена не растёт на хорошей новости, это сигнал слабости. Если на плохой новости
продажи иссякли и цена держится, шорт опасен. «Покупай слух, продавай факт» для ожидаемых событий. Соглашайся только
при отсутствии весомых контраргументов. confidence: твоя оценка вероятности, что сделка дойдёт до первого тейка раньше
стопа. reasoning и key_risk на русском, коротко."""

SYSTEM_SETUP = """Ты опытный трейдер-риск-менеджер. Бот собрал сетап с высокой уверенностью и хочет войти увеличенным
размером. Твоя роль: искать причины НЕ входить. Учитывай: режим рынка, перегретость (RSI, ставка финансирования,
открытый интерес, доля лонгов, индекс страха и жадности), свежесть новостей (не отыграно ли уже, не слух ли),
ликвидность, близость уровней, запланированные события (CPI, FOMC, разблокировки) в ближайшие часы.
Психология: кто ещё не в позиции и будет покупать? Если толпа уже в лонге и жадна, лонг слаб даже на хорошей
новости. Если цена не растёт на хорошей новости, это сигнал слабости. Если на плохой новости продажи иссякли
и цена держится, шорт опасен. «Покупай слух, продавай новость» для ожидаемых событий. Соглашайся только если не нашёл серьёзных
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
        cost += (getattr(usage, "cache_creation_input_tokens", 0) or 0) * p_in * 1.25 / 1e6
        with self.lock:
            cur = self.j.kv_get(self._key(), {"usd": 0.0, "calls": 0})
            cur["usd"] += cost
            cur["calls"] += 1
            self.j.kv_set(self._key(), cur)

    def today(self):
        return self.j.kv_get(self._key(), {"usd": 0.0, "calls": 0})

    def total(self):
        """Расход за всё время по суточным записям."""
        total = 0.0
        try:
            for r in self.j._run("SELECT k, v FROM kv WHERE k LIKE ?", ("llm_cost_%",), "all"):
                try:
                    total += float(json.loads(r["v"]).get("usd", 0))
                except (ValueError, TypeError, AttributeError):
                    pass
        except Exception:  # noqa: BLE001
            pass
        return round(total, 2)

    def month(self):
        """Расход за календарный месяц (UTC) по всем суточным записям."""
        pref = "llm_cost_" + datetime.now(timezone.utc).strftime("%Y-%m")
        total = 0.0
        try:
            for r in self.j._run("SELECT k, v FROM kv WHERE k LIKE ?", (pref + "%",), "all"):
                try:
                    total += float(json.loads(r["v"]).get("usd", 0))
                except (ValueError, TypeError, AttributeError):
                    pass
        except Exception:  # noqa: BLE001
            pass
        return round(total, 2)


class Analyst:
    def __init__(self, settings, journal, playbook=None, calendar=None, whales=None, mood=None):
        self.s, self.j = settings, journal
        self.playbook, self.calendar, self.whales, self.mood = playbook, calendar, whales, mood
        self.client = anthropic.Anthropic(max_retries=3, timeout=90.0)
        self.model = settings.get("llm.model")
        self.cheap = settings.get("llm.cheap_model")
        self.budget = float(settings.get("llm.daily_budget_usd") or 0)
        self.market_notes = settings.get("llm.market_notes") or ""
        self.cost = CostTracker(journal)
        self._budget_warned = False

    def spend_tier(self):
        """obычный / экономный / минимальный по расходу за сутки: не выключаем, а тратим меньше."""
        usd = self.cost.today()["usd"]
        soft = float(self.s.get("llm.budget_soft_usd") or self.budget or 0)
        hard = float(self.s.get("llm.budget_hard_usd") or 0)
        if hard and usd >= hard:
            return "minimal"
        if soft and usd >= soft:
            return "economy"
        mcap = float(self.s.get("llm.monthly_budget_usd") or 0)
        if mcap and self.cost.month() >= 0.8 * mcap:
            return "economy"
        return "normal"

    def over_budget(self):
        return bool(self.budget) and self.cost.today()["usd"] >= self.budget

    def _parse(self, model, system, user, schema, max_tokens=8000, effort="medium"):
        blocks = system if isinstance(system, (list, tuple)) else [system]
        system_blocks = [{"type": "text", "text": b, "cache_control": {"type": "ephemeral"}} for b in blocks if b]
        # Модель сначала думает, потом отвечает: на мышление уходят токены из того же лимита,
        # поэтому лимит с запасом, а глубину мышления задаёт effort.
        resp = self.client.messages.parse(
            model=model, max_tokens=max_tokens, output_config={"effort": effort},
            system=system_blocks,
            messages=[{"role": "user", "content": user}], output_format=schema,
        )
        self.cost.add(model, getattr(resp, "usage", None))
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            log.warning("Модель отказала или не дала структурный ответ: stop_reason=%s", resp.stop_reason)
            return None
        return resp.parsed_output

    def _context(self):
        parts = []
        if self.market_notes:
            parts.append(self.market_notes)
        if self.playbook and self.s.get("signals.use_playbook", True):
            pb = self.playbook.text()
            if pb:
                parts.append(pb)
        if self.calendar:
            parts.append(self.calendar.context_text(96))
        if self.whales:
            w = self.whales.text(24)
            if w:
                parts.append(w)
        if self.mood:
            m = self.mood.text()
            if m:
                parts.append(m)
        pb = DATA / "moex_playbook.txt"
        if self.s.market == "moex" and pb.exists():
            parts.append(pb.read_text(encoding="utf-8")[:2500])
        if getattr(self, "expectations", None):
            e = self.expectations.text()
            if e:
                parts.append(e)
        if getattr(self, "crowd", None):
            c = self.crowd.text(list(self.crowd.cache.keys()))
            if c:
                parts.append(c)
        return "\n\n".join(parts)

    def _relevance_rule(self):
        if self.s.get("market") == "moex":
            return ("новость может повлиять на цену акций или фьючерсов Московской биржи в ближайшие двое суток: "
                    "ставка и заявления ЦБ, инфляция, бюджет, санкции и их снятие, переговоры и геополитика, нефть и газ, "
                    "курс рубля, отчёты и дивиденды компаний, сделки M&A, допэмиссии, выкупы, решения судов и регуляторов, "
                    "аварии. Реклама, интервью без фактов, мнения, обзоры «что было за день», подборки и дайджесты, "
                    "спорт, происшествия без экономики = не относится.")
        return ("новость может повлиять на цену какой-либо криптовалюты в ближайшие двое суток (взломы, регуляторика, "
                "листинги/делистинги, макро, крупные сделки и переводы, ETF, разблокировки, сбои, банкротства). "
                "Реклама, интервью, мнения, обзоры, конкурсы биржи, рекламные акции = не относится.")

    def prefilter_batch(self, items):
        """Один вызов дешёвой модели на пачку новостей: список номеров тех, что могут двинуть цену.
        При ошибке пропускаем все к основной модели (как и одиночный фильтр)."""
        if not items:
            return []
        if len(items) == 1:
            return [self.prefilter(items[0]["title"], items[0].get("summary") or "")]
        lines = []
        for i, it in enumerate(items, 1):
            body = " ".join((it.get("summary") or "").split())[:300]
            lines.append(f"{i}. {it['title'][:200]}" + (f" — {body}" if body and body[:60] != it['title'][:60] else ""))
        user = ("Новости:\n" + "\n".join(lines) + "\n\nВерни номера новостей, для которых верно: " + self._relevance_rule()
                + " Если таких нет — пустой список.")
        try:
            r = self._parse(self.cheap, "Ты фильтр новостей для торгового бота. Отвечай строго по схеме.", user,
                            PrefilterBatch, 2000, "low")
            if r is None:
                return [True] * len(items)
            keep = {int(x) for x in r.relevant_ids}
            return [(i + 1) in keep for i in range(len(items))]
        except Exception as e:  # noqa: BLE001
            log.warning("prefilter_batch: %s", str(e)[:200])
            return [True] * len(items)

    def prefilter(self, title, summary):
        """Дешёвый фильтр: имеет ли новость отношение к движению цен."""
        user = (f"Заголовок: {title}\nТекст: {summary[:600]}\n\n"
                "market_relevant = true, если " + self._relevance_rule())
        try:
            r = self._parse(self.cheap, "Ты фильтр новостей для торгового бота. Отвечай строго по схеме.", user,
                            Prefilter, 2000, "low")
            return bool(r and r.market_relevant)
        except Exception as e:  # noqa: BLE001  (ошибки API и разбора ответа: пропускаем к основной модели)
            log.warning("prefilter: %s", str(e)[:200])
            return True

    def classify(self, title, summary, source, lessons_text=""):
        tier = self.spend_tier()
        # Текст новости — короткий, в сообщении пользователя. Всё повторяющееся (заметки о рынке, контекст, уроки) —
        # отдельным системным блоком с кешем: оплачивается полностью раз в несколько минут, дальше по 10% цены.
        # Раньше контекст (3-6 тыс. символов) шёл в каждом вызове без кеша — это и было $6 в день
        max_chars = int(self.s.get("llm.article_max_chars_economy", 1200) if tier != "normal" else self.s.get("llm.article_max_chars", 2500))
        user = f"Источник: {source}\nЗаголовок: {title}\nТекст: {(summary or '')[:max_chars]}\n"
        ctx_parts = []
        ctx = self._context()   # заметки о рынке уже внутри
        if ctx:
            ctx_parts.append(ctx)
        if lessons_text:
            ctx_parts.append("Уроки из прошлых сделок:\n" + lessons_text)
        system = [SYSTEM_NEWS_MOEX if self.s.get("market") == "moex" else SYSTEM_NEWS, "Контекст рынка:\n\n" + "\n\n".join(ctx_parts)]
        fast = {f"tg:{c}" for c in (self.s.get("sources.telegram_fast") or [])} | {"disclosure"}
        cheap_src = set(self.s.get("llm.cheap_sources") or [])
        if tier == "minimal":
            model = self.cheap                                   # всё дешёвой, но разбор не останавливается
        elif tier == "economy":
            model = self.model if source in fast else self.cheap  # сильная только для быстрых каналов и раскрытия
        else:
            # обычный: сильная для быстрых каналов, раскрытия, лент агентств и прочих RSS; дешёвая для медленных
            # Telegram-каналов и второстепенных источников из cheap_sources
            slow_tg = source.startswith("tg:") and source not in fast
            model = self.cheap if (slow_tg or source in cheap_src) else self.model
        effort = "low" if model == self.cheap else "medium"
        try:
            ev = self._parse(model, system, user, NewsEvent, 8000, effort)
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
        d["contrarian_risk"] = max(0.0, min(1.0, float(d.get("contrarian_risk") or 0.0)))
        d["surprise"] = max(-1.0, min(1.0, float(d.get("surprise") or 0.0)))
        d["expected"] = (d.get("expected") or "")[:200]
        if d["expected"] and abs(d["surprise"]) >= 0.5 and not d["is_rumor"]:   # слух расхождением не считаем
            # новость расходится с ожиданиями: торгуем расхождение, а не заголовок; это сильное событие
            want = "bullish" if d["surprise"] > 0 else "bearish"
            if d["direction"] != want:
                d["reasoning"] = f"Расхождение с ожиданиями ({d['expected']}): {d['reasoning']}"
            d["direction"] = want
            d["confidence"] = max(d["confidence"], 0.75)
            d["importance"] = max(d["importance"], 7)
            d["already_priced"] = False
            d["divergence"] = True
        elif d["expected"] and abs(d["surprise"]) < 0.15 and d["event_type"] in ("earnings", "macro"):
            d["confidence"] *= 0.7   # совпало с ожиданиями: движение в основном уже в цене
        if d["crowd_reaction"] == "sell_the_news" and d["direction"] == "bullish":
            d["confidence"] *= 0.6
        if d["crowd_reaction"] == "capitulation_done" and d["direction"] == "bearish":
            d["confidence"] *= 0.6
        return d

    def judge_setup(self, symbol, direction, ta, ticker, events, lessons_text=""):
        if self.over_budget():
            return None
        keys = ("close", "regime", "trend_d", "rsi_d", "breakout_d", "trend_1h", "trend_4h", "timing_15m", "breakout", "rsi", "adx", "atr_pct",
                "vol_ratio", "ret_24h", "ret_4h", "macd_sign", "bb_pct_b", "bb_squeeze", "rsi_divergence",
                "res_dist_atr", "sup_dist_atr")
        payload = {
            "symbol": symbol, "proposed_direction": direction,
            "ta": {k: ta.get(k) for k in keys},
            "positioning": ({"pct_24h": ticker["pct24h"], "turnover_24h": ticker["turnover24h"]} if self.s.get("market") == "moex"
                            else {"funding_rate_8h": ticker["funding"], "oi_value_usd": ticker["oi_value"],
                                  "pct_24h": ticker["pct24h"], "turnover_24h_usd": ticker["turnover24h"]}),
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
            v = self._parse(self.model, SYSTEM_SETUP_MOEX if self.s.get("market") == "moex" else SYSTEM_SETUP, user, SetupVerdict, 12000, "high")
        except anthropic.APIError as e:
            log.error("judge_setup: %s", e)
            return None
        return v.model_dump() if v else None
