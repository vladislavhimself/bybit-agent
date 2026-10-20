"""Риск-менеджер: защищённый резерв прибыли, лимиты убытка, kill-switch, запрет входов вокруг макро,
размер позиции от стопа и волатильности.

Торговый капитал = equity − резерв. Все проценты считаются от торгового капитала.
Резерв только растёт: каждый раз, когда торговый капитал вырос на profit_lock_step_pct от базы,
profit_lock_pct прироста переносится в резерв. Резерв в сделках не участвует никогда."""
import logging
import math
import time
from datetime import datetime, timezone

from . import fmt

log = logging.getLogger("risk")


class RiskManager:
    def __init__(self, settings, exchange, journal, calendar=None):
        self.s, self.ex, self.j, self.cal = settings, exchange, journal, calendar
        st = journal.kv_get("risk_state", {})
        self.reserve = st.get("reserve", 0.0)      # защищённая прибыль, USDT
        self.base = st.get("base", 0.0)            # торговый капитал на момент последней фиксации
        self.peak = st.get("peak", 0.0)            # пик торгового капитала
        self.day_key = st.get("day_key")
        self.day_start = st.get("day_start", 0.0)
        self.week_key = st.get("week_key")
        self.week_start = st.get("week_start", 0.0)
        self.halted = st.get("halted", False)
        self.halt_reason = st.get("halt_reason", "")

    def _save(self):
        self.j.kv_set("risk_state", {
            "reserve": self.reserve, "base": self.base, "peak": self.peak,
            "day_key": self.day_key, "day_start": self.day_start,
            "week_key": self.week_key, "week_start": self.week_start,
            "halted": self.halted, "halt_reason": self.halt_reason,
        })

    # ---------- состояние ----------
    def tradable(self, equity):
        """Торговый капитал = (лимит + изменение equity с момента старта, если лимит задан) − резерв прибыли."""
        cap = float(self.s.get("risk.capital_cap_usd") or 0)
        if cap > 0:
            start = self.j.kv_get("start_equity")
            if start is None:
                start = equity
                self.j.kv_set("start_equity", start)
            virtual = cap + (equity - start)
            return max(0.0, min(virtual, equity) - self.reserve)
        return max(0.0, equity - self.reserve)

    def halt(self, reason):
        self.halted, self.halt_reason = True, reason
        self._save()
        log.warning("ОСТАНОВКА: %s", reason)

    def resume(self, equity):
        self.halted, self.halt_reason = False, ""
        t = self.tradable(equity)
        self.peak = self.base = t
        self.day_start = self.week_start = t
        self._save()

    def check(self, equity: float, realized: float | None = None):
        """Обновить состояние по текущему equity (с нереализованным результатом). realized = кошелёк без
        открытых позиций: только по нему фиксируем прибыль в резерв, чтобы колебания открытой позиции
        не превращались в «прибыль». Возвращает (можно_открывать_новые, причина/статус)."""
        now = datetime.now(timezone.utc)
        dk, wk = now.strftime("%Y-%m-%d"), now.strftime("%G-W%V")
        r = self.s.get("risk")
        cap = float(r.get("capital_cap_usd") or 0)
        if self.j.kv_get("cap_used") != cap:
            # лимит капитала изменился: старые точки отсчёта (пик, база, день, неделя) бессмысленны
            self.j.kv_set("cap_used", cap)
            if cap > 0:
                self.j.kv_set("start_equity", equity)
            t0 = self.tradable(equity)
            self.base = self.peak = self.day_start = self.week_start = t0
            self.halted, self.halt_reason = False, ""
            log.info("Лимит капитала %s: точки отсчёта сброшены, торговый капитал %.2f", cap or "снят", t0)
        t = self.tradable(equity)
        if self.base <= 0:
            self.base = self.peak = self.day_start = self.week_start = t
        if self.day_key != dk:
            self.day_key, self.day_start = dk, t
        if self.week_key != wk:
            self.week_key, self.week_start = wk, t

        # фиксация прибыли в резерв: только по реализованному результату (кошелёк), не по открытым позициям
        locked_now = 0.0
        step = r.get("profit_lock_step_pct", 0)
        t_real = self.tradable(realized) if realized is not None else t
        if step and self.base > 0 and t_real >= self.base * (1 + step / 100):
            gain = t_real - self.base
            locked_now = gain * r.get("profit_lock_pct", 0) / 100
            self.reserve += locked_now
            t = self.tradable(equity)
            self.base = self.tradable(realized) if realized is not None else t
            # сдвигаем точки отсчёта, чтобы перенос в резерв не выглядел убытком
            self.peak = max(self.peak - locked_now, t)
            self.day_start -= locked_now
            self.week_start -= locked_now
            log.info("В резерв отложено %.2f %s, резерв %.2f, торговый капитал %.2f", locked_now, fmt.CUR, self.reserve, t)

        if t > self.peak:
            self.peak = t
        dd = (self.peak - t) / self.peak * 100 if self.peak > 0 else 0
        day_loss = (self.day_start - t) / self.day_start * 100 if self.day_start > 0 else 0
        week_loss = (self.week_start - t) / self.week_start * 100 if self.week_start > 0 else 0
        self._save()

        if self.halted:
            return False, self.halt_reason
        if dd >= r["kill_switch_drawdown_pct"]:
            self.halt(f"торговый капитал просел на {dd:.1f}% от пика {self.peak:.0f} {fmt.CUR} (порог "
                      f"{r['kill_switch_drawdown_pct']}%), новые сделки остановлены")
            return False, self.halt_reason
        if day_loss >= r["daily_loss_limit_pct"]:
            return False, f"дневной лимит убытка: -{day_loss:.1f}%"
        if week_loss >= r["weekly_loss_limit_pct"]:
            return False, f"недельный лимит убытка: -{week_loss:.1f}%"
        if self.cal:
            black, ev = self.cal.blackout(r.get("macro_blackout_min_before", 0), r.get("macro_blackout_min_after", 0))
            if black:
                return False, f"окно макро-события: {ev['title'][:60]}"
        status = f"dd {dd:.1f}% | день {-day_loss:+.1f}% | неделя {-week_loss:+.1f}%"
        if locked_now:
            status += f" | в резерв +{locked_now:.2f}"
        return True, status

    def metrics(self, equity):
        t = self.tradable(equity)
        dd = (self.peak - t) / self.peak * 100 if self.peak > 0 else 0
        day = (t - self.day_start) / self.day_start * 100 if self.day_start > 0 else 0
        week = (t - self.week_start) / self.week_start * 100 if self.week_start > 0 else 0
        return {"tradable": round(t, 2), "reserve": round(self.reserve, 2), "drawdown_pct": round(dd, 2),
                "day_pct": round(day, 2), "week_pct": round(week, 2), "peak": round(self.peak, 2),
                "halted": self.halted, "halt_reason": self.halt_reason}

    # ---------- проверки перед входом ----------
    def can_open(self, symbol, open_trades, positions, equity, direction=None, event_key="", tier=None):
        """open_trades: открытые сделки + отложенные ордера (у каждого symbol, side, entry, stop_init, qty, event_key).
        При отказе код причины кладётся в self.last_reject (для теневых сделок)."""
        self.last_reject = ""
        r = self.s.get("risk")
        t = self.tradable(equity)
        if any(x["symbol"] == symbol for x in open_trades) or any(p["symbol"] == symbol for p in positions):
            self.last_reject = "symbol_busy"
            return False, "уже есть позиция или ордер по символу"
        cd = r.get("symbol_cooldown_hours", 0)
        if cd and direction:
            last = self.j.last_entry_ts(symbol, "Buy" if direction == "long" else "Sell")
            if last and time.time() - last < cd * 3600:
                self.last_reject = "cooldown"
                return False, f"по монете был вход в эту сторону меньше {cd} ч назад"
        if len(open_trades) >= r["max_open_positions"]:
            self.last_reject = "max_positions"
            return False, f"достигнут лимит позиций {r['max_open_positions']}"
        # Альткоины ходят вместе: много позиций в одну сторону = одна большая ставка на рынок
        if direction:
            side = "Buy" if direction == "long" else "Sell"
            same_side = [x for x in open_trades if x["side"] == side]
            if len(same_side) >= r.get("max_same_direction", 3):
                self.last_reject = "same_direction"
                return False, f"уже {len(same_side)} позиции в ту же сторону (лимит {r.get('max_same_direction', 3)})"
            # суммарный риск по направлению, если все стопы сработают
            risk_now = sum(abs((x.get("entry") or 0) - (x.get("stop_init") or x.get("stop") or 0)) * (x.get("qty_init") or x.get("qty") or 0)
                           for x in same_side)
            new_risk = t * r["risk_per_trade_pct"].get(tier or "low", 0.4) / 100
            cap = t * r.get("max_direction_risk_pct", 2.5) / 100
            if risk_now + new_risk > cap:
                self.last_reject = "direction_risk"
                return False, (f"риск по направлению {risk_now + new_risk:.2f} {fmt.CUR} превысит лимит {cap:.2f} "
                               f"({r.get('max_direction_risk_pct', 2.5)}% капитала)")
        if event_key:
            n_ev = sum(1 for x in open_trades if (x.get("event_key") or "") == event_key)
            if n_ev >= r.get("max_trades_per_event", 2):
                self.last_reject = "per_event"
                return False, f"по этой новости уже {n_ev} сделки (лимит {r.get('max_trades_per_event', 2)})"
        exposure = sum(p["qty"] * p["mark"] for p in positions)
        if t > 0 and exposure / t >= r["max_total_exposure_x"]:
            self.last_reject = "exposure"
            return False, f"суммарная экспозиция {exposure / t:.1f}x >= лимита"
        return True, ""

    def size(self, symbol, equity, price, stop, tier, positions):
        """Вернуть (qty, leverage) или (None, причина). Размер считается от торгового капитала."""
        r = self.s.get("risk")
        inst = self.ex.instruments()[symbol]
        t = self.tradable(equity)
        if t <= 0:
            return None, "торговый капитал исчерпан, резерв не трогаем"
        risk_amt = t * r["risk_per_trade_pct"][tier] / 100
        if datetime.now(timezone.utc).weekday() >= 5:
            risk_amt *= r.get("weekend_risk_mult", 1.0)
        dist = abs(price - stop)
        if dist <= 0 or price <= 0:
            return None, "нулевое расстояние до стопа"
        stop_pct = dist / price
        mult = float(inst.get("mult") or 1.0)   # рублей/долларов за 1 пункт цены на 1 лот
        qty = risk_amt / (dist * mult)
        notional = qty * price * mult

        exposure = sum(p["qty"] * p["mark"] * float(self.ex.instruments().get(p["symbol"], {}).get("mult") or 1.0)
                       for p in positions)
        cap = max(0.0, t * r["max_total_exposure_x"] - exposure)
        cap = min(cap, t * r["max_total_exposure_x"] / 2)
        if notional > cap:
            qty, notional = cap / (price * mult), cap
        if notional <= 0:
            return None, "нет свободного лимита экспозиции"
        # фьючерсы Мосбиржи: гарантийное обеспечение не должно съесть весь торговый капитал
        if hasattr(self.ex, "margin_per_lot") and inst.get("kind") == "future":
            m = self.ex.margin_per_lot(symbol, "Buy" if stop < price else "Sell")
            if m:
                max_lots = int(t * r.get("max_margin_share", 0.6) / m)
                if max_lots < 1:
                    return None, f"ГО одного контракта {m:.0f} больше {r.get('max_margin_share', 0.6) * 100:.0f}% капитала"
                qty = min(qty, max_lots)
                notional = qty * price * mult

        # Плечо: ликвидация должна быть минимум вдвое дальше стопа
        lev = math.ceil(notional / t) + 1
        max_lev_by_stop = math.floor(0.9 / (stop_pct * 2)) if stop_pct > 0 else r["max_leverage"]
        lev = int(max(1, min(lev, r["max_leverage"], inst["max_leverage"], max(1, max_lev_by_stop))))
        if notional / t > lev:
            qty = lev * t / (price * mult) * 0.98
        if not self.ex.qty_ok(symbol, qty, price):
            # целые лоты (Мосбиржа): берём 1 лот, если его риск не больше max_single_lot_risk_pct капитала
            one_lot_risk = dist * mult * float(inst.get("lot_min", 1) or 1)
            hard = t * r.get("max_single_lot_risk_pct", 0) / 100
            if inst.get("qty_step") == 1 and hard and one_lot_risk <= hard and self.ex.qty_ok(symbol, 1, price):
                return 1.0, lev
            return None, f"объём ниже минимального (qty={qty:.6f}, риск одного лота {one_lot_risk:.0f})"
        return float(self.ex.fmt_qty(symbol, qty)), lev
