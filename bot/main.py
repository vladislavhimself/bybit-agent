"""Главный цикл бота.
Поток новостей: опрос источников -> фильтр -> классификация -> очередь событий.
Главный поток: события из очереди (закрыть контр-позиции, сразу оценить затронутые монеты) ->
сопровождение позиций -> периодический скан рынка -> отчёты -> самообучение."""
import logging
import queue
import sys
import threading
import time
import traceback
from logging.handlers import RotatingFileHandler

from .analyst import Analyst
from .calendar import Calendar
from .config import LOGS, Settings
from .exchange import Exchange
from . import fmt
from .executor import Executor
from .journal import Journal
from .macro import MarketMood
from .news import NewsCollector
from .notify import Telegram
from .playbook import Playbook
from .review import Reviewer
from .risk import RiskManager
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
        self.ex = Exchange(self.s)
        self.j = Journal()
        self.tg = Telegram(self.s.telegram_token, self.s.telegram_chat_id)
        self.cal = Calendar()
        self.playbook = Playbook()
        self.risk = RiskManager(self.s, self.ex, self.j, self.cal)
        self.exe = Executor(self.s, self.ex, self.risk, self.j, self.tg)
        self.whales = WhaleFlows(self.j)
        self.mood = MarketMood()
        self.analyst = (Analyst(self.s, self.j, self.playbook, self.cal, self.whales, self.mood)
                        if self.s.has_anthropic else None)
        self.reviewer = Reviewer(self.s, self.j, self.tg) if self.s.has_anthropic else None
        self.news = NewsCollector(self.ex, self.j, self.s)
        self.events_q = queue.Queue()
        self.raw_q = queue.Queue()          # посты из потока X, минуя опрос
        self.news_wake = threading.Event()
        self.stop_flag = threading.Event()
        self.xstream = None
        if self.s.get("sources.twitter_mode", "stream") == "stream":
            self.xstream = XActivityStream(self.s, self.j, self._on_stream_items,
                                           self.s.get("sources.twitter_accounts") or [])
        self.last = {"scan": 0, "manage": 0, "report": 0, "review": 0}
        self.closed_since_review = 0
        self.cache = {}   # symbol -> (ts, ta, ticker, oi)

    # ---------- поток новостей ----------
    def _on_stream_items(self, items):
        for it in items:
            self.raw_q.put(it)
        self.news_wake.set()

    def news_loop(self):
        interval = self.s.get("intervals.news_poll_sec", 20)
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
                    self.process_news(fresh)
            except Exception as e:  # noqa: BLE001
                log.error("Поток новостей: %s\n%s", e, traceback.format_exc())
            self.news_wake.wait(timeout=max(0.5, next_poll - time.time()))
            self.news_wake.clear()

    def process_news(self, fresh):
        log.info("Новых новостей: %d", len(fresh))
        whale_srcs = {f"tg:{c}" for c in (self.s.get("sources.whale_channels") or [])}
        lessons = self.reviewer.lessons_text() if self.reviewer else ""
        for it in fresh:
            self.j.mark_analyzed(it["id"])
            ev = None
            if it["source"] in whale_srcs:
                ev = parse_whale(it["summary"])
                if ev:
                    if ev["usd"] < self.s.get("sources.whale_min_usd", 50_000_000):
                        continue
                    self.whales.add(ev)
                elif not self.analyst:
                    continue
            if ev is None:
                if not self.analyst:
                    continue
                if not self.analyst.prefilter(it["title"], it["summary"]):
                    continue
                ev = self.analyst.classify(it["title"], it["summary"], it["source"], lessons)
            if not ev or not ev["relevant"] or ev["already_priced"]:
                continue
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
                self.tg.send(f"📰 ВАЖНАЯ НОВОСТЬ ({fmt.RU_TYPE.get(ev['event_type'], 'событие')}, важность "
                             f"{ev['importance']}/10, {d} для {', '.join(ev['affected_symbols']) or 'всего рынка'})\n"
                             f"{fmt.clean(it['title'], 160)}\n{ev['reasoning']}")

    # ---------- данные ----------
    def market_data(self, symbol, max_age=240):
        c = self.cache.get(symbol)
        if c and time.time() - c[0] < max_age:
            return c[1], c[2], c[3]
        df1h = self.ex.klines(symbol, "60", 300)
        df4h = self.ex.klines(symbol, "240", 120)
        df15 = self.ex.klines(symbol, "15", 100)
        dfd = self.ex.klines(symbol, "D", 250)
        ta = snapshot(df1h, df4h, df15, dfd)
        tk = self.ex.ticker(symbol)
        tk["long_ratio"] = self.ex.long_short_ratio(symbol)
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
        open_trades = self.j.open_trades()
        candidates = []
        for sym in symbols:
            if sym not in inst:
                continue
            try:
                ta, tk, oi = self.market_data(sym, max_age)
            except Exception as e:  # noqa: BLE001
                log.debug("%s: данные: %s", sym, e)
                continue
            if not ta:
                continue
            df1h = self.cache.get(sym, (0, None, None, None, None))[4]
            sig = build_signal(sym, inst[sym]["base"], ta, tk, oi, events, params, playbook=self.playbook,
                               whale_net=self.whales.net_score(inst[sym]["base"]), mood=self.mood.score(),
                               df1h=df1h, psych_cfg=self.s.get("psychology") or {})
            if sig:
                candidates.append((sig, ta, tk))
        candidates.sort(key=lambda x: x[0].strength, reverse=True)
        log.info("%s: %d символов, кандидатов %d", tag, len(symbols), len(candidates))
        opened = 0
        for sig, ta, tk in candidates:
            can, why = self.risk.can_open(sig.symbol, open_trades, positions, equity)
            if not can:
                log.info("%s: %s", sig.symbol, why)
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
                                               dist * qty, self.risk.tradable(equity), equity, ta, dry=True))
                continue
            tid = self.exe.open(sig, ta, equity, positions)
            if tid:
                opened += 1
                open_trades = self.j.open_trades()
                positions = self.ex.positions()
                equity, _ = self.ex.equity()
        return opened

    def step_scan(self, equity, positions):
        universe = self.ex.universe()
        inst = self.ex.instruments()
        for e in self.j.recent_events(72):
            if e["importance"] >= 6:
                for base in e["affected"]:
                    for sym in self.ex.symbols_for_base(base):
                        if sym in inst and sym not in universe:
                            universe.append(sym)
        self.evaluate(universe, equity, positions, tag="скан")

    def handle_events(self, equity, positions):
        """События из потока новостей: закрыть контр-позиции и сразу оценить затронутые монеты."""
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
                                         llm, self.cal.next_high(), self.xstream.status() if self.xstream else None))

    def handle_commands(self, equity, positions):
        for cmd in self.tg.pop_commands():
            c = cmd.split()[0].lower()
            log.info("Команда: %s", cmd)
            if c in ("/status", "/balance"):
                self.step_report(equity, positions)
            elif c == "/stop":
                self.risk.halt("ручная остановка (/stop)")
                self.tg.send("⛔ Новые входы остановлены. Открытые позиции остаются под стопами. /panic закроет всё.")
            elif c == "/panic":
                n = self.exe.close_all("ручное закрытие (/panic)")
                self.risk.halt("ручная остановка (/panic)")
                self.tg.send(f"⛔ Закрыто позиций: {n}. Бот остановлен. /resume чтобы продолжить.")
            elif c == "/resume":
                self.risk.resume(equity)
                self.tg.send("▶️ Торговля возобновлена, отсчёт просадки начат заново")
            elif c == "/approve":
                msgs = self.reviewer.apply_pending() if self.reviewer else []
                self.tg.send("Применено:\n" + "\n".join(msgs) if msgs else "Нет ожидающих правок")
            elif c == "/lessons":
                self.tg.send("Уроки:\n" + (self.reviewer.lessons_text(20) if self.reviewer else "аналитик выключен"))
            elif c == "/review":
                if self.reviewer:
                    self.reviewer.run(apply=self._auto_apply())
            elif c == "/calendar":
                self.tg.send(self.cal.context_text(96))
            elif c == "/help":
                self.tg.send("/status /stop /panic /resume /review /approve /lessons /calendar")

    def _auto_apply(self):
        return bool(self.s.get("learning.auto_apply")) and self.s.mode != "live"

    # ---------- цикл ----------
    def run(self):
        iv = self.s.get("intervals")
        self.cal.refresh(force=True)
        equity, _ = self.ex.equity()
        self.risk.check(equity)
        threading.Thread(target=self.news_loop, daemon=True, name="news").start()
        x_on = self.xstream.start() if self.xstream else False
        m = self.risk.metrics(equity)
        mode = {"demo": "demo-счёт, виртуальные деньги", "testnet": "testnet", "live": "РЕАЛЬНЫЕ ДЕНЬГИ"}[self.s.mode]
        self.tg.send(f"🤖 Бот запущен ({mode}{', dry-run: сделки не открываются, только сигналы' if self.dry else ''})\n"
                     f"Торговый капитал {m['tradable']:.0f} USDT, резерв {m['reserve']:.0f}, весь счёт {equity:.0f} USDT\n"
                     f"Аналитик {'включён' if self.analyst else 'ВЫКЛЮЧЕН (нет ANTHROPIC_API_KEY)'}; "
                     f"новости: {len(self.news.tg_channels)} Telegram-каналов, RSS, анонсы бирж{', поток X' if x_on else ''}")
        while True:
            now = time.time()
            try:
                self.cal.refresh()
                self.mood.refresh()
                equity, _ = self.ex.equity()
                positions = self.ex.positions()
                self.handle_commands(equity, positions)
                self.handle_events(equity, positions)

                if now - self.last["manage"] >= iv["manage_sec"]:
                    self.last["manage"] = now
                    self.j.log_equity(equity)
                    closed = self.exe.manage()
                    self.closed_since_review += len(closed)
                if now - self.last["scan"] >= iv["scan_sec"]:
                    self.last["scan"] = now
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
                self.tg.send(f"⚠️ Ошибка цикла: {e}")
                time.sleep(30)
            time.sleep(3)


if __name__ == "__main__":
    setup_logging()
    Bot(dry_run="--dry-run" in sys.argv).run()
