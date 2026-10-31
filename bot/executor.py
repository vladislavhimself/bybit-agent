"""Исполнение: вход со стопом и частичными тейками, перенос в безубыток, трейлинг остатка, тайм-стоп,
закрытие по контр-новости, фиксация результата."""
import logging
import time
from decimal import Decimal

from . import fmt

log = logging.getLogger("executor")


class Executor:
    def __init__(self, settings, exchange, risk, journal, tg):
        self.s, self.ex, self.risk, self.j, self.tg = settings, exchange, risk, journal, tg

    # ---------- открытие ----------
    def _tp_plan(self, symbol, qty, entry, dist, long):
        """Разбить позицию на частичные тейки. Части меньше минимального лота сливаются в остаток."""
        inst = self.ex.instruments()[symbol]
        plan, used = [], Decimal("0")
        for lvl in self.s.get("risk.tp_levels") or []:
            part_qty = Decimal(self.ex.fmt_qty(symbol, qty * lvl["part"]))
            if part_qty < inst["min_qty"] or part_qty <= 0:
                continue
            price = entry + dist * lvl["r"] * (1 if long else -1)
            plan.append({"r": lvl["r"], "qty": float(part_qty), "price": price, "order_id": "", "hit": False})
            used += part_qty
        if used >= Decimal(str(qty)):   # остаток для трейлинга должен быть
            plan = plan[:-1]
        return plan

    def _sig_payload(self, sig, ta):
        return {"direction": sig.direction, "strength": sig.strength, "tier": sig.tier, "components": sig.components,
                "reasons": sig.reasons, "regime": sig.regime, "news_driven": sig.news_driven,
                "event_key": sig.event_key, "atr": ta["atr"],
                "ta": {k: ta.get(k) for k in ("trend_1h", "trend_4h", "trend_d", "breakout", "rsi")}}

    @staticmethod
    def _sig_from_payload(symbol, p):
        from .signals import Signal
        return Signal(symbol=symbol, direction=p["direction"], strength=p["strength"], tier=p["tier"],
                      components=p["components"], reasons=p["reasons"], regime=p["regime"],
                      news_driven=p["news_driven"], events=[], event_key=p.get("event_key", ""))

    def _entry_mode(self, sig, ta):
        """market: входить сразу; pullback: ждать откат лимитным ордером."""
        e = self.s.get("entry") or {}
        if getattr(sig, "force_market", False):
            return "market", "сценарий события: вход сразу"
        if e.get("mode", "pullback") != "pullback":
            return "market", "режим market"
        long = sig.direction == "long"
        pull = ta.get("pull_15m", 0.0)
        # цена и так у 15-минутной средней или ниже неё (для лонга): откат уже случился
        if (long and pull <= e.get("already_pulled_atr", 0.15)) or (not long and pull >= -e.get("already_pulled_atr", 0.15)):
            return "market", f"цена у средней 15м (отрыв {pull:+.2f} ATR), откат уже есть"
        # очень важная и свежая новость: ждать нечего, импульс только начинается
        if sig.news_driven and sig.events:
            newest = max(sig.events, key=lambda x: x["ts"])
            age_min = (time.time() - newest["ts"]) / 60
            if newest["importance"] >= e.get("impulse_min_importance", 8) and age_min <= e.get("impulse_max_age_min", 15):
                return "market", f"новость важности {newest['importance']} вышла {age_min:.0f} мин назад"
        return "pullback", f"цена оторвалась от средней 15м на {pull:+.2f} ATR, ждём откат"

    def open(self, sig, ta, equity, positions):
        r = self.s.get("risk")
        e = self.s.get("entry") or {}
        price = ta.get("price_now") or ta["close"]
        dist = ta["atr"] * r["stop_atr_mult"]
        long = sig.direction == "long"
        side = "Buy" if long else "Sell"
        if hasattr(self.ex, "trading_open") and not self.ex.trading_open(sig.symbol):
            log.info("%s: торги по инструменту сейчас не идут", sig.symbol)
            return None
        mode, why = self._entry_mode(sig, ta)
        if mode == "market":
            stop = price - dist if long else price + dist
            qty, lev = self.risk.size(sig.symbol, equity, price, stop, sig.tier, positions)
            if qty is None:
                log.info("%s: пропуск, %s", sig.symbol, lev)
                return None
            try:
                self.ex.set_leverage(sig.symbol, lev)
                order_id = self.ex.place_market(sig.symbol, side, qty, sl=stop)
            except Exception as ex:  # noqa: BLE001
                log.error("%s: ошибка открытия: %s", sig.symbol, ex)
                if "Not enough balance" in str(ex):
                    if time.time() - getattr(self, "_balance_warn_ts", 0) > 7200:
                        self._balance_warn_ts = time.time()
                        self.tg.send(fmt.block("ℹ️ Песочница: нет свободных денег под новую позицию", f"{fmt.esc(fmt.label(sig.symbol))} {sig.direction}: пропускаю; повторю такие предупреждения не чаще раза в 2 часа"), html=True)
                    return None
                self.tg.send("\n\n".join([fmt.block("⚠️ Не удалось открыть", f"{fmt.esc(fmt.label(sig.symbol))} {sig.direction}"),
                                           fmt.block("Ошибка", fmt.esc(str(ex)[:160]))]), html=True)
                return None
            sig.reasons.append("вход по рынку: " + why)
            return self._finish_entry(sig, ta["atr"], dist, lev, order_id, equity, price, qty)

        # --- вход на откате: лимитный ордер ниже/выше рынка ---
        # по свежей новости рынок часто уходит без отката: ждём неглубокий откат
        k = e.get("pullback_atr_news", 0.15) if sig.news_driven else e.get("pullback_atr", 0.25)
        limit = price - k * ta["atr"] if long else price + k * ta["atr"]
        stop = limit - dist if long else limit + dist
        qty, lev = self.risk.size(sig.symbol, equity, limit, stop, sig.tier, positions)
        if qty is None:
            log.info("%s: пропуск, %s", sig.symbol, lev)
            return None
        try:
            self.ex.set_leverage(sig.symbol, lev)
            order_id = self.ex.place_limit(sig.symbol, side, qty, limit, reduce_only=False)
        except Exception as ex:  # noqa: BLE001
            log.error("%s: ошибка лимитного ордера: %s", sig.symbol, ex)
            if "Not enough balance" in str(ex):
                if time.time() - getattr(self, "_balance_warn_ts", 0) > 7200:
                    self._balance_warn_ts = time.time()
                    self.tg.send(fmt.block("ℹ️ Песочница: нет свободных денег под новую позицию", f"{fmt.esc(fmt.label(sig.symbol))} {sig.direction}: пропускаю; повторю такие предупреждения не чаще раза в 2 часа"), html=True)
                return None
            self.tg.send("\n\n".join([fmt.block("⚠️ Не удалось выставить ордер", f"{fmt.esc(fmt.label(sig.symbol))} {sig.direction}"),
                                       fmt.block("Ошибка", fmt.esc(str(ex)[:160]))]), html=True)
            return None
        wait = int(e.get("wait_minutes", 90))
        sig.reasons.append("вход на откате: " + why)
        payload = self._sig_payload(sig, ta)
        payload.update({"lev": lev, "dist": dist, "ref_price": price, "pull": k})
        self.j.add_pending({"symbol": sig.symbol, "side": side, "qty": qty, "price": limit, "stop": stop,
                            "order_id": order_id, "expires_ts": int(time.time()) + wait * 60,
                            "event_key": sig.event_key, "payload": payload})
        base = fmt.label(sig.symbol)
        self.tg.send("\n\n".join([
            f"⏳ ОРДЕР НА ОТКАТ: <b>{fmt.esc(base)} {'ЛОНГ' if long else 'ШОРТ'}</b>",
            fmt.block("Направление", f"{'ЛОНГ' if long else 'ШОРТ'}, размер {fmt.TIER_RU.get(sig.tier, sig.tier)}, плечо {lev}x"),
            fmt.block("Ждём цену", f"Сейчас {price:g}, лимитка {limit:g} (откат {k} ATR), стоп {stop:g}, объём {qty:g}",
                      f"Если за {wait} мин цена не придёт, ордер снимется"),
            fmt.block("Почему", *[fmt.esc(x) for x in fmt.short_reasons(sig, ta)]),
        ]), html=True)
        return -1

    def _mult(self, symbol):
        return float(self.ex.instruments().get(symbol, {}).get("mult") or 1.0)

    def _finish_entry(self, sig, atr, dist, lev, order_id, equity, fallback_price, fallback_qty):
        """Позиция уже есть на бирже: уточнить вход, поставить стоп и тейки, записать в журнал, сообщить."""
        r = self.s.get("risk")
        long = sig.direction == "long"
        mult = self._mult(sig.symbol)
        fee_in = float((getattr(self.ex, "_last_fill", None) or {}).get("commission") or 0.0)
        side = "Buy" if long else "Sell"
        entry, qty = fallback_price, fallback_qty
        for _ in range(5):
            pos = [p for p in self.ex.positions() if p["symbol"] == sig.symbol]
            if pos:
                entry, qty = pos[0]["entry"], pos[0]["qty"]
                break
            time.sleep(1)
        stop = entry - dist if long else entry + dist
        try:
            self.ex.set_stop_loss(sig.symbol, stop)
        except Exception as ex:  # noqa: BLE001
            log.warning("%s: не удалось поставить стоп: %s", sig.symbol, ex)
        plan = self._tp_plan(sig.symbol, qty, entry, dist, long)
        close_side = "Sell" if long else "Buy"
        limits = None
        if hasattr(self.ex, "price_limits"):
            try:
                limits = self.ex.price_limits(sig.symbol)
            except Exception:  # noqa: BLE001
                limits = None
        for lvl in plan:
            if limits:
                # тейк за пределами дневного коридора (ЛСР: ATR 3.6%, тейк +11%) биржа не принимает — ставим у границы
                lo, hi = limits
                tick = float(self.ex.instruments()[sig.symbol]["tick"])
                clamped = min(lvl["price"], hi - tick) if long else max(lvl["price"], lo + tick)
                if clamped != lvl["price"]:
                    log.info("%s: тейк %.2fR %g вне коридора %g–%g, ставлю %g", sig.symbol, lvl["r"], lvl["price"], lo, hi, clamped)
                    lvl["price"] = clamped
            try:
                lvl["order_id"] = self.ex.place_limit(sig.symbol, close_side, lvl["qty"], lvl["price"])
            except Exception as ex:  # noqa: BLE001
                log.warning("%s: тейк %.2fR не выставлен: %s", sig.symbol, lvl["r"], ex)
        final_tp = plan[-1]["price"] if plan else entry + dist * 2.5 * (1 if long else -1)
        tid = self.j.open_trade({
            "symbol": sig.symbol, "side": side, "qty": qty, "entry": entry, "stop": stop, "tp": final_tp,
            "tier": sig.tier, "score": sig.components["total"], "confidence": sig.strength,
            "reasons": sig.reasons, "regime": sig.regime, "atr": atr, "leverage": lev,
            "order_id": order_id, "news_driven": sig.news_driven, "tp_plan": plan, "event_key": sig.event_key,
            "mult": mult, "fees": fee_in,
        })
        risk_pct = r["risk_per_trade_pct"][sig.tier]
        ta_short = {"trend_1h": 0, "trend_4h": 0, "trend_d": 0, "breakout": 0, "rsi": 0}
        self.tg.send(fmt.entry_message(sig, entry, qty, lev, stop, plan, risk_pct, abs(entry - stop) * qty * mult,
                                       self.risk.tradable(equity), equity, ta_short, mult=mult, fee=fee_in), html=True)
        return tid

    def check_pendings(self, equity):
        """Отложенные ордера: исполнился -> оформить сделку; истёк -> снять."""
        now = time.time()
        for p in self.j.pendings():
            st = self.ex.order_status(p["symbol"], p["order_id"])
            status = (st or {}).get("status", "")
            filled = (st or {}).get("filled", 0.0)
            pay = p["payload"]
            sig = self._sig_from_payload(p["symbol"], pay)
            if status == "Filled" or (filled > 0 and (now >= p["expires_ts"] or status in ("Cancelled", "PartiallyFilledCanceled"))):
                if status != "Filled":
                    self.ex.cancel_order(p["symbol"], p["order_id"])
                self.j.remove_pending(p["id"])
                self._finish_entry(sig, pay["atr"], pay["dist"], pay["lev"], p["order_id"], equity,
                                   (st or {}).get("avg") or p["price"], filled or p["qty"])
            elif status in ("Cancelled", "Rejected", "Deactivated"):
                self.j.remove_pending(p["id"])
                log.info("%s: отложенный ордер %s (%s)", p["symbol"], status, p["order_id"])
            elif now >= p["expires_ts"]:
                self.ex.cancel_order(p["symbol"], p["order_id"])
                self.j.remove_pending(p["id"])
                if getattr(self, "shadow", None) and pay.get("ref_price"):
                    try:   # что было бы, если бы вошли сразу по рынку
                        self.shadow.record(p["symbol"], pay["direction"], "pullback_not_filled", pay["strength"],
                                           self.s.get("signals.entry_threshold"), pay["ref_price"], pay["atr"],
                                           regime=pay.get("regime", ""), comps=pay.get("components"),
                                           news_driven=pay.get("news_driven", False), ts=p["created_ts"],
                                           pull_atr=pay.get("pull"))
                    except Exception as e:  # noqa: BLE001
                        log.warning("теневая сделка: %s", e)
                self.tg.send(fmt.block("⌛ Ордер снят", f"{fmt.esc(fmt.label(p['symbol']))}: откат к {p['price']:g} не случился, сделки нет"), html=True)

    def cancel_pendings(self, reason, symbols=None):
        n = 0
        for p in self.j.pendings():
            if symbols is not None and p["symbol"] not in symbols:
                continue
            self.ex.cancel_order(p["symbol"], p["order_id"])
            self.j.remove_pending(p["id"])
            n += 1
            log.info("%s: отложенный ордер снят: %s", p["symbol"], reason)
        return n

    # ---------- сопровождение ----------
    def manage(self):
        """Сопровождение открытых сделок. Возвращает список закрытых."""
        r = self.s.get("risk")
        closed = []
        positions = {p["symbol"]: p for p in self.ex.positions()}
        for t in self.j.open_trades():
            sym = t["symbol"]
            pos = positions.get(sym)
            if not pos:
                self._finalize(t, "стоп/тейк на бирже")
                closed.append(t)
                continue
            long = t["side"] == "Buy"
            mark = pos["mark"] or t["entry"]
            risk_dist = abs(t["entry"] - t["stop_init"])
            if risk_dist <= 0:
                continue
            r_now = ((mark - t["entry"]) if long else (t["entry"] - mark)) / risk_dist
            hours = (time.time() - t["opened_ts"]) / 3600

            # частичные тейки: позиция уменьшилась
            if pos["qty"] < t["qty"] - 1e-12:
                hits = t["tp_hits"] or 0
                taken = (t["qty_init"] or t["qty"]) - pos["qty"]
                acc, new_hits = 0.0, 0
                for lvl in t["tp_plan"]:
                    acc += lvl["qty"]
                    if taken + 1e-9 >= acc:
                        new_hits += 1
                new_hits = max(new_hits, hits)
                self.j.update_partial(t["id"], pos["qty"], new_hits)
                if new_hits > hits:
                    self.tg.send("\n\n".join([
                        f"💰 ЧАСТИЧНЫЙ ТЕЙК: <b>{fmt.esc(fmt.label(sym))}</b>",
                        fmt.block("Исполнено", f"Тейк {new_hits} по {mark:g}"),
                        fmt.block("Остаток", f"{pos['qty']:g}, стоп переносится в безубыток"),
                    ]), html=True)
                    if hits == 0 and r.get("breakeven_after_tp1"):
                        be = t["entry"] * (1 + 0.0015 * (1 if long else -1))
                        self._move_stop(t, be, "безубыток после первого тейка")
                t["qty"], t["tp_hits"] = pos["qty"], new_hits

            # программный стоп: подстраховка, если стоп-заявка на бирже не сработала (или её нет)
            if r.get("soft_stops") and ((long and mark <= t["stop"]) or (not long and mark >= t["stop"])):
                self._close_now(t, f"стоп {t['stop']:g} (программный)")
                closed.append(t)
                continue

            # пик прибыли (MFE) и когда он обновлялся
            mfe = t.get("mfe_r") or 0.0
            mfe_ts = t.get("mfe_ts") or t["opened_ts"]
            if r_now > mfe + 0.05:
                mfe, mfe_ts = r_now, int(time.time())
                self.j.update_mfe(t["id"], mfe)

            # тайм-стоп: сделка не идёт
            if hours >= r["max_hours_in_trade"] and r_now < 0.5:
                self._close_now(t, f"тайм-стоп {hours:.0f}ч, R={r_now:.2f}")
                closed.append(t)
                continue

            # новостную сделку держим минимум news_min_hold_hours: по архиву плюс набирается к суткам, а не к 4 часам;
            # стоп, безубыток и тейки работают, а защита прибыли и «застряла» ждут
            min_hold = r.get("news_min_hold_hours", 0) if t.get("news_driven") else 0
            # защита прибыли: от пика откатились больше чем на giveback_r. Ступени (giveback_tiers: [[пик R, откат R], ...]):
            # чем выше забрались, тем короче поводок (7 лет Мосбиржи: 1.5R до 3R, 1.0R с 3R, 0.6R с 5R даёт +12% к итогу,
            # жёсткая защита 0.6R режет треть прибыли, «держать до конца» = как сейчас)
            if getattr(self, "driver", None):
                self.driver.maybe_check(t, r_now, mfe, mark, risk_dist, hours)   # пока только тени: вердикт записывается, не применяется
            gb_r, gb_min = r.get("giveback_r", 0.6), r.get("giveback_min_peak_r", 0.8)
            for pk, gb in (r.get("giveback_tiers") or []):
                if mfe >= pk:
                    gb_r, gb_min = gb, pk
            if hours >= min_hold and mfe >= gb_min and r_now <= mfe - gb_r:
                self._close_now(t, f"защита прибыли: пик был {mfe:.2f}R, сейчас {r_now:.2f}R")
                closed.append(t)
                continue

            # сделка застряла: в плюсе, но пик не обновлялся stall_hours
            stall = r.get("stall_hours", 12)
            if hours >= min_hold and 0.1 <= r_now < 1.0 and (time.time() - mfe_ts) / 3600 >= stall and hours >= stall:
                self._close_now(t, f"сделка застряла: {r_now:.2f}R без прогресса {stall}ч")
                closed.append(t)
                continue

            # безубыток, как только прибыль достигла breakeven_r
            if r_now >= r.get("breakeven_r", 0.7):
                be = t["entry"] * (1 + 0.0015 * (1 if long else -1))
                self._move_stop(t, be, f"безубыток при {r_now:.2f}R")

            # трейлинг остатка: ступенями по R и не дальше giveback_r от пика
            if r_now >= r["trail_after_r"]:
                step = int(r_now)
                ladder = t["entry"] + (step - 1 + 0.2) * risk_dist * (1 if long else -1)
                from_peak = t["entry"] + (mfe - gb_r) * risk_dist * (1 if long else -1)
                new_stop = max(ladder, from_peak) if long else min(ladder, from_peak)
                self._move_stop(t, new_stop, f"трейлинг R={r_now:.2f}, пик {mfe:.2f}R")
        return closed

    def _move_stop(self, t, new_stop, why):
        long = t["side"] == "Buy"
        cur = self.j._run("SELECT stop FROM trades WHERE id=?", (t["id"],), "one")["stop"]
        better = (new_stop > cur) if long else (new_stop < cur)
        if not better:
            return
        try:
            self.ex.set_stop_loss(t["symbol"], new_stop)
            self.j.update_stop(t["id"], new_stop)
            log.info("%s: стоп -> %g (%s)", t["symbol"], new_stop, why)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: не удалось переставить стоп: %s", t["symbol"], e)

    # ---------- реакция на новости ----------
    def react_to_events(self, events):
        """Закрыть позиции, против которых вышла важная новость. Возвращает список закрытых сделок."""
        r = self.s.get("risk")
        min_imp = r.get("news_exit_min_importance", 7)
        inst = self.ex.instruments()
        closed = []
        # отложенные ордера против новости снимаем сразу
        for p in self.j.pendings():
            base = inst.get(p["symbol"], {}).get("base", "")
            long = p["side"] == "Buy"
            for e in events:
                if e["importance"] < min_imp or e["confidence"] < 0.6:
                    continue
                if not (base in e["affected"] or (e["market_wide"] and e["importance"] >= 8)):
                    continue
                if (e["direction"] == "bearish" and long) or (e["direction"] == "bullish" and not long):
                    self.cancel_pendings("контр-новость", symbols={p["symbol"]})
                    break
        sectors = r.get("sectors") or {}
        max_names = int(r.get("news_exit_max_names", 4))
        for t in self.j.open_trades():
            base = inst.get(t["symbol"], {}).get("base", "")
            long = t["side"] == "Buy"
            sec = next((k for k, v in sectors.items() if base in v), None)
            for e in events:
                if e["importance"] < min_imp or e["confidence"] < 0.6:
                    continue
                contra = (e["direction"] == "bearish" and long) or (e["direction"] == "bullish" and not long)
                if not contra:
                    continue
                aff = e.get("affected") or []
                # геополитический заголовок с девятью тикерами — общерыночный шум, по архиву он цену не предсказывает:
                # 31.10 две такие «контр-новости» закрыли технический лонг Газпрома в минус, и бот тут же вошёл снова
                wide = bool(e.get("market_wide")) or len(aff) > max_names
                specific = (not wide) and base in aff
                sector_hit = (not wide) and sec and sum(1 for a in aff if a in sectors[sec]) >= 2
                if specific or sector_hit:
                    self._close_now(t, f"контр-новость [{e['event_type']}, imp {e['importance']}]: {e['title'][:60]}")
                    closed.append(t)
                    break
                if wide and e["importance"] >= 8 and r.get("news_wide_tighten", True):
                    # общерыночная и важная: не выходим, а режем риск вдвое — стоп на полпути к входу
                    risk_dist = abs(t["entry"] - t["stop_init"])
                    if risk_dist > 0:
                        new_stop = t["entry"] - 0.5 * risk_dist if long else t["entry"] + 0.5 * risk_dist
                        self._move_stop(t, new_stop, f"общерыночная контр-новость imp {e['importance']}: стоп ближе")
                    break
        return closed

    # ---------- закрытие ----------
    def _close_now(self, t, reason):
        try:
            self.ex.cancel_all(t["symbol"])
            self.ex.close_position(t["symbol"])
            time.sleep(2)
        except Exception as e:  # noqa: BLE001
            if "30079" in str(e) or "not available for trading" in str(e):
                # биржа закрыта для этой бумаги (ночь, выходной): закроем при открытии, стоп-заявка у брокера стоит
                log.warning("%s: биржа закрыта, закрою при открытии сессии (%s)", t["symbol"], reason[:60])
            else:
                log.error("%s: ошибка закрытия: %s", t["symbol"], e)
            return
        self._finalize(t, reason)

    def _exchange_result(self, t):
        """(pnl, exit, fees) по данным биржи за время жизни сделки, с повторами. None, если биржа не ответила.
        pnl уже за вычетом комиссий (и Bybit, и T-Invest считают так)."""
        since = t["opened_ts"] * 1000 - 60000
        price_based = getattr(self.ex, "price_based_pnl", False)
        if price_based:
            # лимитка могла исполниться раньше, чем бот это заметил: окно шире, результат по ценам входа/выхода
            since = t["opened_ts"] * 1000 - 30 * 60000
        for attempt in range(4):
            try:
                if price_based:
                    rows = [x for x in self.ex.closed_pnl(t["symbol"], start_ms=since, entry=t["entry"], side=t["side"]) if x["ts"] >= since]
                else:
                    rows = [x for x in self.ex.closed_pnl(t["symbol"], start_ms=since) if x["ts"] >= since]
                if rows:
                    return sum(x["pnl"] for x in rows), rows[0]["exit"], sum(x.get("fees") or 0 for x in rows)
                if attempt >= 1:
                    return None   # биржа ответила, но записей нет (ещё не прошла запись о закрытии)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: closed_pnl (попытка %d): %s", t["symbol"], attempt + 1, e)
            time.sleep(2 + attempt * 2)
        return None

    def _finalize(self, t, reason):
        self.ex.cancel_all(t["symbol"])
        res = self._exchange_result(t)
        fees = None
        if res is None:
            pnl, exit_price = 0.0, t["entry"]
            reason = reason + " (результат не получен с биржи, уточню позже)"
        else:
            pnl, exit_price, fees = res
        self.j.close_trade(t["id"], exit_price, pnl, reason, fees)
        risk = abs(t["entry"] - t["stop_init"]) * (t.get("qty_init") or t["qty"]) * (t.get("mult") or 1.0)
        pnl_r = pnl / risk if risk > 0 else 0
        try:
            tradable = self.risk.tradable(self.ex.equity()[0])
        except Exception:  # noqa: BLE001
            tradable = None
        self.tg.send(fmt.close_message(t, pnl, pnl_r, reason, tradable, fees), html=True)

    def reconcile(self):
        """Дозаполнить результат сделок, по которым биржа не ответила при закрытии."""
        for t in self.j.unreconciled(48):
            res = self._exchange_result(t)
            if res is not None:
                pnl, exit_price, _fees = res
                self.j.update_pnl(t["id"], exit_price, pnl, t["close_reason"].split(" (результат")[0])
                log.info("%s: результат уточнён: %+.2f %s", t["symbol"], pnl, fmt.CUR)
                self.tg.send(fmt.block("ℹ️ Уточнение результата", f"{fmt.esc(fmt.label(t['symbol']))}: {fmt.signed(pnl)} {fmt.CUR}"), html=True)

    def close_all(self, reason):
        n = self.cancel_pendings(reason)
        for t in self.j.open_trades():
            self._close_now(t, reason)
            n += 1
        for p in self.ex.positions():
            try:
                self.ex.cancel_all(p["symbol"])
                self.ex.close_position(p["symbol"])
                n += 1
            except Exception as e:  # noqa: BLE001
                log.error("close_all %s: %s", p["symbol"], e)
        return n
