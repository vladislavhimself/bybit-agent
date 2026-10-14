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

    def open(self, sig, ta, equity, positions):
        r = self.s.get("risk")
        price = ta["close"]
        dist = ta["atr"] * r["stop_atr_mult"]
        long = sig.direction == "long"
        stop = price - dist if long else price + dist
        side = "Buy" if long else "Sell"
        qty, lev = self.risk.size(sig.symbol, equity, price, stop, sig.tier, positions)
        if qty is None:
            log.info("%s: пропуск, %s", sig.symbol, lev)
            return None
        try:
            self.ex.set_leverage(sig.symbol, lev)
            order_id = self.ex.place_market(sig.symbol, side, qty, sl=stop)
        except Exception as e:  # noqa: BLE001
            log.error("%s: ошибка открытия: %s", sig.symbol, e)
            self.tg.send(f"⚠️ Не удалось открыть {sig.symbol} {sig.direction}: {e}")
            return None
        entry = price
        for _ in range(5):
            time.sleep(1)
            pos = [p for p in self.ex.positions() if p["symbol"] == sig.symbol]
            if pos:
                entry, qty = pos[0]["entry"], pos[0]["qty"]
                break
        # стоп и тейки пересчитываем от фактической цены входа
        stop = entry - dist if long else entry + dist
        try:
            self.ex.set_stop_loss(sig.symbol, stop)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: не удалось уточнить стоп: %s", sig.symbol, e)
        plan = self._tp_plan(sig.symbol, qty, entry, dist, long)
        close_side = "Sell" if long else "Buy"
        for lvl in plan:
            try:
                lvl["order_id"] = self.ex.place_limit(sig.symbol, close_side, lvl["qty"], lvl["price"])
            except Exception as e:  # noqa: BLE001
                log.warning("%s: тейк %.2fR не выставлен: %s", sig.symbol, lvl["r"], e)
        final_tp = plan[-1]["price"] if plan else entry + dist * 2.5 * (1 if long else -1)
        tid = self.j.open_trade({
            "symbol": sig.symbol, "side": side, "qty": qty, "entry": entry, "stop": stop, "tp": final_tp,
            "tier": sig.tier, "score": sig.components["total"], "confidence": sig.strength,
            "reasons": sig.reasons, "regime": sig.regime, "atr": ta["atr"], "leverage": lev,
            "order_id": order_id, "news_driven": sig.news_driven, "tp_plan": plan,
        })
        risk_pct = r["risk_per_trade_pct"][sig.tier]
        tradable = self.risk.tradable(equity)
        self.tg.send(fmt.entry_message(sig, entry, qty, lev, stop, plan, risk_pct, abs(entry - stop) * qty,
                                       tradable, equity, ta))
        return tid

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
                    self.tg.send(f"💰 ЧАСТИЧНЫЙ ТЕЙК: {fmt.base_of(sym)}, тейк {new_hits} исполнен по {mark:g}, "
                                 f"остаток {pos['qty']:g} {fmt.base_of(sym)}, стоп переносится в безубыток")
                    if hits == 0 and r.get("breakeven_after_tp1"):
                        be = t["entry"] * (1 + 0.0015 * (1 if long else -1))
                        self._move_stop(t, be, "безубыток после первого тейка")
                t["qty"], t["tp_hits"] = pos["qty"], new_hits

            # тайм-стоп: сделка не идёт
            if hours >= r["max_hours_in_trade"] and r_now < 0.5:
                self._close_now(t, f"тайм-стоп {hours:.0f}ч, R={r_now:.2f}")
                closed.append(t)
                continue

            # трейлинг остатка ступенями по R
            if r_now >= r["trail_after_r"]:
                step = int(r_now)
                new_stop = t["entry"] + (step - 1 + 0.2) * risk_dist * (1 if long else -1)
                self._move_stop(t, new_stop, f"трейлинг R={r_now:.2f}")
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
        for t in self.j.open_trades():
            base = inst.get(t["symbol"], {}).get("base", "")
            long = t["side"] == "Buy"
            for e in events:
                if e["importance"] < min_imp or e["confidence"] < 0.6:
                    continue
                direct = base in e["affected"]
                wide = e["market_wide"] and e["importance"] >= 8
                if not (direct or wide):
                    continue
                contra = (e["direction"] == "bearish" and long) or (e["direction"] == "bullish" and not long)
                if contra:
                    self._close_now(t, f"контр-новость [{e['event_type']}, imp {e['importance']}]: {e['title'][:60]}")
                    closed.append(t)
                    break
        return closed

    # ---------- закрытие ----------
    def _close_now(self, t, reason):
        try:
            self.ex.cancel_all(t["symbol"])
            self.ex.close_position(t["symbol"])
            time.sleep(2)
        except Exception as e:  # noqa: BLE001
            log.error("%s: ошибка закрытия: %s", t["symbol"], e)
            return
        self._finalize(t, reason)

    def _finalize(self, t, reason):
        self.ex.cancel_all(t["symbol"])
        pnl, exit_price = 0.0, t["entry"]
        try:
            since = t["opened_ts"] * 1000 - 60000
            rows = [x for x in self.ex.closed_pnl(t["symbol"], start_ms=since) if x["ts"] >= since]
            if rows:
                pnl = sum(x["pnl"] for x in rows)
                exit_price = rows[0]["exit"]
        except Exception as e:  # noqa: BLE001
            log.warning("%s: closed_pnl: %s", t["symbol"], e)
        self.j.close_trade(t["id"], exit_price, pnl, reason)
        risk = abs(t["entry"] - t["stop_init"]) * (t.get("qty_init") or t["qty"])
        pnl_r = pnl / risk if risk > 0 else 0
        try:
            tradable = self.risk.tradable(self.ex.equity()[0])
        except Exception:  # noqa: BLE001
            tradable = None
        self.tg.send(fmt.close_message(t, pnl, pnl_r, reason, tradable))

    def close_all(self, reason):
        n = 0
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
