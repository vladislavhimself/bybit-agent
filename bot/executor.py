"""Исполнение: вход со стопом и частичными тейками, перенос в безубыток, трейлинг остатка, тайм-стоп,
закрытие по контр-новости, фиксация результата."""
import json
import logging
import threading
import time
import traceback
from datetime import datetime, timezone
from decimal import Decimal

from . import fmt

log = logging.getLogger("executor")


def _locked(fn):
    """Вход, ведение, сверка и закрытие не должны пересекаться: сценарий решения ЦБ открывает сделку из потока новостей,
    и сверка в главном потоке могла принять позицию «в полёте» за бесхозную и записать её второй раз."""
    def wrap(self, *a, **k):
        with self.lock:
            return fn(self, *a, **k)
    wrap.__name__, wrap.__doc__ = fn.__name__, fn.__doc__
    return wrap


class Executor:
    def __init__(self, settings, exchange, risk, journal, tg):
        self.s, self.ex, self.risk, self.j, self.tg = settings, exchange, risk, journal, tg
        self.lock = threading.RLock()
        self._miss, self._tp_checked, self._pyr_checked, self._close_log, self._alerts = {}, {}, {}, {}, {}
        self._sma_cache, self._sma_bad = {}, {}
        self._audit_ts = self._recon_ts = 0.0
        self.last_deferred = 0

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

    @_locked
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
                if "30049" in str(ex) or "Order book is empty" in str(ex):
                    # 06.11 Циан: бумага на планке, продавцов в стакане нет — рыночная заявка невозможна. Бот через 16 секунд
                    # стучался по рынку ещё раз и слал две одинаковые ошибки. Теперь сразу ставит лимитную заявку: она стоит
                    # в стакане и исполнится, если цена откатит; повторных рыночных попыток нет (по бумаге уже есть ордер).
                    log.warning("%s: стакан пуст (цена на планке), по рынку войти нельзя: ставлю лимитную заявку", sig.symbol)
                    mode, why = "pullback", "стакан пуст (цена на планке), по рынку войти нельзя"
                else:
                    return self._order_failed(sig, ex, "Не удалось открыть")
            if mode == "market":
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
            return self._order_failed(sig, ex, "Не удалось выставить ордер")
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

    def _order_failed(self, sig, ex, title):
        """Заявка не принята: в журнал и владельцу. Нехватка денег песочницы — сообщение не чаще раза в 2 часа."""
        log.error("%s: ошибка заявки: %s", sig.symbol, ex)
        if "Not enough balance" in str(ex):
            if time.time() - getattr(self, "_balance_warn_ts", 0) > 7200:
                self._balance_warn_ts = time.time()
                self.tg.send(fmt.block("ℹ️ Песочница: нет свободных денег под новую позицию", f"{fmt.esc(fmt.label(sig.symbol))} {sig.direction}: пропускаю; повторю такие предупреждения не чаще раза в 2 часа"), html=True)
            return None
        self.tg.send("\n\n".join([fmt.block(f"⚠️ {title}", f"{fmt.esc(fmt.label(sig.symbol))} {sig.direction}"),
                                   fmt.block("Ошибка", fmt.esc(str(ex)[:160]))]), html=True)
        return None

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

    @_locked
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

    @_locked
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
    @_locked
    def manage(self):
        """Сопровождение открытых сделок. Возвращает список закрытых. Ошибка по одной сделке не останавливает остальные."""
        r = self.s.get("risk")
        closed = []
        positions = {p["symbol"]: p for p in self.ex.positions()}
        for t in self.j.open_trades():
            try:
                if self._manage_one(t, positions.get(t["symbol"]), r):
                    closed.append(t)
            except Exception as e:  # noqa: BLE001
                log.error("%s: ошибка ведения сделки %s: %s\n%s", t["symbol"], t["id"], e, traceback.format_exc())
                self._alert_once(f"manage:{t['id']}", 3600, "\n\n".join([
                    f"⚠️ <b>СДЕЛКА НЕ ВЕДЁТСЯ</b>: {fmt.esc(fmt.label(t['symbol']))}",
                    fmt.block("Ошибка", fmt.esc(str(e)[:160])),
                    fmt.block("Что с позицией", "Стоп-заявка у брокера остаётся. Остальные сделки ведутся как обычно, эту повторяю каждый цикл")]))
        return closed

    def _alert_once(self, key, every_sec, text):
        if time.time() - self._alerts.get(key, 0) < every_sec:
            return
        self._alerts[key] = time.time()
        try:
            self.tg.send(text, html=True)
        except Exception:  # noqa: BLE001
            pass

    def _company(self, base):
        """Обычная и привилегированная акция — одна компания (risk.base_alias)."""
        f = getattr(self.risk, "_alias", None)
        return f(base) if f else base

    # отложенные закрытия: биржа была закрыта в момент решения (ночь, клиринг, выходной)
    def _pc_all(self):
        return self.j.kv_get("close_pending", {}) or {}

    def _pc_set(self, t, reason):
        d = self._pc_all()
        new = str(t["id"]) not in d
        if new:
            d[str(t["id"])] = {"reason": reason, "ts": int(time.time())}
            self.j.kv_set("close_pending", d)
        return new

    def _pc_drop(self, t):
        d = self._pc_all()
        if d.pop(str(t["id"]), None) is not None:
            self.j.kv_set("close_pending", d)

    def _manage_one(self, t, pos, r):
        """Одна сделка. True — закрыта и записана."""
        sym, tid = t["symbol"], t["id"]
        inst = self.ex.instruments().get(sym)
        if not pos:
            # Позиции нет в ответе брокера. Один пропуск может быть сбоем ответа: закрытой считаем после двух подряд,
            # иначе сняли бы стоп-заявку с живой позиции и записали сделку закрытой
            miss = self._miss.get(tid, 0) + 1
            self._miss[tid] = miss
            if miss < 2 and inst is not None:
                return False
            self._miss.pop(tid, None)
            self._finalize(t, "стоп/тейк на бирже" if inst is not None
                           else "инструмент пропал из справочника брокера (экспирация): позиции у брокера нет")
            return True
        self._miss.pop(tid, None)
        long = t["side"] == "Buy"
        sign = 1 if long else -1
        if pos.get("side") and pos["side"] != t["side"]:
            # заявка закрыла больше остатка и открыла встречную позицию: журнал считает её своей сделкой, а это не так
            log.error("%s: позиция у брокера %s, в журнале %s: закрываю", sym, pos["side"], t["side"])
            self._alert_once(f"flip:{tid}", 3600, "\n\n".join([
                f"🚨 <b>ПОЗИЦИЯ ПЕРЕВЕРНУЛАСЬ</b>: {fmt.esc(fmt.label(sym))}",
                fmt.block("Что случилось", "У брокера позиция в другую сторону, чем в журнале: заявка закрыла больше остатка"),
                fmt.block("Что делаю", "Закрываю её по рынку и закрываю запись сделки")]))
            return self._close_now(t, "позиция у брокера оказалась встречной (заявка закрыла больше остатка): закрыл её")
        pc = self._pc_all().get(str(tid))
        if pc:
            return self._close_now(t, pc["reason"], defer=True)   # биржа ещё закрыта — ждём, стоп-заявка стоит
        mark = float(pos.get("mark") or 0)
        if pos.get("mark_ok") is False or mark <= 0:
            # брокер не прислал текущую цену: по цене входа считать нельзя (после переноса стопа это выглядело бы как стоп)
            try:
                mark = float(self.ex.last_price(sym) or 0)
            except Exception:  # noqa: BLE001
                mark = 0.0
            if mark <= 0:
                return False
        if pos["qty"] > t["qty"] + 1e-9:
            # у брокера больше, чем в журнале: добор исполнился, а запись не прошла. Приводим журнал и стоп-заявку к факту
            extra = float(pos["qty"]) - float(t["qty"])
            self.j.update_add(tid, float(pos["qty"]), float(pos.get("entry") or t["entry"]), float(t.get("add_qty") or 0) + extra, mark)
            log.warning("%s: у брокера %g, в журнале %g: журнал и стоп-заявка приведены к факту", sym, pos["qty"], t["qty"])
            t["qty"], t["entry"], t["add_qty"] = float(pos["qty"]), float(pos.get("entry") or t["entry"]), float(t.get("add_qty") or 0) + extra
            self._resync_stop(t)
        e0 = float(t.get("entry_init") or t["entry"])   # R всегда от первоначального входа, даже после добора
        risk_dist = abs(e0 - t["stop_init"])
        if risk_dist <= 0:
            return False
        r_now = (mark - e0) * sign / risk_dist
        hours = (time.time() - t["opened_ts"]) / 3600
        daily_mode = bool(r.get("daily_trend_exit"))
        sma = None
        if daily_mode:
            sma = self._sma20d(sym)
            if sma is None and self._sma_unavailable(sym):
                # 20-дневной средней нет (новый контракт, долгий сбой данных): без неё в дневном режиме не осталось бы
                # ни одного выхода, кроме стопа — ведём сделку по обычным правилам (защита прибыли, «застряла», тайм-стоп)
                daily_mode = False

        # фьючерс перед экспирацией: закрываем заранее, после экспирации инструмент пропадает из справочника
        exp = (inst or {}).get("expiration")
        if exp:
            try:
                days_left = (datetime.strptime(exp, "%Y-%m-%d").date() - datetime.now(timezone.utc).date()).days
            except ValueError:
                days_left = 999
            if days_left <= int(r.get("close_days_before_expiry", 3)):
                return self._close_now(t, f"до экспирации контракта {days_left} дн.: закрываю заранее")

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
            self.j.update_partial(tid, pos["qty"], new_hits)
            t["qty"], t["tp_hits"] = pos["qty"], new_hits
            # стоп-заявка у брокера стоит на прежний объём: после тейка она закрыла бы больше остатка и открыла встречную позицию
            self._resync_stop(t)
            if new_hits > hits:
                self.tg.send("\n\n".join([
                    f"💰 ЧАСТИЧНЫЙ ТЕЙК: <b>{fmt.esc(fmt.label(sym))}</b>",
                    fmt.block("Исполнено", f"Тейк {new_hits} по {mark:g}"),
                    fmt.block("Остаток", f"{pos['qty']:g}, стоп-заявка переставлена на остаток"),
                ]), html=True)
                try:   # цена дошла до тейка: пик не меньше его уровня, даже если опрос раз в 30 секунд его не застал
                    tp_r = (float(t["tp_plan"][new_hits - 1]["price"]) - e0) * sign / risk_dist
                    if tp_r > float(t.get("mfe_r") or 0):
                        self.j.update_mfe(tid, tp_r)
                        t["mfe_r"], t["mfe_ts"] = tp_r, int(time.time())
                except Exception:  # noqa: BLE001
                    pass
                if hits == 0 and r.get("breakeven_after_tp1"):
                    be = e0 * (1 + 0.0015 * sign)
                    if self._move_stop(t, be, "безубыток после первого тейка", mark) == "closed":
                        return True

        # выход по дневному тренду: держим, пока часовое закрытие по нашу сторону 20-дневной средней (7 лет: +38%)
        if daily_mode and hours >= 24:
            last_close = self._last_hour_close(sym)   # None при сбое данных: в этом цикле не судим (по текущей цене нельзя — это не закрытие часа)
            if sma and last_close and ((long and last_close < sma) or (not long and last_close > sma)):
                return self._close_now(t, f"дневной тренд сломан: закрытие {last_close:g} за 20-дневной средней {sma:g}")
            if hours >= float(r.get("max_hours_daily_exit", 720)) and r_now < 0.5:
                return self._close_now(t, f"тайм-стоп {hours:.0f}ч (дневной режим), R={r_now:.2f}")
        # программный стоп: подстраховка, если стоп-заявка на бирже не сработала (или её нет)
        if r.get("soft_stops") and ((long and mark <= t["stop"]) or (not long and mark >= t["stop"])):
            return self._close_now(t, f"стоп {t['stop']:g} (программный)")

        # пик прибыли (MFE). В памяти — всегда точный; в базу пишем при заметном приросте или при переходе через порог,
        # от которого зависит защита (пик 2.96R и касание 3.00R раньше не включали защиту половины пика)
        mfe_db = float(t.get("mfe_r") or 0.0)
        mfe = max(mfe_db, r_now)
        mfe_ts = t.get("mfe_ts") or t["opened_ts"]
        if r_now > mfe_db:
            marks = [float((r.get("daily_peak_lock") or {}).get("min_peak_r") or 0), float((r.get("driver_check") or {}).get("min_peak_r") or 0),
                     float(r.get("giveback_min_peak_r") or 0)] + [float(pk) for pk, _ in (r.get("giveback_tiers") or [])]
            if r_now > mfe_db + 0.05 or any(mfe_db < x <= r_now for x in marks if x > 0):
                mfe_ts = int(time.time())
                self.j.update_mfe(tid, r_now)

        # тайм-стоп: сделка не идёт
        if not daily_mode and hours >= r["max_hours_in_trade"] and r_now < 0.5:
            return self._close_now(t, f"тайм-стоп {hours:.0f}ч, R={r_now:.2f}")

        # новостную сделку держим минимум news_min_hold_hours: по архиву плюс набирается к суткам, а не к 4 часам;
        # стоп, безубыток и тейки работают, а защита прибыли и «застряла» ждут
        min_hold = r.get("news_min_hold_hours", 0) if t.get("news_driven") else 0
        if getattr(self, "driver", None):
            v = self.driver.maybe_check(t, r_now, mfe, mark, risk_dist, hours)
            dc = r.get("driver_check") or {}
            if (v is not None and dc.get("mode") == "tighten" and v.verdict in ("tighten", "take")
                    and float(v.confidence) >= float(dc.get("min_confidence", 0.6)) and r_now >= 0.5):
                part = float(dc.get("take_part") or 0)
                if (part and v.verdict == "take" and float(v.confidence) >= float(dc.get("take_min_confidence", 0.8))
                        and not (t.get("cut_qty") or 0)):
                    self._cut_part(t, pos, part, long, mark, r_now, v)
                lock = e0 + r_now * float(dc.get("keep", 0.5)) * risk_dist * sign
                if self._move_stop(t, lock, f"драйвер выдыхается ({v.verdict}, уверенность {float(v.confidence):.2f}): держу половину текущей прибыли", mark) == "closed":
                    return True
        # защита прибыли: от пика откатились больше чем на giveback_r. Ступени (giveback_tiers: [[пик R, откат R], ...]):
        # чем выше забрались, тем короче поводок (7 лет Мосбиржи: 1.5R до 3R, 1.0R с 3R, 0.6R с 5R даёт +12% к итогу)
        gb_r, gb_min = r.get("giveback_r", 0.6), r.get("giveback_min_peak_r", 0.8)
        for pk, gb in (r.get("giveback_tiers") or []):
            if mfe >= pk:
                gb_r, gb_min = gb, pk
        if not daily_mode and hours >= min_hold and mfe >= gb_min and r_now <= mfe - gb_r:
            return self._close_now(t, f"защита прибыли: пик был {mfe:.2f}R, сейчас {r_now:.2f}R")

        # сделка застряла: в плюсе, но пик не обновлялся stall_hours
        stall = r.get("stall_hours", 12)
        if not daily_mode and hours >= min_hold and 0.1 <= r_now < 1.0 and (time.time() - mfe_ts) / 3600 >= stall and hours >= stall:
            return self._close_now(t, f"сделка застряла: {r_now:.2f}R без прогресса {stall}ч")

        # дневной режим: после пика min_peak_r стоп держит долю пика (7 лет: +419R, плюс в каждом году; без защиты −14R в 2025)
        if daily_mode:
            plk = r.get("daily_peak_lock") or {}
            if plk and mfe >= float(plk.get("min_peak_r", 3.0)):
                keep = float(plk.get("keep", 0.5))
                lock = e0 + mfe * keep * risk_dist * sign
                if self._move_stop(t, lock, f"защита прибыли: держу {keep * 100:.0f}% от пика {mfe:.2f}R", mark) == "closed":
                    return True
        self._refresh_tps(t, pos, long)
        # безубыток, как только прибыль достигла breakeven_r
        if r_now >= r.get("breakeven_r", 0.7):
            be = e0 * (1 + 0.0015 * sign)
            if self._move_stop(t, be, f"безубыток при {r_now:.2f}R", mark) == "closed":
                return True

        # трейлинг остатка: ступенями по R и не дальше giveback_r от пика
        if not daily_mode and r_now >= r["trail_after_r"]:
            step = int(r_now)
            ladder = e0 + (step - 1 + 0.2) * risk_dist * sign
            from_peak = e0 + (mfe - gb_r) * risk_dist * sign
            new_stop = max(ladder, from_peak) if long else min(ladder, from_peak)
            if self._move_stop(t, new_stop, f"трейлинг R={r_now:.2f}, пик {mfe:.2f}R", mark) == "closed":
                return True

        # пирамидинг: добор части при +pyramid_r R, если тренд 4ч в нашу сторону (7 лет: +12% к итогу). После всех проверок
        # выхода и только пока позицию не разгружали: добирать после тейка или после «забрать» от наблюдателя — противоречие
        pyr = float(r.get("pyramid_part") or 0)
        if (pyr and not (t.get("add_qty") or 0) and not (t.get("add_ts") or 0) and not (t.get("tp_hits") or 0)
                and not (t.get("cut_qty") or 0) and r_now >= float(r.get("pyramid_r", 1.5)) and hours >= 1):
            self._pyramid(t, pos, pyr, long, mark)
        return False

    def _refresh_tps(self, t, pos, long):
        """Лимитные заявки у брокера живут до конца торгового дня: тейк, выставленный вечером, ночью сгорает.
        Раз в 15 минут проверяем, что неисполненные тейки стоят у брокера, и перевыставляем пропавшие.
        Если список заявок или статус заявки получить не удалось — ничего не выставляем: дубль тейка закрыл бы лишнее."""
        plan = t.get("tp_plan") or []
        hits = int(t.get("tp_hits") or 0)
        if not isinstance(plan, list) or len(plan) <= hits:
            return
        if time.time() - self._tp_checked.get(t["id"], 0) < 900:
            return
        if pos["qty"] < t["qty"] - 1e-9:
            return   # объём у брокера и в журнале ещё не сошёлся (исполнение или добор в этом цикле): проверим в следующем
        sym = t["symbol"]
        try:
            lister = getattr(self.ex, "open_orders_strict", None) or self.ex.open_orders
            active = {o.get("orderId") for o in lister(sym)}
        except Exception as e:  # noqa: BLE001
            log.debug("%s: список заявок не получен, тейки не трогаю: %s", sym, e)
            return
        self._tp_checked[t["id"]] = time.time()
        limits = None
        if hasattr(self.ex, "price_limits"):
            try:
                limits = self.ex.price_limits(sym)
            except Exception:  # noqa: BLE001
                limits = None
        changed = False
        close_side = "Sell" if long else "Buy"
        for lvl in plan[hits:]:
            oid = lvl.get("order_id")
            if oid and oid in active:
                continue
            qty = float(lvl["qty"])
            if oid and hasattr(self.ex, "order_status"):
                st = self.ex.order_status(sym, oid)
                if st is None or st.get("status") in ("Filled", "New", "PartiallyFilled"):
                    continue   # статус неизвестен, заявка жива или исполнена (объём учтёт блок частичных тейков): не дублируем
                qty -= float(st.get("filled") or 0)   # сгорела частично исполненной: довыставляем только остаток
            if qty <= 0 or qty > float(pos["qty"]) + 1e-9:
                continue
            price = lvl["price"]
            if limits:
                lo, hi = limits
                tick = float(self.ex.instruments()[sym]["tick"])
                price = min(price, hi - tick) if long else max(price, lo + tick)
            try:
                lvl["order_id"] = self.ex.place_limit(sym, close_side, qty, price)
                changed = True
                log.info("%s: тейк %.2fR перевыставлен по %g (заявка сгорела с прошлой сессии)", sym, lvl["r"], price)
            except Exception as e:  # noqa: BLE001
                log.debug("%s: тейк %.2fR не перевыставлен: %s", sym, lvl["r"], e)
        if changed:
            self.j.update_tp_plan(t["id"], plan)

    def _sma20d(self, sym):
        """20-дневная средняя по завершённым дням (сегодняшний день не считаем), кеш час.
        При сбое данных возвращает прошлое значение (за час средняя почти не меняется); None — средней нет."""
        c = self._sma_cache
        v = c.get(sym)
        if v and time.time() - v[0] < 3600:
            return v[1]
        try:
            d = self.ex.klines(sym, "D", 30)
            closes = d["close"].astype(float)
            if len(closes) < 22:
                self._sma_bad[sym] = ("short", time.time())   # истории меньше 22 дней (новый контракт): средней не будет
                c[sym] = (time.time(), None)
                return None
            last_day = datetime.fromtimestamp(int(d["ts"].iloc[-1]) / 1000, timezone.utc).date()
            closed = closes.iloc[:-1] if last_day >= datetime.now(timezone.utc).date() else closes
            sma = float(closed.iloc[-20:].mean())
        except Exception as e:  # noqa: BLE001
            log.debug("sma20d %s: %s", sym, e)
            self._sma_bad.setdefault(sym, ("fail", time.time()))
            return v[1] if v else None
        self._sma_bad.pop(sym, None)
        c[sym] = (time.time(), sma)
        return sma

    def _sma_unavailable(self, sym):
        """Средней нет по существу (короткая история) или данные не приходят дольше 30 минут."""
        b = self._sma_bad.get(sym)
        return bool(b) and (b[0] == "short" or time.time() - b[1] > 1800)

    def _last_hour_close(self, sym):
        try:
            d = self.ex.klines(sym, "60", 3)
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

    def _resync_stop(self, t):
        """Перевыставить стоп-заявку по цене из журнала на текущий объём позиции (объём стоп-заявки у брокера фиксированный)."""
        try:
            cur = self.j._run("SELECT stop FROM trades WHERE id=?", (t["id"],), "one")["stop"]
            self.ex.set_stop_loss(t["symbol"], cur)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("%s: стоп-заявка не перевыставлена на новый объём: %s", t["symbol"], e)
            return False

    def _pyramid(self, t, pos, part, long, mark):
        """Добор части позиции по рынку; стоп у брокера переставляется на весь объём; журнал: средний вход и объём."""
        sym = t["symbol"]
        if time.time() - self._pyr_checked.get(t["id"], 0) < 900:
            return
        if not self._trend4h_agrees(sym, long):
            self._pyr_checked[t["id"]] = time.time()   # тренд 4ч меняется медленно: перепроверяем раз в 15 минут, а не каждый цикл
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
        except Exception as e:  # noqa: BLE001
            log.warning("%s: добор не удался: %s", sym, e)
            return
        # заявка исполнена: журнал обновляем в любом случае, даже если брокер не ответил на запрос позиции,
        # иначе у брокера 4 лота, в журнале 3, а стоп-заявка стоит на 3
        qty_total = float(t["qty"]) + add_qty
        entry_avg = (float(t["entry"]) * float(t["qty"]) + mark * add_qty) / qty_total
        try:
            time.sleep(2)
            p2 = [p for p in self.ex.positions() if p["symbol"] == sym]
            if p2:
                qty_total, entry_avg = float(p2[0]["qty"]), float(p2[0]["entry"])
        except Exception as e:  # noqa: BLE001
            log.warning("%s: позиция после добора не получена, считаю по заявке: %s", sym, e)
        self.j.update_add(t["id"], qty_total, entry_avg, add_qty, mark)
        t["qty"], t["entry"], t["add_qty"], t["add_entry"] = qty_total, entry_avg, add_qty, mark
        pos["qty"] = qty_total
        moved = False
        if self.s.get("risk.pyramid_stop_breakeven", True):
            # 05.11 Газпром: добор по 109.66 при стопе 111.28 (безубыток первых трёх) превратил ноль в −156.
            # Стоп должен защищать всю позицию: общий безубыток по среднему входу
            be_all = entry_avg * (1 + 0.0015 * (1 if long else -1))
            moved = self._move_stop(t, be_all, "общий безубыток после добора") is True
        if not moved:
            self._resync_stop(t)   # стоп-заявка на весь новый объём
        log.info("%s: добор %g по %g, объём %g, средний вход %g", sym, add_qty, mark, qty_total, entry_avg)
        e0 = float(t.get("entry_init") or t["entry"])
        self.tg.send("\n\n".join([
            f"➕ <b>ДОБОР</b>: {fmt.esc(fmt.label(sym))} {'ЛОНГ' if long else 'ШОРТ'}",
            fmt.block("Почему", f"Сделка в плюсе {((mark - e0) if long else (e0 - mark)) / abs(e0 - t['stop_init']):.2f}R, тренд 4ч подтверждён"),
            fmt.block("Что сделал", f"Добрал {add_qty:g} по {mark:g}; объём {qty_total:g}, средний вход {entry_avg:g}, стоп {t['stop']:g} на весь объём"),
        ]), html=True)

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
        except Exception as e:  # noqa: BLE001
            log.warning("%s: частичное закрытие по вердикту наблюдателя не удалось: %s", sym, e)
            return False
        # заявка исполнена: журнал обновляем в любом случае, иначе закрытая часть была бы принята за исполненный тейк
        left = qty_now - cut
        try:
            time.sleep(2)
            p2 = [p for p in self.ex.positions() if p["symbol"] == sym]
            if p2:
                left = float(p2[0]["qty"])
        except Exception as e:  # noqa: BLE001
            log.warning("%s: позиция после частичного закрытия не получена, считаю по заявке: %s", sym, e)
        self.j.update_cut(t["id"], left, cut)
        t["qty"], t["cut_qty"], pos["qty"] = left, cut, left
        self._resync_stop(t)   # стоп-заявка на прежний объём после сокращения закрыла бы больше, чем осталось
        log.info("%s: закрыто %g из %g по вердикту наблюдателя (take %.2f) при %.2fR, остаток %g", sym, cut, qty_now, float(v.confidence), r_now, left)
        self.tg.send("\n\n".join([
            f"✂️ <b>ЧАСТЬ ПРИБЫЛИ ЗАБРАНА</b>: {fmt.esc(fmt.label(sym))} {'ЛОНГ' if long else 'ШОРТ'}",
            fmt.block("Почему", f"Наблюдатель за драйвером: движение сломано (уверенность {float(v.confidence):.2f})", fmt.esc(str(v.reasoning)[:220])),
            fmt.block("Что сделал", f"Закрыл {cut:g} из {qty_now:g} по рынку около {mark:g} при {r_now:+.2f}R",
                      f"Остаток {left:g} веду дальше, стоп подтягиваю на половину текущей прибыли"),
        ]), html=True)
        return True

    def _move_stop(self, t, new_stop, why, mark=None):
        """True — стоп переставлен; False — без изменений; "closed" — уровень уже пройден ценой, сделка закрыта по рынку.
        Стоп только подтягивается, никогда не ослабляется."""
        long = t["side"] == "Buy"
        cur = self.j._run("SELECT stop FROM trades WHERE id=?", (t["id"],), "one")["stop"]
        better = (new_stop > cur) if long else (new_stop < cur)
        if not better:
            return False
        if mark and ((long and new_stop >= mark) or (not long and new_stop <= mark)):
            # новый уровень уже за ценой: стоп-заявку там брокер исполнит сразу или отклонит (и позиция осталась бы без стопа)
            ok = self._close_now(t, f"{why}: уровень {new_stop:g} уже пройден (цена {mark:g}), закрыл по рынку")
            return "closed" if ok else False
        try:
            self.ex.set_stop_loss(t["symbol"], new_stop)
            self.j.update_stop(t["id"], new_stop)
            t["stop"] = new_stop
            log.info("%s: стоп -> %g (%s)", t["symbol"], new_stop, why)
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("%s: не удалось переставить стоп: %s", t["symbol"], e)
            return False

    # ---------- реакция на новости ----------
    @_locked
    def react_to_events(self, events):
        """Закрыть позиции, против которых вышла важная новость. Возвращает список закрытых сделок."""
        r = self.s.get("risk")
        min_imp = r.get("news_exit_min_importance", 7)
        inst = self.ex.instruments()
        closed = []
        # отложенные ордера против новости снимаем сразу
        for p in self.j.pendings():
            base = self._company(inst.get(p["symbol"], {}).get("base", ""))
            long = p["side"] == "Buy"
            for e in events:
                if e["importance"] < min_imp or e["confidence"] < 0.6:
                    continue
                if not (base in {self._company(a) for a in (e.get("affected") or [])} or (e["market_wide"] and e["importance"] >= 8)):
                    continue
                if (e["direction"] == "bearish" and long) or (e["direction"] == "bullish" and not long):
                    self.cancel_pendings("контр-новость", symbols={p["symbol"]})
                    break
        sectors = r.get("sectors") or {}
        max_names = int(r.get("news_exit_max_names", 4))
        for t in self.j.open_trades():
            base = self._company(inst.get(t["symbol"], {}).get("base", ""))
            long = t["side"] == "Buy"
            sec = next((k for k, v in sectors.items() if base in v), None)
            for e in events:
                if e["importance"] < min_imp or e["confidence"] < 0.6:
                    continue
                contra = (e["direction"] == "bearish" and long) or (e["direction"] == "bullish" and not long)
                if not contra:
                    continue
                raw = e.get("affected") or []
                aff = {self._company(a) for a in raw}   # новость про «Сбер» относится и к позиции в «Сбер-п»
                # геополитический заголовок с девятью тикерами — общерыночный шум, по архиву он цену не предсказывает:
                # 31.10 две такие «контр-новости» закрыли технический лонг Газпрома в минус, и бот тут же вошёл снова
                wide = bool(e.get("market_wide")) or len(raw) > max_names
                specific = (not wide) and base in aff
                sector_hit = (not wide) and sec and sum(1 for a in aff if a in sectors[sec]) >= 2
                if specific or sector_hit:
                    # ночью и в клиринг биржа закрыта: закрытие запоминается и выполняется при открытии, стоп-заявка остаётся
                    if self._close_now(t, f"контр-новость [{e['event_type']}, imp {e['importance']}]: {e['title'][:60]}", defer=True):
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
    def _close_now(self, t, reason, defer=False):
        """Закрыть позицию по рынку. True — сделка закрыта и записана.
        Если торги по бумаге не идут (ночь, клиринг, выходной), НИЧЕГО не отменяем: раньше стоп-заявка и тейки снимались,
        закрытие не проходило, и позиция оставалась без стопа. defer=True — решение разовое (контр-новость, /panic):
        запоминаем его и закрываем при открытии торгов. Остальные причины пересчитываются каждый цикл сами."""
        sym = t["symbol"]
        is_open = True
        if hasattr(self.ex, "trading_open"):
            try:
                is_open = bool(self.ex.trading_open(sym))
            except Exception:  # noqa: BLE001
                is_open = True
        if not is_open:
            self._close_failed(t, reason, defer, "торги по бумаге сейчас не идут")
            return False
        try:
            self.ex.cancel_all(sym)
            self.ex.close_position(sym)
            time.sleep(2)
        except Exception as e:  # noqa: BLE001
            log.error("%s: ошибка закрытия: %s", sym, e)
            self._resync_stop(t)                   # заявки уже могли быть сняты: возвращаем стоп
            self._tp_checked.pop(t["id"], None)    # и тейки при ближайшей проверке
            self._close_failed(t, reason, defer, str(e)[:120])
            return False
        self._finalize(t, reason)
        return True

    def _close_failed(self, t, reason, defer, why):
        sym = t["symbol"]
        if defer:
            if self._pc_set(t, reason):
                log.warning("%s: закрытие отложено (%s): %s", sym, why, reason[:80])
                self.tg.send("\n\n".join([
                    f"⏳ <b>ЗАКРЫТИЕ ОТЛОЖЕНО</b>: {fmt.esc(fmt.label(sym))}",
                    fmt.block("Причина закрытия", fmt.esc(reason[:160])),
                    fmt.block("Почему не сейчас", fmt.esc(why)),
                    fmt.block("Что с позицией", "Стоп-заявка у брокера на месте. Закрою, как только откроются торги")]), html=True)
        elif time.time() - self._close_log.get(t["id"], 0) > 1800:
            self._close_log[t["id"]] = time.time()
            log.warning("%s: закрыть сейчас нельзя (%s), стоп-заявка на месте, повторю: %s", sym, why, reason[:80])

    def _exchange_result(self, t):
        """(pnl, exit, fees) по данным биржи за время жизни сделки, с повторами. None, если биржа не ответила
        или ответила не полностью. pnl уже за вычетом комиссий (и Bybit, и T-Invest считают так)."""
        sym = t["symbol"]
        inst = self.ex.instruments().get(sym) if hasattr(self.ex, "instruments") else {}
        if inst is None:
            return None   # инструмента нет в справочнике (экспирация): результат по цене из журнала
        since = t["opened_ts"] * 1000 - 60000
        price_based = getattr(self.ex, "price_based_pnl", False)
        kw = {}
        if price_based:
            # лимитка могла исполниться раньше, чем бот это заметил: окно шире, результат по ценам входа/выхода.
            # Окно не должно захватывать соседние сделки по той же бумаге: 31.10 закрытие предыдущего Газпрома
            # попадало в расчёт следующего, а при повторном уточнении — и закрытия более поздних сделок
            since = t["opened_ts"] * 1000 - 30 * 60000
            prev = self.j._run("SELECT MAX(closed_ts) AS m FROM trades WHERE symbol=? AND status='closed' AND id<>? AND closed_ts<=?",
                               (sym, t["id"], int(t["opened_ts"]) + 5), "one")
            if prev and prev.get("m"):
                since = max(since, (int(prev["m"]) + 1) * 1000)
            kw = {"entry": t["entry"], "side": t["side"]}
            if t.get("closed_ts"):
                kw["end_ms"] = (int(t["closed_ts"]) + 300) * 1000
        for attempt in range(4):
            try:
                rows = [x for x in self.ex.closed_pnl(sym, start_ms=since, **kw) if x["ts"] >= since]
                if rows and price_based:
                    # сделка закрывалась частями (тейк, разгрузка): пока брокер не отдал все закрывающие операции,
                    # результат неполный — считаем по цене из журнала с пометкой и уточняем позже
                    lots = float(t.get("qty_init") or t["qty"]) + float(t.get("add_qty") or 0)
                    per_lot = float(inst.get("lot") or 1) if inst.get("kind") == "share" else 1.0
                    if float(rows[0].get("qty") or 0) + 1e-6 < lots * per_lot:
                        rows = []
                if rows:
                    return sum(x["pnl"] for x in rows), rows[0]["exit"], sum(x.get("fees") or 0 for x in rows)
                if attempt >= 1:
                    return None   # биржа ответила, но записей нет или они не все (ещё не прошла запись о закрытии)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: closed_pnl (попытка %d): %s", sym, attempt + 1, e)
            time.sleep(2 + attempt * 2)
        return None

    def _finalize(self, t, reason):
        try:
            self.ex.cancel_all(t["symbol"])
        except Exception as e:  # noqa: BLE001
            log.warning("%s: заявки при закрытии не сняты (%s), снимет сверка заявок", t["symbol"], str(e)[:80])
        self._pc_drop(t)
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
        for d in (self._miss, self._tp_checked, self._pyr_checked, self._close_log):
            d.pop(t["id"], None)
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

    def _audit_orders(self):
        """Сверка заявок у брокера с журналом раз в 5 минут. Правило: у каждой сделки ровно одна стоп-заявка на весь объём
        позиции по цене из журнала; тейков не больше позиции; по бумагам без сделки заявок нет. Всё, что не так, исправляется.
        Закрывает целый класс ошибок «журнал думает одно, у брокера стоит другое» (05.11 Газпром и находки аудита)."""
        view_fn = getattr(self.ex, "orders_view", None)
        if not view_fn or time.time() - self._audit_ts < 300:
            return
        self._audit_ts = time.time()
        try:
            view = view_fn()   # при любой ошибке получения списков — исключение: по неполным данным ничего не трогаем
            positions = {p["symbol"]: p for p in self.ex.positions()}
        except Exception as e:  # noqa: BLE001
            log.debug("сверка заявок: списки не получены: %s", e)
            return
        trades = {t["symbol"]: t for t in self.j.open_trades()}
        pend = {p["symbol"] for p in self.j.pendings()}
        inst = self.ex.instruments()
        # 1) заявки по бумагам, где нет ни сделки, ни отложенного входа, ни позиции: остались от закрытой сделки.
        #    Забытая стоп-заявка бессрочная и через неделю открыла бы позицию сама
        for kind in ("stops", "limits"):
            for o in view[kind]:
                sym = o["symbol"]
                if sym in trades or sym in pend or sym in positions:
                    continue
                try:
                    self.ex.cancel_stop(o["id"]) if kind == "stops" else self.ex.cancel_order(sym, o["id"])
                    log.warning("%s: сверка заявок: снята лишняя %s %g по %g (сделки по бумаге нет)", sym,
                                "стоп-заявка" if kind == "stops" else "заявка", o["qty"], o.get("price") or 0)
                except Exception as e:  # noqa: BLE001
                    log.warning("%s: сверка заявок: лишняя заявка не снята: %s", sym, e)
        # 2) по каждой сделке: стоп-заявка и тейки
        pcs = self._pc_all()
        for sym, t in trades.items():
            pos = positions.get(sym)
            if not pos or sym not in inst or pos["side"] != t["side"]:
                continue   # позиции нет или она встречная: этим занимается manage()
            long = t["side"] == "Buy"
            want = "Sell" if long else "Buy"
            tick = float(inst[sym]["tick"])
            stops = [o for o in view["stops"] if o["symbol"] == sym]
            # объём 0 = брокер не прислал поле объёма: по нему не судим, иначе переставляли бы стоп каждые 5 минут
            ok = (len(stops) == 1 and (stops[0]["qty"] <= 0 or abs(stops[0]["qty"] - float(pos["qty"])) < 1e-9) and stops[0]["side"] == want
                  and abs(stops[0]["price"] - float(t["stop"])) <= 1.5 * tick)
            if not ok:
                was = ", ".join(f"{o['qty']:g} по {o['price']:g}" for o in stops) or "нет"
                try:
                    self.ex.set_stop_loss(sym, float(t["stop"]))
                    log.warning("%s: сверка заявок: стоп-заявка приведена к журналу: было [%s], стало %g по %g",
                                sym, was, pos["qty"], t["stop"])
                except Exception as e:  # noqa: BLE001
                    log.warning("%s: сверка заявок: стоп-заявка не исправлена (было [%s]): %s", sym, was, e)
            if str(t["id"]) in pcs:
                continue
            tps = [o for o in view["limits"] if o["symbol"] == sym and o["side"] == want]
            if sum(o["qty"] for o in tps) > float(pos["qty"]) + 1e-9:
                # тейков больше, чем позиция (дубль): исполнившись, они открыли бы встречную позицию. Снимаем все,
                # _refresh_tps выставит заново по плану
                for o in tps:
                    self.ex.cancel_order(sym, o["id"])
                plan = t.get("tp_plan") if isinstance(t.get("tp_plan"), list) else []
                for lvl in plan[int(t.get("tp_hits") or 0):]:
                    lvl["order_id"] = ""
                self.j.update_tp_plan(t["id"], plan)
                self._tp_checked.pop(t["id"], None)
                log.warning("%s: сверка заявок: тейков %g при позиции %g, сняты и будут выставлены заново",
                            sym, sum(o["qty"] for o in tps), pos["qty"])

    @_locked
    def reconcile(self):
        """Подхватить позиции без записи; сверить заявки с журналом; дозаполнить результат сделок, по которым биржа не ответила."""
        self.adopt_orphans()
        try:
            self._audit_orders()
        except Exception as e:  # noqa: BLE001
            log.warning("сверка заявок: %s", e)
        if time.time() - self._recon_ts < 120:
            return
        self._recon_ts = time.time()
        for t in self.j.unreconciled(48):
            res = self._exchange_result(t)
            if res is not None:
                pnl, exit_price, fees = res
                self.j.update_pnl(t["id"], exit_price, pnl, t["close_reason"].split(" (результат")[0])
                if fees:
                    self.j._run("UPDATE trades SET fees=? WHERE id=?", (fees, t["id"]))
                log.info("%s: результат уточнён: %+.2f %s", t["symbol"], pnl, fmt.CUR)
                self.tg.send(fmt.block("ℹ️ Уточнение результата", f"{fmt.esc(fmt.label(t['symbol']))}: {fmt.signed(pnl)} {fmt.CUR}"), html=True)

    @_locked
    def close_all(self, reason):
        """Закрыть всё. Возвращает число закрытого (снятые ордера + закрытые позиции); self.last_deferred — сколько
        позиций закрыть не удалось, потому что торги по ним не идут: они закроются при открытии, стоп-заявки на месте."""
        n = self.cancel_pendings(reason)
        self.last_deferred = 0
        managed = set()
        for t in self.j.open_trades():
            managed.add(t["symbol"])
            if self._close_now(t, reason, defer=True):
                n += 1
            else:
                self.last_deferred += 1
        for p in self.ex.positions():
            if p["symbol"] in managed:
                continue
            try:
                if hasattr(self.ex, "trading_open") and not self.ex.trading_open(p["symbol"]):
                    self.last_deferred += 1
                    continue
                self.ex.cancel_all(p["symbol"])
                self.ex.close_position(p["symbol"])
                n += 1
            except Exception as e:  # noqa: BLE001
                log.error("close_all %s: %s", p["symbol"], e)
        return n
