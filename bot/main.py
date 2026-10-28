"""Главный цикл бота.
Поток новостей: опрос источников -> фильтр -> классификация -> очередь событий.
Главный поток: события из очереди (закрыть контр-позиции, сразу оценить затронутые монеты) ->
сопровождение позиций -> периодический скан рынка -> отчёты -> самообучение."""
import logging
import os
import itertools
import queue
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import Counter
import sys
import threading
import time
import traceback
from logging.handlers import RotatingFileHandler

from .analyst import Analyst
from .calendar import Calendar
from .config import LOGS, MARKET_FILE, Settings, ROOT
from .exchange import Exchange
from .moex import MoexExchange
from . import article, fmt
from .executor import Executor
from .journal import Journal
from .macro import MarketMood
from .news import NewsCollector
from .notify import Telegram
from .playbook import Playbook
from .review import Reviewer
from .risk import RiskManager
from .analytics import Analytics
from .crowd import Crowd
from .disclosure import Disclosure
from .eventplay import EventPlay
from .expectations import ExpectationsMemory
from .shadow import ShadowBook
from .tg_live import TelegramLive
from .driver import DriverCheck
from .factors import Factors
from .divcal import DivCalendar
from .signals import build_signal
from .ta import snapshot
from .whales import WhaleFlows, parse as parse_whale
from .xstream import XActivityStream


def setup_logging():
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(LOGS / "bot.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)
    for noisy in ("pybit", "httpx", "httpx2", "urllib3", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


log = logging.getLogger("main")


class Bot:
    def __init__(self, dry_run=False):
        self.s = Settings()
        self.dry = dry_run
        self.market = self.s.market
        fmt.CUR = self.s.get("currency", "USDT")
        self.ex = MoexExchange(self.s) if self.market == "moex" else Exchange(self.s)
        self.j = Journal(self.s.db_path)
        self.tg = Telegram(self.s.telegram_token, self.s.telegram_chat_id)
        self.cal = Calendar(settings=self.s)
        self.playbook = Playbook()
        self.risk = RiskManager(self.s, self.ex, self.j, self.cal)
        self.exe = Executor(self.s, self.ex, self.risk, self.j, self.tg)
        self.whales = WhaleFlows(self.j)
        self.shadow = ShadowBook(self.s, self.j, self.ex)
        self.analytics = Analytics(self.j, self.s)
        fmt.NAMES = self._names()
        self.exe.shadow = self.shadow
        self.mood = MarketMood()
        self.analyst = (Analyst(self.s, self.j, self.playbook, self.cal, self.whales, self.mood)
                        if self.s.has_anthropic else None)
        self.factors = Factors(self.s, self.ex) if hasattr(self.ex, "candles_uid") and self.s.get("signals.factor_map") else None
        self.divcal = DivCalendar(self.s, self.ex) if hasattr(self.ex, "candles_uid") and self.s.get("risk.div_block_days") else None
        self.exe.driver = (DriverCheck(self.s, self.analyst, self.j, self.ex)
                           if self.analyst and (self.s.get("risk.driver_check") or {}).get("mode") else None)
        self.reviewer = Reviewer(self.s, self.j, self.tg) if self.s.has_anthropic else None
        if self.reviewer:
            self.reviewer.analytics = self.analytics
        if self.reviewer:
            self.reviewer.shadow = self.shadow   # разбор учитывает и теневые сделки
        self.news = NewsCollector(self.ex, self.j, self.s)
        self.seen_titles = {}
        self.paper_mode = bool(self.j.kv_get("paper_mode", False))
        self.last_known, self.last_known_ts, self.broker_down_since = (0.0, [], {"wallet": 0.0}), time.time(), None   # до первого ответа биржи
        self.expect = self.crowd = self.eventplay = None
        if self.market == "moex":
            self.eventplay = EventPlay(self)
            self.news.disclosure = Disclosure(self.s, self.ex)
            if self.analyst:
                self.expect = ExpectationsMemory(self.s, self.j, self.analyst, self.cal)
                self.crowd = Crowd(self.s, self.analyst, self.j)
                self.analyst.expectations, self.analyst.crowd = self.expect, self.crowd
        self.events_q = queue.Queue()
        self.analyze_q = queue.PriorityQueue()   # новости на разбор моделью: (приоритет, номер, новость)
        self._seq = itertools.count()
        self.whale_lock = threading.Lock()
        self._lessons_cache = (0.0, "")
        self.raw_q = queue.Queue()          # посты из потока X, минуя опрос
        self.news_wake = threading.Event()
        # живой Telegram (Telethon) — только если заданы TELEGRAM_API_ID/HASH/SESSION; без них выключен, работает ярусный опрос
        self.tg_live = TelegramLive(self.s.get("sources.telegram_channels") or [], self.raw_q, self.news_wake, self.s)
        self.news.live = self.tg_live
        self.stop_flag = threading.Event()
        self.xstream = None
        if self.s.get("sources.twitter_mode", "stream") == "stream":
            self.xstream = XActivityStream(self.s, self.j, self._on_stream_items,
                                           self.s.get("sources.twitter_accounts") or [])
        self.last = {"scan": 0, "manage": 0, "report": 0, "review": 0}
        self.closed_since_review = 0
        self.err_count, self.last_err_sent = 0, 0.0
        self.funnel_since_report = Counter()   # причины отказа с прошлого отчёта о балансе
        self.best_weak_since_report = None
        self.last_funnel = (Counter(), None)
        self.cache = {}   # symbol -> (ts, ta, ticker, oi, df1h)
        self.kcache = {}  # (symbol, interval) -> (ts, df) для старших таймфреймов

    # ---------- поток новостей ----------
    def _on_stream_items(self, items):
        for it in items:
            self.raw_q.put(it)
        self.news_wake.set()

    def news_loop(self):
        interval = self.s.get("intervals.news_poll_sec", 5)   # ярусы внутри poll(): быстрые каналы 5 с, остальные 30 с
        next_poll = 0.0
        while not self.stop_flag.is_set():
            try:
                fresh = []
                while True:
                    try:
                        it = self.raw_q.get_nowait()
                    except queue.Empty:
                        break
                    if self.j.add_news(it):
                        fresh.append(it)
                if time.time() >= next_poll:
                    next_poll = time.time() + interval
                    fresh += self.news.poll()
                if fresh:
                    if self.eventplay:
                        self.eventplay.flash(fresh)   # решение ЦБ: за секунды, без очереди к модели
                    self.enqueue_news(fresh)
            except Exception as e:  # noqa: BLE001
                log.error("Поток новостей: %s\n%s", e, traceback.format_exc())
            self.news_wake.wait(timeout=max(0.5, next_poll - time.time()))
            self.news_wake.clear()

    def enqueue_news(self, fresh):
        """Сборщик только раскладывает новости по очереди с приоритетом; разбирают их рабочие потоки.
        Раньше сбор и разбор шли в одном потоке по очереди: в час пик новость ждала модель 2–20 минут."""
        log.info("Новых новостей: %d", len(fresh))
        import re as _re
        fast = {f"tg:{c}" for c in (self.s.get("sources.telegram_fast") or [])} | {"disclosure"}
        fast_rss = set(self.s.get("sources.rss_fast") or [])
        for it in fresh:
            self.j.mark_analyzed(it["id"])
            # один и тот же заголовок из десяти каналов (особенно в день ЦБ): анализируем один раз за 2 часа
            key = _re.sub(r"[^a-zа-яё0-9%]+", " ", (it.get("title") or "").lower()).strip()[:70]
            now_ts = time.time()
            if key and now_ts - self.seen_titles.get(key, 0) < 7200:
                continue
            if key:
                self.seen_titles[key] = now_ts
                if len(self.seen_titles) > 5000:
                    self.seen_titles = {k: v for k, v in self.seen_titles.items() if now_ts - v < 7200}
            src = it["source"]
            prio = 0 if src in fast or src in fast_rss else (1 if src.startswith("tg:") else 2)
            self.analyze_q.put((prio, next(self._seq), it))
        qn = self.analyze_q.qsize()
        if qn > 50:
            log.warning("очередь на разбор: %d новостей", qn)

    def _lessons(self):
        if not self.reviewer:
            return ""
        ts, txt = self._lessons_cache
        if time.time() - ts > 120:
            txt = self.reviewer.lessons_text()
            self._lessons_cache = (time.time(), txt)
        return txt

    def news_worker(self):
        while not self.stop_flag.is_set():
            try:
                _, _, it = self.analyze_q.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._analyze_item(it)
            except Exception as e:  # noqa: BLE001
                log.error("Разбор новости: %s\n%s", e, traceback.format_exc())

    def _analyze_item(self, it):
        whale_srcs = {f"tg:{c}" for c in (self.s.get("sources.whale_channels") or [])}
        ev = None
        if True:
            if it["source"] in whale_srcs:
                ev = parse_whale(it["summary"])
                if ev:
                    if ev["usd"] < self.s.get("sources.whale_min_usd", 50_000_000):
                        return
                    with self.whale_lock:
                        self.whales.add(ev)
                elif not self.analyst:
                    return
            if ev is None:
                if not self.analyst:
                    return
                if not self.analyst.prefilter(it["title"], it["summary"]):
                    return
                body = it["summary"]
                if not it["source"].startswith("tg:") and it["source"] != "disclosure" and self.s.get("news.full_text", True):
                    full = article.fetch_text(it.get("url"))
                    if full:
                        body = full   # статья целиком: направление часто ясно только из тела, а не из заголовка
                ev = self.analyst.classify(it["title"], body, it["source"], self._lessons())
            if not ev or not ev["relevant"]:
                return
            if ev["already_priced"] and self.s.get("signals.drop_already_priced", True):
                return   # на Мосбирже выключено: по архиву «уже в цене» события шли в сторону прогноза не хуже остальных
            self.j.add_event(it["id"], it["published_ts"], ev, it["title"])
            lag = time.time() - it["published_ts"]
            log.info("Событие [%s, imp %d, %s, %s] задержка %.0fс: %s", ev["event_type"], ev["importance"],
                     ev["direction"], ev["affected_symbols"] or "рынок", lag, it["title"][:90])
            rec = {"news_id": it["id"], "ts": it["published_ts"], "event_type": ev["event_type"],
                   "importance": ev["importance"], "direction": ev["direction"], "confidence": ev["confidence"],
                   "affected": ev["affected_symbols"], "market_wide": ev["market_wide"],
                   "horizon_hours": ev["horizon_hours"], "reasoning": ev["reasoning"], "title": it["title"]}
            self.events_q.put(rec)
            if ev["importance"] >= 8:
                d = {"bullish": "позитив", "bearish": "негатив", "neutral": "нейтрально"}[ev["direction"]]
                parts = [f"📰 <b>ВАЖНАЯ НОВОСТЬ</b> — {fmt.RU_TYPE.get(ev['event_type'], 'событие')}, важность {ev['importance']}/10",
                         fmt.block("Новость", fmt.esc(fmt.clean(it['title'], 200))),
                         fmt.block("Оценка", f"{d} для {fmt.esc(', '.join(ev['affected_symbols']) or 'всего рынка')}"
                                   + (f"; ожидали: {fmt.esc(ev['expected'])}, факт {'лучше' if ev['surprise'] > 0 else 'хуже'} ожиданий" if ev.get('expected') and abs(ev.get('surprise') or 0) >= 0.25 else "")),
                         fmt.block("Почему", fmt.esc(ev['reasoning']))]
                self.tg.send("\n\n".join(parts), html=True)

    # ---------- данные ----------
    def _klines_cached(self, symbol, interval, limit, max_age):
        """Старшие таймфреймы меняются медленно: кешируем их дольше, чтобы не упираться в лимит запросов Bybit."""
        key = (symbol, interval)
        c = self.kcache.get(key)
        if c and time.time() - c[0] < max_age:
            return c[1]
        df = self.ex.klines(symbol, interval, limit)
        self.kcache[key] = (time.time(), df)
        return df

    def market_data(self, symbol, max_age=240):
        c = self.cache.get(symbol)
        if c and time.time() - c[0] < max_age:
            return c[1], c[2], c[3]
        df1h = self.ex.klines(symbol, "60", 300)
        df4h = self._klines_cached(symbol, "240", 120, 900)
        df15 = self.ex.klines(symbol, "15", 100)
        dfd = self._klines_cached(symbol, "D", 250, 3600)
        time.sleep(0.15)   # бережём лимит запросов Bybit при скане 40 пар
        # Теханализ только по закрытым свечам: незакрытая свеча «мигает» (пробой внутри часа часто не доживает
        # до закрытия), а проверка на истории велась по закрытым. Текущая цена берётся отдельно.
        now_ms = int(time.time() * 1000)

        def closed(df, ms):
            return df[df["ts"] + ms <= now_ms].reset_index(drop=True) if df is not None and len(df) else df

        ta = snapshot(closed(df1h, 3_600_000), closed(df4h, 14_400_000), df15, closed(dfd, 86_400_000))
        tk = self.ex.ticker(symbol)
        if ta:
            ta["price_now"] = tk["last"] or float(df1h["close"].iloc[-1])
        tk["long_ratio"] = self.ex.long_short_ratio(symbol)
        if hasattr(self.ex, "whale_flow"):   # Мосбиржа: крупные сделки за час и перекос стакана
            wf = self.ex.whale_flow(symbol)
            tk["whale_imb"], tk["whale_n"] = (wf["imbalance"], wf["n_big"]) if wf else (None, 0)
            tk["book_imb"] = self.ex.book_imbalance(symbol)
            if self.crowd:   # толпа на форуме по базовой бумаге (кэш час)
                base = (self.ex.instruments().get(symbol) or {}).get("base") or symbol
                tk["crowd"], tk["crowd_reason"] = self.crowd.score(base)
        oi = self.ex.open_interest(symbol, 24)
        self.cache[symbol] = (time.time(), ta, tk, oi, df1h)
        return ta, tk, oi

    # ---------- оценка и входы ----------
    def evaluate(self, symbols, equity, positions, max_age=240, tag="скан"):
        """Построить сигналы по символам и открыть лучшие. Возвращает число открытых сделок."""
        ok, why = self.risk.check(equity)
        if not ok:
            log.info("Новые входы запрещены: %s", why)
            return 0
        events = self.j.recent_events(72)
        params = self.s.get("signals")
        inst = self.ex.instruments()
        # отложенные ордера считаем как занятые слоты: по символу, направлению, новости и риску
        open_trades = self.j.open_trades() + [
            {"symbol": p["symbol"], "side": p["side"], "entry": p["price"], "stop_init": p["stop"], "qty": p["qty"],
             "qty_init": p["qty"], "event_key": p.get("event_key", ""), "pending": True} for p in self.j.pendings()]
        candidates = []
        funnel, best_weak = Counter(), None
        for sym in symbols:
            if self.tg.commands:   # скан длится минуты: команды из Telegram обрабатываем, не дожидаясь его конца
                self.handle_commands(equity, positions)
            if tag == "скан" and not self.events_q.empty():
                # важная новость важнее планового скана: прерываем его и реагируем сразу (скан повторится через 5 минут)
                log.info("скан прерван: новое событие, реагирую")
                self.handle_events(equity, positions)
                return 0
            if sym not in inst:
                continue
            try:
                ta, tk, oi = self.market_data(sym, max_age)
            except Exception as e:  # noqa: BLE001
                log.debug("%s: данные: %s", sym, e)
                funnel["error"] += 1
                continue
            if not ta:
                funnel["no_data"] += 1
                continue
            df1h = self.cache.get(sym, (0, None, None, None, None))[4]
            dg = {}
            fsc, ftxt = self.factors.score_for(inst[sym]["base"]) if self.factors else (0.0, "")
            sig = build_signal(sym, inst[sym]["base"], ta, tk, oi, events, params, playbook=self.playbook,
                               whale_net=self.whales.net_score(inst[sym]["base"]), mood=self.mood.score(),
                               df1h=df1h, psych_cfg=self.s.get("psychology") or {}, diag=dg, factor=fsc, factor_text=ftxt)
            if sig and self.divcal and self.divcal.blocks(sym, inst):
                log.info("%s: %s", sym, self.divcal.blocks(sym, inst))
                funnel["div_gap"] += 1
                self._shadow_from_signal(sig, ta, "div_gap")
                sig = None
            if sig:
                candidates.append((sig, ta, tk))
            else:
                funnel[dg.get("reason", "no_data")] += 1
                self._shadow_from_diag(sym, dg, ta)
                if dg.get("reason") == "weak" and (best_weak is None or dg["strength"] > best_weak[1]):
                    best_weak = (sym, dg["strength"])
        candidates.sort(key=lambda x: x[0].strength, reverse=True)
        thr = self.s.get("signals.entry_threshold")
        log.info("%s: %d символов, кандидатов %d | %s", tag, len(symbols), len(candidates),
                 fmt.funnel_text(funnel, best_weak, thr))
        self.last_funnel = (funnel, best_weak)
        self.funnel_since_report.update(funnel)
        if best_weak and (self.best_weak_since_report is None or best_weak[1] > self.best_weak_since_report[1]):
            self.best_weak_since_report = best_weak
        opened = 0
        dirs_done = set()   # одновременные сигналы в одну сторону = одна ставка на рынок: берём только сильнейший
        for sig, ta, tk in candidates:
            gate = self._gate(sig, ta)
            if gate:
                log.info("%s %s: %s", sig.symbol, sig.direction, fmt.REASONS_RU.get(gate, gate))
                self._shadow_from_signal(sig, ta, gate)
                continue
            if sig.direction in dirs_done:
                log.info("%s: уже есть вход в эту сторону в этом скане", sig.symbol)
                self._shadow_from_signal(sig, ta, "one_per_scan")
                continue
            can, why = self.risk.can_open(sig.symbol, open_trades, positions, equity, sig.direction, sig.event_key,
                                          sig.tier)
            if not can:
                log.info("%s: %s", sig.symbol, why)
                code = getattr(self.risk, "last_reject", "")
                if code and code != "symbol_busy":
                    self._shadow_from_signal(sig, ta, "limit_" + code)
                continue
            if sig.tier == "conviction":
                if not self.analyst:
                    sig.tier = "high"
                else:
                    verdict = self.analyst.judge_setup(sig.symbol, sig.direction, ta, tk, sig.events,
                                                       self.reviewer.lessons_text() if self.reviewer else "")
                    if not verdict or not verdict["agree"] or verdict["direction"] != sig.direction \
                            or verdict["confidence"] < 0.7:
                        sig.tier = "high"
                        sig.reasons.append("модель не подтвердила повышенный размер"
                                           + (f": {verdict['key_risk']}" if verdict else ""))
                    else:
                        sig.reasons.append(f"модель подтвердила ({verdict['confidence']:.2f}): {verdict['reasoning']}")
            log.info("СИГНАЛ %s %s [%s] сила %.2f: %s", sig.symbol, sig.direction, sig.tier, sig.strength,
                     sig.components)
            if self.dry:
                r = self.s.get("risk")
                dist = ta["atr"] * r["stop_atr_mult"]
                stop = ta["close"] - dist if sig.direction == "long" else ta["close"] + dist
                qty, lev = self.risk.size(sig.symbol, equity, ta["close"], stop, sig.tier, positions)
                if qty is None:
                    log.info("%s: dry-run, размер не рассчитан: %s", sig.symbol, lev)
                    continue
                plan = self.exe._tp_plan(sig.symbol, qty, ta["close"], dist, sig.direction == "long")
                self.tg.send(fmt.entry_message(sig, ta["close"], qty, lev, stop, plan, r["risk_per_trade_pct"][sig.tier],
                                               dist * qty, self.risk.tradable(equity), equity, ta, dry=True), html=True)
                continue
            tid = self.exe.open(sig, ta, equity, positions)
            if tid:
                dirs_done.add(sig.direction)
                opened += 1
                open_trades = self.j.open_trades() + [
                    {"symbol": p["symbol"], "side": p["side"], "entry": p["price"], "stop_init": p["stop"],
                     "qty": p["qty"], "qty_init": p["qty"], "event_key": p.get("event_key", ""), "pending": True}
                    for p in self.j.pendings()]
                positions = self.ex.positions()
                equity, _ = self.ex.equity()
        return opened

    # ---------- теневые сделки ----------
    def _shadow_from_diag(self, sym, dg, ta):
        try:
            thr = self.s.get("signals.entry_threshold")
            reason, strength = dg.get("reason"), dg.get("strength", 0.0)
            if reason and dg.get("direction") and self.shadow.wants(reason, strength, thr):
                self.shadow.record(sym, dg["direction"], reason, strength, thr, ta.get("price_now") or ta["close"],
                                   ta["atr"], regime=ta.get("regime", ""), comps=dg.get("comps"), ext=dg.get("ext", 0.0),
                                   news_driven=dg.get("news_driven", False))
        except Exception as e:  # noqa: BLE001
            log.warning("теневая сделка %s: %s", sym, e)

    def _shadow_from_signal(self, sig, ta, reason):
        try:
            thr = self.s.get("signals.entry_threshold")
            ext = ((1 if sig.direction == "long" else -1) * ta["ret_24h"] / ta["atr_pct"]) if ta.get("atr_pct") else 0.0
            self.shadow.record(sig.symbol, sig.direction, reason, sig.strength, thr, ta.get("price_now") or ta["close"],
                               ta["atr"], regime=ta.get("regime", ""), comps=sig.components, ext=ext,
                               news_driven=sig.news_driven, event_key=sig.event_key)
        except Exception as e:  # noqa: BLE001
            log.warning("теневая сделка %s: %s", sig.symbol, e)

    def session_open(self):
        """Мосбиржа: входы только в часы сессии из настроек (время московское). Крипта круглосуточно."""
        hours = self.s.get("moex.trading_hours") if self.market == "moex" else None
        if not hours:
            return True
        now = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%H:%M")
        return any(a <= now <= b for a, b in hours)

    def step_scan(self, equity, positions):
        if not self.session_open():
            log.info("скан: вне торговых часов, входов нет")
            return
        universe = self.ex.universe()
        inst = self.ex.instruments()
        extra = []
        for e in self.j.recent_events(72):
            if e["importance"] >= 6:
                for base in e["affected"]:
                    syms = [x for x in self.ex.symbols_for_base(base) if x in inst]
                    if not syms or any(x in universe for x in syms):
                        continue
                    # одна бумага на компанию: вечный фьючерс, иначе ближайший квартальный, иначе акция
                    perp = [x for x in syms if inst[x].get("perpetual")]
                    fut = sorted((x for x in syms if inst[x]["kind"] == "future" and inst[x].get("expiration")),
                                 key=lambda x: inst[x]["expiration"])
                    share = [x for x in syms if inst[x]["kind"] == "share"]
                    pick = (perp or fut or share)[:1]
                    if pick and pick[0] not in extra:
                        extra.append(pick[0])
        universe = universe + extra[:5]    # бережём лимиты брокера и внимание: ~12 ликвидных бумаг + до 5 по новостям
        self.evaluate(universe, equity, positions, tag="скан")

    def handle_events(self, equity, positions):
        """События из потока новостей: закрыть контр-позиции и сразу оценить затронутые монеты."""
        if self.eventplay and self.eventplay.blocked():
            while not self.events_q.empty():
                self.events_q.get_nowait()
            return
        new = []
        while True:
            try:
                new.append(self.events_q.get_nowait())
            except queue.Empty:
                break
        if not new:
            return
        r = self.s.get("risk")
        closed = self.exe.react_to_events(new)
        touched = set()
        min_imp = self.s.get("signals.min_news_importance", 5)
        for e in new:
            if e["importance"] < min_imp:
                continue
            for base in e["affected"]:
                touched.update(self.ex.symbols_for_base(base))
            if e["market_wide"] and e["importance"] >= 8:
                touched.update(self.s.get("universe.always") or [])
        for t in closed:
            if r.get("news_flip"):
                touched.add(t["symbol"])
        if touched:
            for sym in touched:
                self.cache.pop(sym, None)
            positions = self.ex.positions()
            equity, _ = self.ex.equity()
            self.evaluate(sorted(touched), equity, positions, max_age=0, tag="реакция на новость")

    # ---------- отчёты и команды ----------
    def step_report(self, equity, positions):
        m = self.risk.metrics(equity)
        llm = None
        if self.analyst:
            c = self.analyst.cost.today()
            llm = {"calls": c["calls"], "usd": c["usd"], "budget": self.analyst.budget}
        self.tg.send(fmt.balance_message(equity, m, positions, self.j.stats(), float(self.s.get("risk.capital_cap_usd") or 0),
                                         llm, self.cal.next_high(), self.xstream.status() if self.xstream else None,
                                         pendings=self.j.pendings(),
                                         shadow_line=self.shadow.summary_text(7, top=3),
                                         why_none=fmt.funnel_text(self.funnel_since_report, self.best_weak_since_report,
                                                                  self.s.get("signals.entry_threshold"))
                                         if self.funnel_since_report else None), html=True)
        self.funnel_since_report, self.best_weak_since_report = Counter(), None

    def handle_commands(self, equity, positions):
        for cmd in self.tg.pop_commands():
            c = cmd.split()[0].lower()
            log.info("Команда: %s", cmd)
            if c in ("/status", "/balance"):
                self.step_report(equity, positions)
            elif c == "/stop":
                self.risk.halt("ручная остановка (/stop)")
                n = self.exe.cancel_pendings("ручная остановка")
                self.tg.send(fmt.block("⛔ Стоп", f"Новые входы остановлены, отложенных ордеров снято: {n}. "
                             "Открытые позиции остаются под стопами. /panic закроет всё."), html=True)
            elif c == "/panic":
                n = self.exe.close_all("ручное закрытие (/panic)")
                self.risk.halt("ручная остановка (/panic)")
                self.tg.send(fmt.block("⛔ Паника", f"Закрыто позиций: {n}. Бот остановлен. /resume чтобы продолжить."), html=True)
            elif c == "/resume":
                self.risk.resume(equity)
                self.tg.send(fmt.block("▶️ Возобновлено", "Торговля возобновлена, отсчёт просадки начат заново"), html=True)
            elif c == "/approve":
                msgs = self.reviewer.apply_pending() if self.reviewer else []
                self.tg.send(fmt.block("Применено", *[fmt.esc(m) for m in msgs]) if msgs else fmt.block("Правки", "нет ожидающих"), html=True)
            elif c == "/lessons":
                self.tg.send(fmt.block("📚 Уроки", fmt.esc(self.reviewer.lessons_text(20)) if self.reviewer else "аналитик выключен"), html=True)
            elif c == "/review":
                if self.reviewer:
                    self.reviewer.run(apply=self._auto_apply())
            elif c == "/calendar":
                parts = [fmt.block("📅 Календарь", fmt.esc(self.cal.context_text(96)))]
                if self.divcal:
                    parts.append(fmt.block("Дивидендные отсечки (30 дн.)", *([fmt.esc(x) for x in self.divcal.text(30)] or ["нет"])))
                if self.factors:
                    parts.append(fmt.block("Факторы", *([fmt.esc(x) for x in self.factors.text()] or ["нет данных"])))
                self.tg.send("\n\n".join(parts), html=True)
            elif c == "/shadow":
                self.tg.send("👻 <b>ТЕНЕВЫЕ СДЕЛКИ</b> (сигналы, которые бот не взял, проверенные по факту)\n\n"
                             + fmt.block("За 7 дней", fmt.esc(self.shadow.summary_text(7, top=10, detailed=True))) + "\n\n"
                             + fmt.block("За 30 дней", fmt.esc(self.shadow.summary_text(30, top=10, detailed=True))), html=True)
            elif c == "/market":
                parts = cmd.split()
                target = parts[1].lower() if len(parts) > 1 else ""
                if target not in ("crypto", "moex"):
                    self.tg.send(fmt.block("Рынок", f"Сейчас: {self.market}. Переключить: /market crypto или /market moex"), html=True)
                elif target == self.market:
                    self.tg.send(f"Уже торгуем {self.market}")
                else:
                    n = self.exe.close_all(f"переключение рынка на {target}")
                    MARKET_FILE.write_text(target, encoding="utf-8")
                    self.tg.send(fmt.block("🔁 Переключение рынка", f"На {target}: закрыто позиций и ордеров {n}, перезапуск через несколько секунд"), html=True)
                    log.info("Переключение рынка на %s, перезапуск", target)
                    time.sleep(2)
                    os._exit(3)   # systemd поднимет процесс заново уже с другим рынком
            elif c == "/why":
                f, bw = self.last_funnel
                self.tg.send("🔎 <b>ПОЧЕМУ НЕТ ВХОДА</b> (последний скан)\n\n"
                             + fmt.block("Отсеяно", fmt.esc(fmt.funnel_text(f, bw, self.s.get("signals.entry_threshold"), top=8))), html=True)
            elif c == "/analytics":
                parts = cmd.split()
                days = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 30
                txt = self.analytics.text(days)
                drv = self.exe.driver.summary_text(days) if self.exe.driver else None
                if drv:
                    txt += "\n\n" + fmt.block("Драйвер движения (тени)", *[fmt.esc(x) for x in drv])
                self.tg.send(txt, html=True)
            elif c == "/log":
                # последние строки журнала: нужны, когда к машине нет доступа по SSH
                try:
                    lines = (LOGS / "bot.log").read_text(encoding="utf-8", errors="replace").splitlines()
                    tail = [l for l in lines if "news: tg" not in l][-25:]
                    self.tg.send(fmt.block("📜 Лог", *[fmt.esc(l[11:200]) for l in tail]), html=True)
                except Exception as e:  # noqa: BLE001
                    self.tg.send(f"Лог недоступен: {e}")
            elif c == "/update":
                # обновление кода из GitHub и перезапуск (цикл run.ps1 / systemd поднимет процесс заново)
                import subprocess
                try:
                    out = subprocess.run(["git", "pull", "--ff-only"], cwd=str(ROOT), capture_output=True, text=True, timeout=120)
                    msg = (out.stdout + out.stderr).strip()[-600:]
                    if out.returncode != 0:
                        self.tg.send(f"Обновление не удалось:\n{msg}")
                    elif "Already up to date" in msg or "Уже обновлено" in msg:
                        self.tg.send("Уже последняя версия, перезапуск не нужен")
                    else:
                        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt")],
                                       cwd=str(ROOT), capture_output=True, timeout=600)
                        n = self.exe.cancel_pendings("обновление")
                        self.tg.send(f"Обновлено:\n{msg}\nОтменено ожидающих ордеров: {n}. Перезапуск через несколько секунд.")
                        log.info("Обновление кода, перезапуск")
                        time.sleep(2)
                        os._exit(3)
                except Exception as e:  # noqa: BLE001
                    self.tg.send(f"Обновление не удалось: {e}")
            elif c == "/help":
                self.tg.send("/status /why /shadow /analytics /market /stop /panic /resume /review /approve /lessons /calendar /log /update")

    def _auto_apply(self):
        return bool(self.s.get("learning.auto_apply")) and self.s.mode != "live"

    # ---------- цикл ----------
    # ---------- ворота перед входом: тренд рынка, свежесть новости, режим наблюдения ----------
    def market_regime(self):
        """True = рынок растёт (индекс выше 20-дневной средней и дневной тренд вверх), False = падает, None = нет данных."""
        c = getattr(self, "_regime_cache", None)
        if c and time.time() - c[0] < 1800:
            return c[1]
        up = None
        for sym in (self.s.get("signals.trend_index") or ["IMOEXF", "MOEX"]):
            try:
                if sym not in self.ex.instruments():
                    continue
                d = self._klines_cached(sym, "D", 60, 3600)
                if d is None or len(d) < 25:
                    continue
                closes = d["close"].astype(float)
                sma20 = closes.tail(20).mean()
                sma5 = closes.tail(5).mean()
                up = bool(closes.iloc[-1] > sma20 and sma5 >= closes.tail(10).mean())
                break
            except Exception as e:  # noqa: BLE001
                log.debug("режим рынка %s: %s", sym, e)
        self._regime_cache = (time.time(), up)
        return up

    def _gate(self, sig, ta):
        """Причина отказа (ключ для теней) или None."""
        p = self.s.get("signals") or {}
        if getattr(self, "paper_mode", False):
            return "paper_mode"
        strong_news = False
        if sig.news_driven and sig.events:
            top = max(sig.events, key=lambda e: e["importance"] * e.get("confidence", 1))
            age_min = (time.time() - top["ts"]) / 60
            if top["importance"] < p.get("news_entry_min_importance", 0) or age_min > p.get("news_entry_max_age_min", 1e9):
                return "stale_news"
            strong_news = True
        if p.get("trend_gate") and not strong_news:
            # ворота по тренду — для технических и слабых сигналов; сильная свежая новость идёт в любую сторону
            up = self.market_regime()
            td = ta.get("trend_d", 0)
            if sig.direction == "long" and (up is False or td < 0):
                return "trend_gate"
            if sig.direction == "short" and up is True and td > 0:
                return "trend_gate"
        elif p.get("trend_gate") and strong_news:
            up = self.market_regime()
            against = (sig.direction == "long" and up is False) or (sig.direction == "short" and up is True)
            if against and sig.tier in ("high", "conviction"):
                sig.tier = "medium"   # новость против тренда рынка: размер не больше среднего
                sig.reasons.append("новость против тренда рынка: размер не больше среднего")
        return None

    def update_paper_mode(self, now=None):
        """Тени за последние дни в минусе → торгуем только тенями, пока статистика не вернётся в плюс."""
        c = self.s.get("shadow") or {}
        if not c.get("paper_mode"):
            return
        now = now or time.time()
        since = max(int(now - c.get("paper_mode_days", 3) * 86400), int(self.j.kv_get("paper_mode_since", 0) or 0))
        rows = self.j.shadows_resolved(since)   # только тени, записанные после включения ворот: старую систему не судим
        vals = [r["r_pullback"] if r["r_pullback"] is not None else r["r_market"] for r in rows]
        vals = [v for v in vals if v is not None]
        if len(vals) < c.get("paper_mode_min_n", 20):
            return
        avg = sum(vals) / len(vals)
        want = avg < c.get("paper_mode_below_r", -0.02)
        if want != getattr(self, "paper_mode", False):
            self.paper_mode = want
            self.j.kv_set("paper_mode", want)
            if want:
                self.tg.send("\n\n".join([
                    "👻 <b>РЕЖИМ НАБЛЮДЕНИЯ</b>",
                    fmt.block("Почему", f"Теневые сделки за {c.get('paper_mode_days', 3)} дн.: {len(vals)} шт., средний {avg:+.2f}R — система сейчас не зарабатывает"),
                    fmt.block("Что делаю", "Новых сделок не открываю, только тени для статистики. Открытые позиции веду как обычно. "
                              "Вернусь к торговле, когда тени выйдут в плюс.")]), html=True)
            else:
                self.tg.send("\n\n".join([
                    "✅ <b>ТОРГОВЛЯ ВОЗОБНОВЛЕНА</b>",
                    fmt.block("Почему", f"Теневые сделки за {c.get('paper_mode_days', 3)} дн.: {len(vals)} шт., средний {avg:+.2f}R")]), html=True)
        log.info("Режим наблюдения: %s (тени %d, средний %+.2fR)", "ДА" if self.paper_mode else "нет", len(vals), avg)

    def _names(self):
        """Тикер -> название компании для сообщений: по списку инструментов биржи (Мосбиржа)."""
        names = {}
        try:
            inst = self.ex.instruments() if self.market == "moex" else {}
        except Exception as e:  # noqa: BLE001
            log.warning("названия инструментов: %s", e)
            return names
        share_names = {i["base"]: i.get("name", "") for i in inst.values() if i["kind"] == "share"}
        idx = {"IMOEX": "индекс Мосбиржи", "RTS": "индекс РТС"}
        for sym, i in inst.items():
            if i["kind"] == "share":
                names[sym] = i.get("name", "")
            else:
                base = share_names.get(i["base"]) or idx.get(i["base"]) or i["base"]
                if i.get("expiration"):
                    d = i["expiration"]
                    names[sym] = f"{base}, фьючерс до {d[8:10]}.{d[5:7]}.{d[:4]}"
                else:
                    names[sym] = f"{base}, вечный фьючерс"
        return {k: v for k, v in names.items() if v}

    def run(self):
        iv = self.s.get("intervals")
        self.cal.refresh(force=True)
        if self.divcal:
            threading.Thread(target=self.divcal.refresh, kwargs={"force": True}, daemon=True, name="divcal").start()
        bal = self.ex.balances()
        equity = bal["equity"]
        self.risk.check(equity, realized=bal["wallet"])
        threading.Thread(target=self.news_loop, daemon=True, name="news").start()
        for i in range(int(self.s.get("llm.workers", 4) or 1)):
            threading.Thread(target=self.news_worker, daemon=True, name=f"analyst-{i}").start()
        self.tg_live.start()
        x_on = self.xstream.start() if self.xstream else False
        m = self.risk.metrics(equity)
        mode = {"demo": "demo-счёт, виртуальные деньги", "testnet": "testnet", "live": "РЕАЛЬНЫЕ ДЕНЬГИ"}[self.s.mode]
        mk = {"crypto": "крипта, Bybit", "moex": "Мосбиржа, T-Invest"}.get(self.market, self.market)
        self.tg.send("\n\n".join([
            f"🤖 <b>БОТ ЗАПУЩЕН</b>: {fmt.esc(mk)}",
            fmt.block("Режим", fmt.esc(mode) + (", dry-run: сделки не открываются, только сигналы" if self.dry else "")),
            fmt.block("Капитал", f"Торговый {fmt.money(m['tradable'])} {fmt.CUR}, резерв {fmt.money(m['reserve'])}, "
                      f"весь счёт {fmt.money(equity)} {fmt.CUR}"),
            fmt.block("Источники", f"Аналитик {'включён' if self.analyst else 'ВЫКЛЮЧЕН (нет ANTHROPIC_API_KEY)'}; "
                      f"{len(self.news.tg_channels)} Telegram-каналов, {len(self.s.get('sources.rss') or {})} RSS-лент"
                      + (", раскрытие эмитентов" if self.market == "moex" else ", анонсы бирж") + (", поток X" if x_on else "")
                      + ("; Telegram в реальном времени" if self.tg_live.enabled else "; Telegram: быстрые каналы каждые 5 с, остальные 30 с")),
        ]), html=True)
        while True:
            now = time.time()
            try:
                self.cal.refresh()
                if self.divcal and time.time() - self.divcal.ts > 6 * 3600:
                    threading.Thread(target=self.divcal.refresh, daemon=True, name="divcal").start()
                self.mood.refresh()
                if self.expect and self.expect.due(now):
                    self.expect.refresh(now)
                if now - self.last.get("paper_check", 0) >= 1800:
                    self.last["paper_check"] = now
                    try:
                        self.update_paper_mode(now)
                    except Exception as e:  # noqa: BLE001
                        log.warning("режим наблюдения: %s", e)
                try:
                    bal = self.ex.balances()
                    equity = bal["equity"]
                    positions = self.ex.positions()
                    self.last_known = (equity, positions, bal)
                    self.risk.check(equity, realized=bal["wallet"])
                except Exception as e:  # noqa: BLE001
                    # биржа/ретранслятор недоступны: команды из Telegram всё равно обрабатываем по последним данным
                    equity, positions, bal = self.last_known
                    self.broker_down_since = getattr(self, "broker_down_since", None) or now
                    log.error("Биржа недоступна (%s), работаю по последним данным от %s",
                              str(e)[:120], time.strftime("%H:%M:%S", time.gmtime(self.last_known_ts)))
                    self.handle_commands(equity, positions)
                    if now - self.broker_down_since > 600 and now - self.last.get("broker_alert", 0) > 1800:
                        self.last["broker_alert"] = now
                        self.tg.send(fmt.block("⚠️ Нет связи с брокером", "Уже больше 10 минут: котировки и ордера недоступны, "
                                     "позиции под стоп-заявками брокера. Команды принимаю, сделки не открываю."), html=True)
                    time.sleep(iv.get("loop_sleep_sec", 10))
                    continue
                self.last_known_ts = now
                self.broker_down_since = None
                self.handle_commands(equity, positions)
                self.handle_events(equity, positions)

                if now - self.last["manage"] >= iv["manage_sec"]:
                    self.last["manage"] = now
                    self.j.log_equity(equity)
                    self.exe.check_pendings(equity)
                    try:
                        self.shadow.resolve_due()
                    except Exception as e:  # noqa: BLE001
                        log.warning("проверка теневых сделок: %s", e)
                    closed = self.exe.manage()
                    self.closed_since_review += len(closed)
                    if self.exe.driver and now - self.last.get("driver_eval", 0) >= 1800:
                        self.last["driver_eval"] = now
                        try:
                            self.exe.driver.evaluate_due()
                        except Exception as e:  # noqa: BLE001
                            log.warning("проверка вердиктов драйвера: %s", e)
                    self.exe.reconcile()
                event_block = self.eventplay.tick(now, equity, positions) if self.eventplay else False
                if now - self.last["scan"] >= iv["scan_sec"]:
                    self.last["scan"] = now
                    if event_block:
                        log.info("скан: входы запрещены до решения ЦБ")
                    else:
                        self.step_scan(equity, self.ex.positions())
                if now - self.last["report"] >= iv["balance_report_hours"] * 3600:
                    self.last["report"] = now
                    self.step_report(equity, positions)
                if self.reviewer and self.s.get("learning.enabled") and (
                        self.closed_since_review >= iv["review_every_closed_trades"]
                        or now - self.last["review"] >= iv["review_every_hours"] * 3600):
                    self.last["review"] = now
                    self.closed_since_review = 0
                    self.reviewer.run(apply=self._auto_apply())
            except KeyboardInterrupt:
                self.stop_flag.set()
                if self.xstream:
                    self.xstream.stop.set()
                self.tg.send("Бот остановлен вручную (Ctrl+C). Позиции остаются под стопами на бирже.")
                raise
            except Exception as e:  # noqa: BLE001
                log.error("Ошибка цикла: %s\n%s", e, traceback.format_exc())
                self.err_count += 1
                # в Telegram не чаще раза в 30 минут, со счётчиком, чтобы не засыпать сообщениями
                if time.time() - self.last_err_sent >= 1800:
                    self.last_err_sent = time.time()
                    short = str(e).split("\n")[0][:160]
                    self.tg.send("\n\n".join([fmt.block("⚠️ Ошибка в работе бота", f"{self.err_count} за последнее время"),
                                               fmt.block("Текст", fmt.esc(short)),
                                               fmt.block("Что это значит", "Бот продолжает работать, позиции под стопами на бирже.")]), html=True)
                    self.err_count = 0
                time.sleep(30)
            time.sleep(3)


if __name__ == "__main__":
    setup_logging()
    Bot(dry_run="--dry-run" in sys.argv).run()
