"""Сценарий запланированного события: решение ЦБ РФ по ключевой ставке.
1. За 20 минут до решения закрываем позиции и отменяем ордера (через событие не держим), новые входы
   запрещены до +2 минут после.
2. Решение ловим регулярным выражением в сырых новостях (Telegram опрашивается каждые 20 с) — без модели,
   за секунды. Сравниваем с консенсусом (память ожиданий или moex.cbr_consensus в config).
3. Факт мягче консенсуса (ставка ниже ожиданий) → лонг индекса и Сбера по рынку; жёстче → шорт.
   Совпало с консенсусом → сделки нет: движение уже в цене, розница отыграет и откатит.
Сделка одна на событие, размер medium, выходы стандартные (защита прибыли, трейлинг)."""
import logging
import re
import time
from datetime import datetime, timezone

from . import fmt
from .signals import Signal

log = logging.getLogger("eventplay")
RATE_RE = re.compile(r"(ключев\w+\s+ставк\w*|ставк\w+\s+(?:на|до)\s+уровн\w*|ставк\w*)[^%\d]{0,60}?(\d{1,2}(?:[.,]\d{1,2})?)\s*%", re.I)
DECISION_RE = re.compile(r"\b(сохранил[аи]?|оставил[аи]?|повысил[аи]?|снизил[аи]?|понизил[аи]?|установил[аи]?|"
                         r"(?:принял[аи]?\s+решение|решил[аи]?)\s+(?:сохранить|оставить|повысить|снизить|понизить|установить))\b", re.I)
FORECAST_RE = re.compile(r"(ожида|прогноз|аналитик|консенсус|может|вероятн|должен|будет|сможет|рынок закладыва)", re.I)
PAST_RE = re.compile(r"\b(сохранил|оставил|повысил|снизил|понизил|установил|принял[аи]? решение)", re.I)
CBR_RE = re.compile(r"(банк россии|цб\b|цб рф|центробанк|регулятор|совет директоров)", re.I)
NUM_RE = re.compile(r"(\d{1,2}(?:[.,]\d{1,2})?)\s*%")


class EventPlay:
    def __init__(self, bot):
        self.b = bot
        self.flat_done = set()
        self.block_until = 0.0
        self.traded = set()
        self.symbols = (bot.s.get("moex.event_symbols") or ["IMOEXF", "SBERF"])

    # ---------- до события ----------
    def _cbr_events(self, now, ahead_sec=25 * 60):
        return [e for e in self.b.cal.upcoming(1, now) if e.get("impact") == "high" and "ЦБ" in (e.get("title") or "")
                and e["ts"] - now <= ahead_sec]

    def tick(self, now, equity, positions):
        """Вызывается каждый цикл. Возвращает True, если новые входы сейчас запрещены."""
        for e in self._cbr_events(now):
            key = int(e["ts"])
            self.block_until = max(self.block_until, e["ts"] + 2 * 60)
            if key not in self.flat_done:
                self.flat_done.add(key)
                n = self.b.exe.close_all("за 20 минут до решения ЦБ: через событие позиции не держим")
                self.b.tg.send("\n\n".join([f"🏦 <b>РЕШЕНИЕ ЦБ ЧЕРЕЗ {int((e['ts'] - now) / 60)} МИН</b>",
                                             fmt.block("Сделано", f"Закрыл позиции и ордера ({n}), новых входов не будет до решения"),
                                             fmt.block("Консенсус", fmt.esc(self._consensus_text()))]), html=True)
        return now < self.block_until

    def blocked(self, now=None):
        return (now or time.time()) < self.block_until

    # ---------- консенсус ----------
    def consensus(self):
        cfg = self.b.s.get("moex.cbr_consensus")
        if cfg:
            return float(cfg), "config"
        if self.b.expect:
            for it in self.b.expect.items:
                text = f"{it.get('event', '')} {it.get('expected', '')}"
                if re.search(r"ставк", text, re.I):
                    m = NUM_RE.search(it.get("expected", "") or "")
                    if m:
                        return float(m.group(1).replace(",", ".")), it.get("source", "ожидания")
        return None, ""

    def _consensus_text(self):
        c, src = self.consensus()
        return f"{c:g}% ({src})" if c is not None else "неизвестен"

    # ---------- само решение ----------
    def flash(self, items, now=None):
        """Сырые новости до модели: ищем решение ЦБ по ставке. True, если событие отработано."""
        now = now or time.time()
        if not any(abs(e["ts"] - now) <= 90 * 60 for e in self.b.cal.events if e.get("impact") == "high" and "ЦБ" in (e.get("title") or "")):
            return False
        day = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        if day in self.traded:
            return False
        for it in items:
            text = f"{it.get('title', '')} {it.get('summary', '')}"
            if not (CBR_RE.search(text) and DECISION_RE.search(text)):
                continue
            head = text[:220]
            if FORECAST_RE.search(head) and not PAST_RE.search(head):
                continue   # прогноз или ожидание, а не решение
            m = RATE_RE.search(text)
            if not m:
                continue
            actual = float(m.group(2).replace(",", "."))
            if not 3 <= actual <= 30:
                continue
            self.traded.add(day)
            self.block_until = 0.0
            cons, src = self.consensus()
            log.info("Решение ЦБ: %.2f%% (консенсус %s) — %s", actual, cons, it.get("title", "")[:80])
            if cons is None:
                self.b.tg.send("\n\n".join([f"🏦 <b>РЕШЕНИЕ ЦБ: {actual:g}%</b>", fmt.block("Сделка", "нет: консенсус не известен"),
                                             fmt.block("Источник", fmt.esc(it.get('title', '')[:200]))]), html=True)
                return True
            surprise = cons - actual   # + = мягче ожиданий (бычье), − = жёстче (медвежье)
            if abs(surprise) < 0.25:
                self.b.tg.send("\n\n".join([f"🏦 <b>РЕШЕНИЕ ЦБ: {actual:g}%</b>", fmt.block("Консенсус", f"{cons:g}% — совпало"),
                                             fmt.block("Сделка", "нет: движение уже в цене"), fmt.block("Источник", fmt.esc(it.get('title', '')[:200]))]), html=True)
                return True
            direction = "long" if surprise > 0 else "short"
            reason = f"решение ЦБ {actual:g}% против консенсуса {cons:g}% ({'мягче' if surprise > 0 else 'жёстче'} на {abs(surprise):g} п.п.)"
            self.b.tg.send("\n\n".join([f"🏦 <b>РЕШЕНИЕ ЦБ: {actual:g}%</b>",
                                         fmt.block("Консенсус", f"{cons:g}% — факт {'МЯГЧЕ' if surprise > 0 else 'ЖЁСТЧЕ'} ожиданий на {abs(surprise):g} п.п."),
                                         fmt.block("Сделка", f"Вхожу {'в лонг' if direction == 'long' else 'в шорт'} по рынку: {', '.join(self.symbols)}"),
                                         fmt.block("Источник", fmt.esc(it.get('title', '')[:200]))]), html=True)
            equity, positions, _ = self.b.last_known
            for sym in self.symbols:
                try:
                    ta, tk, oi = self.b.market_data(sym, max_age=0)
                    if not ta:
                        continue
                    sig = Signal(symbol=sym, direction=direction, strength=0.9, tier="medium",
                                 components={"news": 0.9 if direction == "long" else -0.9, "ta": 0.0, "positioning": 0.0, "total": 0.9},
                                 reasons=[reason], regime=ta.get("regime", ""), news_driven=True, events=[], event_key=f"cbr-{day}")
                    sig.force_market = True
                    tid = self.b.exe.open(sig, ta, equity, positions)
                    log.info("Сделка по решению ЦБ %s %s -> %s", sym, direction, tid)
                except Exception as ex:  # noqa: BLE001
                    log.error("вход по решению ЦБ %s: %s", sym, ex)
            return True
        return False
