"""Исполнение: вход со стопом и частичными тейками, перенос в безубыток, трейлинг остатка, тайм-стоп,
закрытие по контр-новости, фиксация результата."""
import json
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
            if part_qty < inst["min_qty"] and Decimal(str(qty)) >= 2 * Decimal(str(inst["min_qty"])):
                part_qty = Decimal(str(inst["min_qty"]))   # целые лоты: доля меньше лота округляется до одного, а не до нуля
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

    def open(self, sig, ta, equity, positions, risk_cap=None):
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
            qty, lev = self.risk.size(sig.symbol, equity, price, stop, sig.tier, positions, risk_cap=risk_cap)
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
        qty, lev = self.risk.size(sig.symbol, equity, limit, stop, sig.tier, positions, risk_cap=risk_cap)
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
        self.j._run("UPDATE trades SET entry_init=entry WHERE id=?", (tid,))
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
        """Снять отложенные ордера. Перед отменой спрашиваем брокера: если ордер уже исполнился (03.11 Яндекс: лимитка
        исполнилась за 2 минуты до отмены по контр-новости), оформляем сделку, иначе позиция осталась бы без присмотра."""
        n = 0
        for p in self.j.pendings():
            if symbols is not None and p["symbol"] not in symbols:
                continue
            st = self.ex.order_status(p["symbol"], p["order_id"]) or {}
            filled = float(st.get("filled") or 0)
            if st.get("status") == "Filled" or filled > 0:
                if st.get("status") != "Filled":
                    self.ex.cancel_order(p["symbol"], p["order_id"])
                self.j.remove_pending(p["id"])
                pay = p["payload"]
                sig = self._sig_from_payload(p["symbol"], pay)
                try:
                    equity = self.ex.balances()["equity"]
                except Exception:  # noqa: BLE001
                    equity = 0.0
                log.warning("%s: отложенный ордер исполнился до отмены (%s): оформляю сделку", p["symbol"], reason)
                self._finish_entry(sig, pay["atr"], pay["dist"], pay["lev"], p["order_id"], equity,
                                   st.get("avg") or p["price"], filled or p["qty"])
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
            e0 = float(t.get("entry_init") or t["entry"])   # R всегда от первоначального входа, даже после добора
            risk_dist = abs(e0 - t["stop_init"])
            if risk_dist <= 0:
                continue
            r_now = ((mark - e0) if long else (e0 - mark)) / risk_dist
            daily_mode = bool(r.get("daily_trend_exit"))
            hours = (time.time() - t["opened_ts"]) / 3600

            # частичные тейки: позиция уменьшилась
            if pos["qty"] < t["qty"] - 1e-12:
                hits = t["tp_hits"] or 0
                taken = (t["qty_init"] or t["qty"]) + float(t.get("add_qty") or 0) - float(t.get("cut_qty") or 0) - pos["qty"]
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
                        be = e0 * (1 + 0.0015 * (1 if long else -1))
                        self._move_stop(t, be, "безубыток после первого тейка")
                t["qty"], t["tp_hits"] = pos["qty"], new_hits

            # 3) пирамидинг: добор части при +pyramid_r R, если тренд 4ч в нашу сторону (7 лет: +12% к итогу)
            pyr = float(r.get("pyramid_part") or 0)
            if pyr and not (t.get("add_qty") or 0) and not (t.get("add_ts") or 0) and r_now >= float(r.get("pyramid_r", 1.5)) and hours >= 1:
                self._pyramid(t, pos, pyr, long, mark)
            # 7) выход по дневному тренду: держим, пока часовое закрытие по нашу сторону 20-дневной средней (7 лет: +38%)
            if daily_mode and hours >= 24:
                sma = self._sma20d(sym)
                last_close = self._last_hour_close(sym) or mark
                if sma and ((long and last_close < sma) or (not long and last_close > sma)):
                    self._close_now(t, f"дневной тренд сломан: закрытие {last_close:g} за 20-дневной средней {sma:g}")
                    closed.append(t)
                    continue
                if hours >= float(r.get("max_hours_daily_exit", 720)) and r_now < 0.5:
                    self._close_now(t, f"тайм-стоп {hours:.0f}ч (дневной режим), R={r_now:.2f}")
                    closed.append(t)
                    continue
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
            if not daily_mode and hours >= r["max_hours_in_trade"] and r_now < 0.5:
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
                v = self.driver.maybe_check(t, r_now, mfe, mark, risk_dist, hours)
                dc = r.get("driver_check") or {}
                if (v is not None and dc.get("mode") == "tighten" and v.verdict in ("tighten", "take")
                        and float(v.confidence) >= float(dc.get("min_confidence", 0.6)) and r_now >= 0.5):
                    part = float(dc.get("take_part") or 0)
                    if (part and v.verdict == "take" and float(v.confidence) >= float(dc.get("take_min_confidence", 0.8))
                            and not (t.get("cut_qty") or 0)):
                        self._cut_part(t, pos, part, long, mark, r_now, v)
                    lock = e0 + r_now * float(dc.get("keep", 0.5)) * risk_dist * (1 if long else -1)
                    self._move_stop(t, lock, f"драйвер выдыхается ({v.verdict}, уверенность {float(v.confidence):.2f}): держу половину текущей прибыли")
            gb_r, gb_min = r.get("giveback_r", 0.6), r.get("giveback_min_peak_r", 0.8)
            for pk, gb in (r.get("giveback_tiers") or []):
                if mfe >= pk:
                    gb_r, gb_min = gb, pk
            if not daily_mode and hours >= min_hold and mfe >= gb_min and r_now <= mfe - gb_r:
                self._close_now(t, f"защита прибыли: пик был {mfe:.2f}R, сейчас {r_now:.2f}R")
                closed.append(t)
                continue

            # сделка застряла: в плюсе, но пик не обновлялся stall_hours
            stall = r.get("stall_hours", 12)
            if not daily_mode and hours >= min_hold and 0.1 <= r_now < 1.0 and (time.time() - mfe_ts) / 3600 >= stall and hours >= stall:
                self._close_now(t, f"сделка застряла: {r_now:.2f}R без прогресса {stall}ч")
                closed.append(t)
                continue

            # дневной режим: после пика min_peak_r стоп держит долю пика (7 лет: +419R, плюс в каждом году; без защиты −14R в 2025)
            if daily_mode:
                plk = r.get("daily_peak_lock") or {}
                if plk and mfe >= float(plk.get("min_peak_r", 3.0)):
                    keep = float(plk.get("keep", 0.5))
                    lock = e0 + mfe * keep * risk_dist * (1 if long else -1)
                    self._move_stop(t, lock, f"защита прибыли: держу {keep * 100:.0f}% от пика {mfe:.2f}R")
            self._refresh_tps(t, pos, long)
            # безубыток, как только прибыль достигла breakeven_r
            if r_now >= r.get("breakeven_r", 0.7):
                be = e0 * (1 + 0.0015 * (1 if long else -1))
                self._move_stop(t, be, f"безубыток при {r_now:.2f}R")

            # трейлинг остатка: ступенями по R и не дальше giveback_r от пика
            if not daily_mode and r_now >= r["trail_after_r"]:
                step = int(r_now)
                ladder = e0 + (step - 1 + 0.2) * risk_dist * (1 if long else -1)
                from_peak = e0 + (mfe - gb_r) * risk_dist * (1 if long else -1)
                new_stop = max(ladder, from_peak) if long else min(ladder, from_peak)
                self._move_stop(t, new_stop, f"трейлинг R={r_now:.2f}, пик {mfe:.2f}R")
        return closed

    def _refresh_tps(self, t, pos, long):
        """Лимитные заявки у брокера живут до конца торгового дня: тейк, выставленный вечером, ночью сгорает.
        Раз в 15 минут проверяем, что неисполненные тейки стоят у брокера, и перевыставляем пропавшие."""
        plan = t.get("tp_plan") or []
        hits = int(t.get("tp_hits") or 0)
        if not isinstance(plan, list) or len(plan) <= hits:
            return
        chk = getattr(self, "_tp_checked", None)
        if chk is None:
            chk = self._tp_checked = {}
        if time.time() - chk.get(t["id"], 0) < 900:
            return
        if pos["qty"] < t["qty"] - 1e-9:
            return   # объём у брокера и в журнале ещё не сошёлся (исполнение или добор в этом цикле): проверим в следующем
        chk[t["id"]] = time.time()
        sym = t["symbol"]
        try:
            active = {o.get("orderId") for o in self.ex.open_orders(sym)}
        except Exception:  # noqa: BLE001
            return
        limits = None
        if hasattr(self.ex, "price_limits"):
            try:
                limits = self.ex.price_limits(sym)
            except Exception:  # noqa: BLE001
                limits = None
        changed = False
        close_side = "Sell" if long else "Buy"
        for lvl in plan[hits:]:
            if lvl.get("order_id") and lvl["order_id"] in active:
                continue
            price = lvl["price"]
            if limits:
                lo, hi = limits
                tick = float(self.ex.instruments()[sym]["tick"])
                price = min(price, hi - tick) if long else max(price, lo + tick)
            try:
                lvl["order_id"] = self.ex.place_limit(sym, close_side, lvl["qty"], price)
                changed = True
                log.info("%s: тейк %.2fR перевыставлен по %g (заявка сгорела с прошлой сессии)", sym, lvl["r"], price)
            except Exception as e:  # noqa: BLE001
                log.debug("%s: тейк %.2fR не перевыставлен: %s", sym, lvl["r"], e)
        if changed:
            self.j.update_tp_plan(t["id"], plan)

    def _sma20d(self, sym):
        """20-дневная средняя по завершённым дням (сегодняшний день не считаем), кеш час."""
        c = getattr(self, "_sma_cache", None)
        if c is None:
            c = self._sma_cache = {}
        v = c.get(sym)
        if v and time.time() - v[0] < 3600:
            return v[1]
        try:
            d = self.ex.klines(sym, "D", 30)
            closes = d["close"].astype(float)
            if len(closes) < 22:
                return None
            import datetime as _dt
            last_day = _dt.datetime.fromtimestamp(int(d["ts"].iloc[-1]) / 1000, _dt.timezone.utc).date()
            closed = closes.iloc[:-1] if last_day >= _dt.datetime.now(_dt.timezone.utc).date() else closes
            sma = float(closed.iloc[-20:].mean())
        except Exception as e:  # noqa: BLE001
            log.debug("sma20d %s: %s", sym, e)
            return None
        c[sym] = (time.time(), sma)
        return sma

    def _last_hour_close(self, sym):
        try:
            d = self.ex.klines(sym, "60", 3)
            import datetime as _dt
            ts_last = int(d["ts"].iloc[-1]) / 1000
            # если последняя свеча ещё идёт (её час не закрылся) — берём предыдущую, иначе она и есть последняя закрытая
            in_progress = time.time() - ts_last < 3600
            return float(d["close"].iloc[-2] if (in_progress and len(d) >= 2) else d["close"].iloc[-1])
        except Exception:  # noqa: BLE001
            return None

    def _trend4h_agrees(self, sym, long):
        try:
            d = self.ex.klines(sym, "240", 80)
            c = d["close"].astype(float)
            e20, e50 = c.ewm(span=20, adjust=False).mean().iloc[-1], c.ewm(span=50, adjust=False).mean().iloc[-1]
            last = float(c.iloc[-1])
            return (last > e20 > e50) if long else (last < e20 < e50)
        except Exception:  # noqa: BLE001
            return False

    def _pyramid(self, t, pos, part, long, mark):
        """Добор части позиции по рынку; стоп у брокера переставляется на весь объём; журнал: средний вход и объём."""
        sym = t["symbol"]
        if not self._trend4h_agrees(sym, long):
            return
        try:
            add_qty = float(self.ex.fmt_qty(sym, (t.get("qty_init") or t["qty"]) * part))
        except Exception:  # noqa: BLE001
            add_qty = 0.0
        self.j._run("UPDATE trades SET add_ts=? WHERE id=?", (int(time.time()), t["id"]))   # одна попытка на сделку
        if add_qty <= 0:
            log.info("%s: добор меньше лота, пропускаю", sym)
            return
        try:
            self.ex.place_market(sym, t["side"], add_qty)
            time.sleep(2)
            p2 = [p for p in self.ex.positions() if p["symbol"] == sym]
            qty_total = float(p2[0]["qty"]) if p2 else float(t["qty"]) + add_qty
            entry_avg = float(p2[0]["entry"]) if p2 else (t["entry"] * t["qty"] + mark * add_qty) / qty_total
            self.j.update_add(t["id"], qty_total, entry_avg, add_qty, mark)
            t["qty"], t["entry"], t["add_qty"], t["add_entry"] = qty_total, entry_avg, add_qty, mark
            moved = False
            if self.s.get("risk.pyramid_stop_breakeven", True):
                # 05.11 Газпром: добор по 109.66 при стопе 111.28 (безубыток первых трёх) превратил ноль в −156.
                # Стоп должен защищать всю позицию: общий безубыток по среднему входу
                be_all = entry_avg * (1 + 0.0015 * (1 if long else -1))
                better = (be_all > t["stop"]) if long else (be_all < t["stop"])
                if better:
                    self._move_stop(t, be_all, "общий безубыток после добора")
                    t["stop"] = be_all
                    moved = True
            if not moved:
                try:
                    self.ex.set_stop_loss(sym, t["stop"])   # стоп-заявка на весь новый объём
                except Exception as e:  # noqa: BLE001
                    log.warning("%s: стоп после добора не переставлен: %s", sym, e)
            log.info("%s: добор %g по %g, объём %g, средний вход %g", sym, add_qty, mark, qty_total, entry_avg)
            self.tg.send("\n\n".join([
                f"➕ <b>ДОБОР</b>: {fmt.esc(fmt.label(sym))} {'ЛОНГ' if long else 'ШОРТ'}",
                fmt.block("Почему", f"Сделка в плюсе {((mark - float(t.get('entry_init') or t['entry'])) if long else (float(t.get('entry_init') or t['entry']) - mark)) / abs(float(t.get('entry_init') or t['entry']) - t['stop_init']):.2f}R, тренд 4ч подтверждён"),
                fmt.block("Что сделал", f"Добрал {add_qty:g} по {mark:g}; объём {qty_total:g}, средний вход {entry_avg:g}, стоп {t['stop']:g} на весь объём"),
            ]), html=True)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: добор не удался: %s", sym, e)

    def _cut_part(self, t, pos, part, long, mark, r_now, v):
        """Наблюдатель сказал «забрать» с высокой уверенностью: закрываем часть позиции по рынку, остаток ведём дальше.
        Один раз на сделку. Позицию в один лот не делим. Стоп-заявка у брокера перевыставляется на новый объём."""
        sym = t["symbol"]
        inst = self.ex.instruments().get(sym) or {}
        min_qty = float(inst.get("min_qty") or 0)
        plan = t.get("tp_plan") if isinstance(t.get("tp_plan"), list) else []
        pend = sum(float(lvl["qty"]) for lvl in plan[int(t.get("tp_hits") or 0):])   # лимитные тейки, которые ещё стоят у брокера
        qty_now = float(pos["qty"])
        try:
            cut = float(self.ex.fmt_qty(sym, qty_now * part))
            if qty_now - cut < pend:   # остаток не должен стать меньше объёма стоящих тейков
                cut = float(self.ex.fmt_qty(sym, qty_now - pend))
        except Exception:  # noqa: BLE001
            cut = 0.0
        if cut <= 0 or cut < min_qty or qty_now - cut <= 0:
            log.info("%s: «забрать» от наблюдателя, но позицию %g не разделить: только подтягиваю стоп", sym, qty_now)
            return False
        try:
            self.ex.place_market(sym, "Sell" if long else "Buy", cut, reduce_only=True)
            time.sleep(2)
            p2 = [p for p in self.ex.positions() if p["symbol"] == sym]
        except Exception as e:  # noqa: BLE001
            log.warning("%s: частичное закрытие по вердикту наблюдателя не удалось: %s", sym, e)
            return False
        left = float(p2[0]["qty"]) if p2 else qty_now - cut
        self.j.update_cut(t["id"], left, cut)
        t["qty"], t["cut_qty"], pos["qty"] = left, cut, left
        cur = self.j._run("SELECT stop FROM trades WHERE id=?", (t["id"],), "one")["stop"]
        try:
            self.ex.set_stop_loss(sym, cur)   # стоп-заявка на прежний объём после сокращения закрыла бы больше, чем осталось
        except Exception as e:  # noqa: BLE001
            log.warning("%s: стоп после частичного закрытия не переставлен: %s", sym, e)
        log.info("%s: закрыто %g из %g по вердикту наблюдателя (take %.2f) при %.2fR, остаток %g", sym, cut, qty_now, float(v.confidence), r_now, left)
        self.tg.send("\n\n".join([
            f"✂️ <b>ЧАСТЬ ПРИБЫЛИ ЗАБРАНА</b>: {fmt.esc(fmt.label(sym))} {'ЛОНГ' if long else 'ШОРТ'}",
            fmt.block("Почему", f"Наблюдатель за драйвером: движение сломано (уверенность {float(v.confidence):.2f})", fmt.esc(str(v.reasoning)[:220])),
            fmt.block("Что сделал", f"Закрыл {cut:g} из {qty_now:g} по рынку около {mark:g} при {r_now:+.2f}R",
                      f"Остаток {left:g} веду дальше, стоп подтягиваю на половину текущей прибыли"),
        ]), html=True)
        return True

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
                        e0 = float(t.get("entry_init") or t["entry"])
                        new_stop = e0 - 0.5 * risk_dist if long else e0 + 0.5 * risk_dist
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
            # биржа ещё не отдала сделку закрытия: считаем по цене стопа (после безубытка это и есть итог),
            # а точную цифру дошлём, когда брокер ответит — без «+0, уточню позже»
            long = t["side"] == "Buy"
            exit_price = float(t.get("stop") or t["entry"])
            if not reason.startswith("стоп"):
                try:
                    exit_price = float(self.ex.last_price(t["symbol"]) or exit_price)   # закрывались по рынку
                except Exception:  # noqa: BLE001
                    pass
            mult = float(t.get("mult") or 1.0)
            pnl = (exit_price - t["entry"]) * float(t["qty"]) * mult * (1 if long else -1) - float(t.get("fees") or 0)
            reason = reason + f" (результат по цене {'стопа' if reason.startswith('стоп') else 'закрытия'} {exit_price:g}, точную цифру уточню по данным брокера)"
        else:
            pnl, exit_price, fees = res
        self.j.close_trade(t["id"], exit_price, pnl, reason, fees)
        risk = abs(float(t.get("entry_init") or t["entry"]) - t["stop_init"]) * (t.get("qty_init") or t["qty"]) * (t.get("mult") or 1.0)
        pnl_r = pnl / risk if risk > 0 else 0
        try:
            tradable = self.risk.tradable(self.ex.equity()[0])
        except Exception:  # noqa: BLE001
            tradable = None
        self.tg.send(fmt.close_message(t, pnl, pnl_r, reason, tradable, fees), html=True)

    def adopt_orphans(self):
        """Позиция у брокера без записи в журнале (сбой, гонка ордера и отмены, ручная сделка) берётся под управление:
        запись в журнал, стоп 2 ATR у брокера, сообщение владельцу. Иначе она висит без стопа и сопровождения."""
        try:
            positions = self.ex.positions()
        except Exception:  # noqa: BLE001
            return []
        known = {t["symbol"] for t in self.j.open_trades()} | {p["symbol"] for p in self.j.pendings()}
        now = time.time()
        just_closed = {t["symbol"] for t in self.j.closed_trades(30) if (t.get("closed_ts") or 0) > now - 600}
        adopted = []
        for p in positions:
            sym = p["symbol"]
            if sym in known or sym in just_closed or abs(p.get("qty") or 0) <= 0:
                continue
            try:
                from . import ta as _ta
                d = self.ex.klines(sym, "60", 120)
                atr = float(_ta.atr(d).iloc[-1])
                inst = self.ex.instruments()[sym]
                long = p["side"] == "Buy"
                dist = float(self.s.get("risk.stop_atr_mult", 2.0)) * atr
                entry = float(p["entry"])
                stop = entry - dist if long else entry + dist
                tid = self.j.open_trade({"symbol": sym, "side": p["side"], "qty": float(p["qty"]), "entry": entry, "stop": stop, "tp": 0.0,
                                         "stop_init": stop, "tier": "low", "score": 0.0, "confidence": 0.0,
                                         "reasons": ["позиция у брокера без записи в журнале: взята под управление при сверке"],
                                         "regime": "", "atr": atr, "leverage": 1.0, "opened_ts": int(now), "status": "open", "order_id": "",
                                         "news_driven": 0, "qty_init": float(p["qty"]), "tp_plan": [], "tp_hits": 0, "event_key": "",
                                         "mult": float(inst["mult"]), "fees": 0.0})
                self.j._run("UPDATE trades SET entry_init=entry WHERE id=?", (tid,))
                try:
                    self.ex.set_stop_loss(sym, stop)
                except Exception as e:  # noqa: BLE001
                    log.warning("%s: стоп для подхваченной позиции не выставлен: %s", sym, e)
                adopted.append(sym)
                log.warning("%s: позиция без записи взята под управление (id %s), стоп %g", sym, tid, stop)
                self.tg.send("\n\n".join([
                    f"🧷 <b>ПОЗИЦИЯ ВЗЯТА ПОД УПРАВЛЕНИЕ</b>: {fmt.esc(fmt.label(sym))} {'ЛОНГ' if long else 'ШОРТ'}",
                    fmt.block("Что случилось", "У брокера есть позиция, а в журнале бота записи не было (сбой или гонка ордера и отмены)"),
                    fmt.block("Что сделал", f"Записал сделку: {p['qty']:g} @ {entry:g}, стоп {stop:g} (2 ATR), веду как обычную"),
                ]), html=True)
            except Exception as e:  # noqa: BLE001
                log.error("%s: не удалось взять позицию под управление: %s", sym, e)
        return adopted

    def reconcile(self):
        """Дозаполнить результат сделок, по которым биржа не ответила при закрытии; подхватить позиции без записи."""
        self.adopt_orphans()
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
