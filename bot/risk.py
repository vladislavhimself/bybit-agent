"""Риск-менеджер: защищённый резерв прибыли, лимиты убытка, kill-switch, запрет входов вокруг макро,
размер позиции от стопа и волатильности.

Торговый капитал = equity − резерв. Все проценты считаются от торгового капитала.
Резерв только растёт: каждый раз, когда торговый капитал вырос на profit_lock_step_pct от базы,
profit_lock_pct прироста переносится в резерв. Резерв в сделках не участвует никогда."""
import logging
import math
from datetime import datetime, timezone

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

    def check(self, equity: float):
        """Обновить состояние по текущему equity. Возвращает (можно_открывать_новые, причина/статус)."""
        now = datetime.now(timezone.utc)
        dk, wk = now.strftime("%Y-%m-%d"), now.strftime("%G-W%V")
        r = self.s.get("risk")
        t = self.tradable(equity)
        if self.base <= 0:
            self.base = self.peak = self.day_start = self.week_start = t
        if self.day_key != dk:
            self.day_key, self.day_start = dk, t
        if self.week_key != wk:
            self.week_key, self.week_start = wk, t

        # фиксация прибыли в резерв
        locked_now = 0.0
        step = r.get("profit_lock_step_pct", 0)
        if step and self.base > 0 and t >= self.base * (1 + step / 100):
            gain = t - self.base
            locked_now = gain * r.get("profit_lock_pct", 0) / 100
            self.reserve += locked_now
            t = self.tradable(equity)
            self.base = t
            # сдвигаем точки отсчёта, чтобы перенос в резерв не выглядел убытком
            self.peak = max(self.peak - locked_now, t)
            self.day_start -= locked_now
            self.week_start -= locked_now
            log.info("В резерв отложено %.2f USDT, резерв %.2f, торговый капитал %.2f", locked_now, self.reserve, t)

        if t > self.peak:
            self.peak = t
        dd = (self.peak - t) / self.peak * 100 if self.peak > 0 else 0
        day_loss = (self.day_start - t) / self.day_start * 100 if self.day_start > 0 else 0
        week_loss = (self.week_start - t) / self.week_start * 100 if self.week_start > 0 else 0
        self._save()

        if self.halted:
            return False, self.halt_reason
        if dd >= r["kill_switch_drawdown_pct"]:
            self.halt(f"просадка торгового капитала {dd:.1f}% >= {r['kill_switch_drawdown_pct']}% (kill-switch)")
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
    def can_open(self, symbol, open_trades, positions, equity):
        r = self.s.get("risk")
        t = self.tradable(equity)
        if any(x["symbol"] == symbol for x in open_trades) or any(p["symbol"] == symbol for p in positions):
            return False, "уже есть позиция по символу"
        if len(open_trades) >= r["max_open_positions"]:
            return False, f"достигнут лимит позиций {r['max_open_positions']}"
        exposure = sum(p["qty"] * p["mark"] for p in positions)
        if t > 0 and exposure / t >= r["max_total_exposure_x"]:
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
        dist = abs(price - stop)
        if dist <= 0 or price <= 0:
            return None, "нулевое расстояние до стопа"
        stop_pct = dist / price
        qty = risk_amt / dist
        notional = qty * price

        exposure = sum(p["qty"] * p["mark"] for p in positions)
        cap = max(0.0, t * r["max_total_exposure_x"] - exposure)
        cap = min(cap, t * r["max_total_exposure_x"] / 2)
        if notional > cap:
            qty, notional = cap / price, cap
        if notional <= 0:
            return None, "нет свободного лимита экспозиции"

        # Плечо: ликвидация должна быть минимум вдвое дальше стопа
        lev = math.ceil(notional / t) + 1
        max_lev_by_stop = math.floor(0.9 / (stop_pct * 2)) if stop_pct > 0 else r["max_leverage"]
        lev = int(max(1, min(lev, r["max_leverage"], inst["max_leverage"], max(1, max_lev_by_stop))))
        if notional / t > lev:
            qty = lev * t / price * 0.98
        if not self.ex.qty_ok(symbol, qty, price):
            return None, f"объём ниже минимального (qty={qty:.6f})"
        return float(self.ex.fmt_qty(symbol, qty)), lev
