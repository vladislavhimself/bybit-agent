"""Сделки на расхождении с ожиданием, подтверждённом рынком (решение владельца 08.11.2026; история — research/backtest_expectations.py,
research/news_earnings_cpi.py).

A. Решение ЦБ. Само решение и консенсус — только для журнала. Через cbr_wait_hours (4 ч) смотрим индекс гособлигаций RGBI:
   упал на cbr_rgbi_min_pct (0.15%) и больше — рынок получил решение ЖЁСТЧЕ, чем закладывал → шорт индекса (IMOEXF);
   вырос — мягче → лонг. 29 случаев 2019–2026: неделя 72% в плюс, 2 недели +2.6% на сделку; сторона по словам решения −18%.
B. Отчёт «хуже ожиданий» (формулировка ленты) по компании. Через earn_confirm_min (60 мин) бумага не выросла → шорт.
   81 случай: 2 недели 59% в плюс, +0.73% на сделку с издержками, плюс до и после 2024. Лонг на «лучше ожиданий» не делаем: −0.5% за неделю.
Выходы по причине, а не по времени (решение владельца): тезис сломался (A — RGBI вернулся к уровню до решения; B — цена выше уровня
до новости), откат от лучшей цены на trail_pct (3%), аварийный стоп stop_pct (5%), предохранитель max_days (14 дней).
Стопы по цене 2–3% на истории ВРЕДИЛИ (выбивает шумом), поэтому стоп только аварийный.
Размер: ступень low и потолок риска risk_cap_pct капитала (первые сделки — уменьшенным размером)."""
import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import fmt, net
from .signals import Signal

log = logging.getLogger("eventtrades")
MSK = ZoneInfo("Europe/Moscow")
ISS_RGBI = "https://iss.moex.com/iss/engines/stock/markets/index/boards/SNDX/securities/RGBI/candles.json"
KV = "eventtrades_state"


class EventTrades:
    def __init__(self, bot):
        self.b = bot
        self.state = bot.j.kv_get(KV, None) or {"cbr": None, "earn": [], "meta": {}, "done": []}
        self._http = None
        self._rgbi_cache = (0.0, None)

    def cfg(self, k, d=None):
        c = self.b.s.get("events") or {}
        return c.get(k, d)

    def _save(self):
        self.b.j.kv_set(KV, self.state)

    # ---------- RGBI (ISS Мосбиржи, 10-минутные свечи) ----------
    def _session(self):
        if self._http is None:
            self._http = net.session()
        return self._http

    def rgbi_candles(self, day):
        r = self._session().get(ISS_RGBI, params={"interval": 10, "from": day, "till": day, "iss.meta": "off"}, timeout=20)
        r.raise_for_status()
        j = r.json()["candles"]
        cols = j["columns"]
        return [dict(zip(cols, row)) for row in j["data"]]

    def rgbi_at(self, ts):
        """Открытие 10-минутной свечи RGBI, в которую попадает момент ts (цена ДО решения, если ts = 13:30 → свеча 13:30)."""
        d = datetime.fromtimestamp(ts, MSK)
        rows = self.rgbi_candles(d.strftime("%Y-%m-%d"))
        best = None
        for c in rows:
            b = datetime.strptime(c["begin"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK)
            if b <= d:
                best = c
        return float(best["open"]) if best else None

    def rgbi_last(self, now=None):
        now = now or time.time()
        ts, v = self._rgbi_cache
        if v is not None and now - ts < 300:
            return v
        d = datetime.fromtimestamp(now, MSK)
        rows = self.rgbi_candles(d.strftime("%Y-%m-%d"))
        if not rows:
            rows = self.rgbi_candles((d - timedelta(days=1)).strftime("%Y-%m-%d"))
        if not rows:
            return None
        v = float(rows[-1]["close"])
        self._rgbi_cache = (now, v)
        return v

    # ---------- A: взвод после решения ЦБ ----------
    def arm_cbr(self, day, decision_ts, actual, consensus, sid):
        try:
            pre = self.rgbi_at(decision_ts - 60)
        except Exception as e:  # noqa: BLE001
            log.warning("RGBI до решения не получен: %s", e)
            pre = None
        self.state["cbr"] = {"day": day, "ts": int(decision_ts), "actual": actual, "consensus": consensus, "pre": pre, "sid": sid, "done": False}
        self._save()
        log.info("взведено правило A: решение %s, RGBI до решения %s", actual, pre)

    def _tick_cbr(self, now, equity, positions):
        c = self.state.get("cbr")
        if not c or c.get("done"):
            return
        wait = float(self.cfg("cbr_wait_hours", 4)) * 3600
        if now < c["ts"] + wait:
            return
        c["done"] = True
        self._save()
        try:
            pre = c.get("pre") or self.rgbi_at(c["ts"] - 60)
            last = self.rgbi_last(now)
        except Exception as e:  # noqa: BLE001
            log.warning("RGBI после решения не получен: %s", e)
            self.b.tg.send(fmt.block("🏦 Решение ЦБ: сделки нет", "Не удалось получить индекс облигаций RGBI, подтверждения нет"), html=True)
            return
        if not pre or not last:
            self.b.tg.send(fmt.block("🏦 Решение ЦБ: сделки нет", "Нет данных RGBI до/после решения"), html=True)
            return
        chg = (last / pre - 1) * 100
        thr = float(self.cfg("cbr_rgbi_min_pct", 0.15))
        expl = (f"RGBI {pre:.2f} → {last:.2f} ({chg:+.2f}%) за {wait / 3600:.0f} ч после решения {c['actual']:g}%"
                + (f", консенсус был {c['consensus']:g}%" if c.get("consensus") is not None else ""))
        if abs(chg) < thr:
            if self.b.expect_rules and c.get("sid"):
                self.b.expect_rules.confirm(c["sid"], chg, False, "сделки нет: облигации не сдвинулись (рынок закладывал)")
            self.b.tg.send("\n\n".join([f"🏦 <b>РЕШЕНИЕ ЦБ {c['actual']:g}%: СДЕЛКИ НЕТ</b>", fmt.block("Облигации", fmt.esc(expl)),
                                        fmt.block("Почему", f"Сдвиг меньше {thr}%: рынок это решение уже закладывал, направления нет")]), html=True)
            return
        direction = "short" if chg < 0 else "long"
        why = f"{'жёстче' if chg < 0 else 'мягче'} ожиданий рынка: облигации {'упали' if chg < 0 else 'выросли'} на {abs(chg):.2f}% после решения ЦБ {c['actual']:g}%"
        key = f"A:cbr-{c['day']}"
        opened = self._enter(self.cfg("symbols") or ["IMOEXF"], direction, key, why, equity, positions, {"pre_level": pre, "sid": c.get("sid"), "kind": "A"})
        if self.b.expect_rules and c.get("sid"):
            self.b.expect_rules.confirm(c["sid"], chg, True, f"{direction}: {', '.join(opened) if opened else 'вход не удался'}", None)
        self.b.tg.send("\n\n".join([f"🏦 <b>РЕШЕНИЕ ЦБ {c['actual']:g}%: {'ШОРТ' if direction == 'short' else 'ЛОНГ'} ИНДЕКСА</b>",
                                    fmt.block("Облигации подтвердили", fmt.esc(expl)),
                                    fmt.block("Сделка", f"{', '.join(opened) if opened else 'не открылась (см. лог)'}; выход — облигации вернутся к {pre:.2f}, "
                                              f"откат {self.cfg('trail_pct', 3.0)}% от лучшей цены или {self.cfg('max_days', 14)} дней"),
                                    fmt.block("История", "29 решений 2019–2026: неделя 72% в плюс, 2 недели +2.6% на сделку")]), html=True)

    # ---------- B: отчёт хуже ожиданий ----------
    def side_for(self, info):
        """Сторона кандидата по расхождению или None. B (шорт): отчёт «хуже ожиданий» по формулировке, либо число (прибыль/дивиденд) ниже
        консенсуса на short_size_min типичных отклонений. C (лонг, по флагу long_enabled): число выше на long_size_min (2 σ = 30%+):
        «чуть лучше» лонг не даёт (бумага потом неделю отстаёт), только большое расхождение (8 дивидендных случаев: неделя +2.5%, 88%)."""
        if not info:
            return None
        kind, s, size = info.get("kind"), float(info.get("surprise") or 0), float(info.get("size") or 0)
        if kind == "earn":
            return "short" if s < 0 else None
        if kind in ("np", "div"):
            if s < 0 and size >= float(self.cfg("short_size_min", 1.0)):
                return "short"
            if s > 0 and size >= float(self.cfg("long_size_min", 2.0)) and self.cfg("long_enabled", False):
                return "long"
        return None

    def on_surprise(self, info, title, ts, news_id=""):
        """Из expect_rules.on_event: кандидат в сделку с проверкой рынком через час."""
        side = self.side_for(info)
        if not side:
            return
        base = info["base"]
        if any(x["base"] == base and ts - x["ts"] < 5 * 86400 for x in self.state["earn"]):
            return
        sym = self._symbol_for(base) if side == "short" else (self.b.ex.news_pick(base) if hasattr(self.b.ex, "news_pick") else None)
        if not sym:
            log.info("%s: %s — нет инструмента", "B" if side == "short" else "C", base)
            return
        try:
            pre = float(self.b.ex.last_price(sym) or 0)
        except Exception:  # noqa: BLE001
            pre = 0.0
        if pre <= 0:
            return
        self.state["earn"].append({"base": base, "sym": sym, "ts": int(ts), "pre": pre, "sid": info.get("sid"), "title": (title or "")[:120],
                                   "check_ts": int(ts + float(self.cfg("earn_confirm_min", 60)) * 60), "done": False, "news_id": news_id,
                                   "side": side, "kind": info.get("kind"), "size": round(float(info.get("size") or 0), 2)})
        self._save()
        log.info("%s: кандидат %s (%s) %s по %.4g, размер %.1f, проверка через %s мин", "B" if side == "short" else "C", base, sym, side, pre,
                 float(info.get("size") or 0), self.cfg("earn_confirm_min", 60))

    def _symbol_for(self, base):
        pick = self.b.ex.news_pick(base) if hasattr(self.b.ex, "news_pick") else None
        if not pick:
            return None
        inst = self.b.ex.instruments().get(pick) or {}
        if inst.get("kind") == "share" and not inst.get("short_ok", False):
            return None
        return pick

    def _tick_earn(self, now, equity, positions):
        keep = []
        for c in self.state["earn"]:
            if c.get("done"):
                if now - c["ts"] < 30 * 86400:
                    keep.append(c)
                continue
            if now - c["ts"] > 2 * 86400:
                continue   # проверка так и не состоялась (торги закрыты двое суток) — забываем
            if now < c["check_ts"] or not self.b.ex.trading_open(c["sym"]):
                keep.append(c)
                continue
            c["done"] = True
            keep.append(c)
            try:
                px = float(self.b.ex.last_price(c["sym"]) or 0)
            except Exception:  # noqa: BLE001
                px = 0.0
            if px <= 0:
                continue
            move = (px / c["pre"] - 1) * 100
            side = c.get("side", "short")
            long = side == "long"
            what = {"earn": "ОТЧЁТ ХУЖЕ ОЖИДАНИЙ", "np": f"ПРИБЫЛЬ {'ВЫШЕ' if long else 'НИЖЕ'} КОНСЕНСУСА", "div": f"ДИВИДЕНД {'ВЫШЕ' if long else 'НИЖЕ'} ОЖИДАНИЙ"}.get(c.get("kind"), "РАСХОЖДЕНИЕ")
            size_txt = f" (размер {c['size']:.1f} σ)" if c.get("size") else ""
            tol = float(self.cfg("earn_max_up_pct", 0.2))
            failed = (move > tol) if not long else (move < -tol)
            if failed:
                if self.b.expect_rules and c.get("sid"):
                    self.b.expect_rules.confirm(c["sid"], move, False, f"сделки нет: бумага {move:+.2f}% за час — рынок понял иначе")
                self.b.tg.send("\n\n".join([f"{'📈' if long else '📉'} <b>{what}: {fmt.esc(fmt.label(c['sym']))} — СДЕЛКИ НЕТ</b>",
                                            fmt.block("Проверка", f"За час бумага {move:+.2f}%: рынок понял иначе, {'не покупаем' if long else 'не шортим'}"),
                                            fmt.block("Новость", fmt.esc(c["title"]))]), html=True)
                continue
            why = f"{what.lower()}{size_txt}, за час бумага {move:+.2f}% ({'удержала рост' if long else 'не выросла'})"
            key = f"{'C' if long else 'B'}:{c['base']}-{datetime.fromtimestamp(c['ts'], MSK).strftime('%Y%m%d')}"
            opened = self._enter([c["sym"]], side, key, why, equity, positions, {"pre_level": c["pre"], "sid": c.get("sid"), "kind": "C" if long else "B"})
            if self.b.expect_rules and c.get("sid"):
                self.b.expect_rules.confirm(c["sid"], move, True, f"{side}: {', '.join(opened) if opened else 'вход не удался'}")
            hist = ("8 дивидендов выше ожиданий на 15%+ (2019–2026): неделя +2.5%, вверх 88%; «чуть лучше» лонг не даёт" if long
                    else "81 отчёт 2019–2026: 2 недели 59% в плюс, +0.7% на сделку; лонг на «лучше ожиданий» не работает")
            self.b.tg.send("\n\n".join([f"{'📈' if long else '📉'} <b>{what}: {'ЛОНГ' if long else 'ШОРТ'} {fmt.esc(fmt.label(c['sym']))}</b>",
                                        fmt.block("Подтверждение", f"За час после новости бумага {move:+.2f}% — {'покупатели остались' if long else 'продавцы остались'}{size_txt}"),
                                        fmt.block("Сделка", f"{', '.join(opened) if opened else 'не открылась (см. лог)'}; выход — цена {'ниже' if long else 'выше'} {c['pre']:g} (уровень до новости), "
                                                  f"откат {self.cfg('trail_pct', 3.0)}% от лучшей цены или {self.cfg('max_days', 14)} дней"),
                                        fmt.block("История", hist),
                                        fmt.block("Новость", fmt.esc(c["title"]))]), html=True)
        self.state["earn"] = keep
        self._save()

    # ---------- общий вход ----------
    def _blocked(self, sym, base, direction, now):
        b = self.b
        if b.eventplay and b.eventplay.blocked(now):
            return "окно решения ЦБ"
        if getattr(b, "_shock_until", 0) > now and not sym.startswith("IMOEX"):
            return "первый час после серии новостей (шок)"
        if b.risk.halted if hasattr(b.risk, "halted") else False:
            return "бот остановлен"
        inst = b.ex.instruments().get(sym) or {}
        if direction == "short" and b.divcal and inst.get("kind") == "share":
            why = b.divcal.short_block(sym, inst)
            if why:
                return why
        if b.corpcal:
            why = b.corpcal.entry_block(base, b._sector_of(base) if hasattr(b, "_sector_of") else "", direction)
            if why:
                return why
        return ""

    def _enter(self, symbols, direction, key, why, equity, positions, meta):
        b = self.b
        now = time.time()
        opened = []
        for sym in symbols:
            base = b.risk.base_of(sym)
            why_block = self._blocked(sym, base, direction, now)
            if why_block:
                log.info("%s %s: вход по событию заблокирован: %s", key, sym, why_block)
                continue
            try:
                ta, tk, oi = b.market_data(sym, max_age=0)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: данные %s: %s", key, sym, e)
                continue
            if not ta:
                continue
            if not b.risk.can_open(sym, b.j.open_trades(), positions, equity, direction, key, "low"):
                log.info("%s %s: лимиты риска: %s", key, sym, getattr(b.risk, "last_reject", ""))
                continue
            price = ta.get("price_now") or ta["close"]
            sig = Signal(symbol=sym, direction=direction, strength=0.9, tier="low",
                         components={"news": 0.0, "ta": 0.0, "positioning": 0.0, "total": 0.9 if direction == "long" else -0.9, "event": 1.0},
                         reasons=[why, "сделка на расхождении с ожиданием, подтверждённом рынком"], regime=ta.get("regime", ""),
                         news_driven=False, events=[], event_key=key)
            sig.force_market = True
            sig.stop_dist = price * float(self.cfg("stop_pct", 5.0)) / 100   # аварийный стоп: на истории стопы 2–3% вредили
            cap = b.risk.tradable(equity) * float(self.cfg("risk_cap_pct", 0.35)) / 100
            try:
                tid = b.exe.open(sig, ta, equity, positions, risk_cap=cap)
            except Exception as e:  # noqa: BLE001
                log.error("%s %s: вход не удался: %s", key, sym, e)
                continue
            if tid:
                opened.append(sym)
                self.state["meta"][key] = dict(meta, best=0.0, opened=int(now), symbol=sym, direction=direction)
                self._save()
        return opened

    # ---------- сопровождение: выходы по причине ----------
    def owns(self, t):
        return str(t.get("event_key") or "").startswith(("A:", "B:", "C:"))

    def manage(self, t, mark, pos, now=None):
        """Вызывается исполнителем вместо его стандартных выходов. True — сделка закрыта."""
        now = now or time.time()
        key = t.get("event_key") or ""
        meta = self.state["meta"].get(key) or {}
        long = t["side"] == "Buy"
        e0 = float(t.get("entry_init") or t["entry"])
        fav = (mark / e0 - 1) * 100 * (1 if long else -1)   # в нашу сторону, %
        best = max(float(meta.get("best") or 0.0), fav)
        if best > float(meta.get("best") or 0.0):
            meta["best"] = best
            self.state["meta"][key] = meta
            self._save()
        exe = self.b.exe
        stop_pct = float(self.cfg("stop_pct", 5.0))
        if fav <= -stop_pct:
            return exe._close_now(t, f"аварийный стоп {stop_pct:g}% (событийная сделка)")
        days = (now - t["opened_ts"]) / 86400
        if days >= float(self.cfg("max_days", 14)):
            return exe._close_now(t, f"предохранитель {self.cfg('max_days', 14)} дней: причина не отработала и не сломалась")
        trail = float(self.cfg("trail_pct", 3.0))
        if best >= float(self.cfg("trail_min_pct", 1.0)) and best - fav >= trail:
            return exe._close_now(t, f"откат {best - fav:.1f}% от лучшей цены (+{best:.1f}%): берём то, что дали")
        pre = meta.get("pre_level")
        if key.startswith("A:") and pre:
            try:
                last = self.rgbi_last(now)
            except Exception as e:  # noqa: BLE001
                last = None
                log.debug("RGBI для тезиса: %s", e)
            if last and ((not long and last >= pre) or (long and last <= pre)):
                return exe._close_now(t, f"тезис сломался: облигации вернулись к уровню до решения ({last:.2f} против {pre:.2f})")
        if key.startswith(("B:", "C:")) and pre and days >= 1 / 24:
            if (not long and mark > float(pre)) or (long and mark < float(pre)):
                return exe._close_now(t, f"тезис сломался: цена {mark:g} снова выше уровня до новости {pre:g}")
        return False

    # ---------- результаты в журнал расхождений ----------
    def _tick_results(self):
        if not self.b.expect_rules:
            return
        done = set(self.state.get("done") or [])
        for t in self.b.j.closed_trades(60):
            key = t.get("event_key") or ""
            if not key.startswith(("A:", "B:", "C:")) or key in done:
                continue
            meta = self.state["meta"].get(key) or {}
            if meta.get("sid"):
                pct = float(t.get("pnl_r") or 0) * float(self.cfg("stop_pct", 5.0))
                self.b.expect_rules.set_result(meta["sid"], round(pct, 2))
            done.add(key)
        self.state["done"] = sorted(done)[-200:]
        self._save()

    def tick(self, now, equity, positions):
        try:
            self._tick_cbr(now, equity, positions)
        except Exception as e:  # noqa: BLE001
            log.error("правило A: %s", e)
        try:
            self._tick_earn(now, equity, positions)
        except Exception as e:  # noqa: BLE001
            log.error("правило B: %s", e)
        if int(now) % 1800 < 15:
            try:
                self._tick_results()
            except Exception as e:  # noqa: BLE001
                log.debug("результаты событийных сделок: %s", e)

    def text(self):
        c = self.state.get("cbr")
        lines = []
        if c and not c.get("done"):
            lines.append(f"A: ждём облигации после решения ЦБ {c['actual']:g}% до {datetime.fromtimestamp(c['ts'] + float(self.cfg('cbr_wait_hours', 4)) * 3600, MSK):%H:%M} МСК")
        for x in self.state.get("earn") or []:
            if not x.get("done"):
                lines.append(f"{'C' if x.get('side') == 'long' else 'B'}: {x['base']} {x.get('side', 'short')} — проверка в {datetime.fromtimestamp(x['check_ts'], MSK):%H:%M} МСК (цена до новости {x['pre']:g})")
        open_keys = [t.get("event_key") for t in self.b.j.open_trades() if self.owns(t)]
        if open_keys:
            lines.append("Открыты событийные сделки: " + ", ".join(open_keys))
        return lines or ["событийных сделок и кандидатов нет"]
