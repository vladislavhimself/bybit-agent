"""Теневые сделки: каждый отклонённый сигнал записывается как виртуальная сделка и через несколько часов
проверяется на реальных 5-минутных свечах по тем же правилам выхода, что у бота. Так бот учится на сигналах,
которые не взял, без риска для счёта: видно, какой фильтр экономит деньги, а какой отсекает прибыльные входы."""
import json
import logging
import time

import numpy as np
import pandas as pd

log = logging.getLogger("shadow")

FEE_TAKER, FEE_MAKER, SLIP = 0.00055, 0.0002, 0.0002

# Причины отказа, которые записываются как теневые сделки (если без этого фильтра сигнал прошёл бы)
FILTER_REASONS = {"range", "high_vol", "counter_4h", "chase", "extension"}

REASON_RU = {
    "range": "флэт", "high_vol": "резкая волатильность", "counter_4h": "против тренда 4ч",
    "chase": "ушло >15%", "extension": "поздний вход (2+ ATR)", "weak": "чуть слабее порога",
    "pullback_not_filled": "откат не случился", "one_per_scan": "второй вход в ту же сторону",
    "limit_cooldown": "пауза по монете", "limit_max_positions": "лимит позиций",
    "limit_same_direction": "лимит позиций в сторону", "limit_direction_risk": "лимит риска по стороне",
    "limit_per_event": "лимит сделок на новость", "limit_exposure": "лимит объёма",
    "trend_gate": "против тренда рынка", "stale_news": "новость слабая или несвежая", "paper_mode": "режим наблюдения",
}


def simulate(bars, side, ref_price, atr, risk, mode="market", pull_atr=0.5, wait_min=60, stop_dist=None,
             force=False):
    """bars: DataFrame 5m (ts в мс, open, high, low, close) с момента сигнала.
    mode: market = вход по открытию первой свечи; pullback = лимитный ордер на pull_atr ATR, ждём wait_min минут.
    Возвращает dict: done (сделка завершилась на имеющихся данных), filled, r (в R после комиссий), exit, mfe, mae.
    force=True закрывает остаток по последней цене, если данные кончились."""
    if bars is None or len(bars) == 0:
        return {"done": False, "filled": None, "r": None, "exit": "", "mfe": 0.0, "mae": 0.0}
    t = bars["ts"].to_numpy(dtype="int64")
    o, hi, lo, cl = (bars[c].to_numpy(dtype=float) for c in ("open", "high", "low", "close"))
    n = len(t)
    sg = 1 if side == "long" else -1
    # --- вход ---
    if mode == "market":
        entry, start, fee_in = o[0] * (1 + sg * SLIP), 0, FEE_TAKER
    else:
        lim = ref_price - sg * pull_atr * atr
        wait_bars = max(1, int(wait_min // 5))
        start = None
        for j in range(min(n, wait_bars)):
            if (sg > 0 and lo[j] <= lim) or (sg < 0 and hi[j] >= lim):
                start = j
                break
        if start is None:
            done = n >= wait_bars or force
            return {"done": done, "filled": False if done else None, "r": None, "exit": "не исполнился" if done else "",
                    "mfe": 0.0, "mae": 0.0}
        entry, fee_in = lim, FEE_MAKER
    R = stop_dist if stop_dist else risk.get("stop_atr_mult", 2.5) * atr
    if R <= 0:
        return {"done": True, "filled": False, "r": None, "exit": "нет ATR", "mfe": 0.0, "mae": 0.0}
    stop = entry - sg * R
    qty, realized, mfe, mae, mfe_t = 1.0, 0.0, 0.0, 0.0, t[start]
    tps = [[lv["r"], lv["part"], False] for lv in (risk.get("tp_levels") or [])]
    be_r = risk.get("breakeven_r", 0.7)
    gb_r, gb_min = risk.get("giveback_r", 0.6), risk.get("giveback_min_peak_r", 0.8)
    stall_h, trail_r, max_h = risk.get("stall_hours", 12), risk.get("trail_after_r", 1.0), risk.get("max_hours_in_trade", 48)
    exit_reason = ""

    def close_at(price, why):
        nonlocal realized, qty, exit_reason
        ex = price * (1 - sg * SLIP)
        realized += qty * (sg * (ex - entry) / R - FEE_TAKER * ex / R)
        qty, exit_reason = 0.0, why

    for j in range(start, n):
        adv = lo[j] if sg > 0 else hi[j]
        mae = min(mae, sg * (adv - entry) / R)
        # 1) стоп раньше тейков (консервативно)
        if (sg > 0 and lo[j] <= stop) or (sg < 0 and hi[j] >= stop):
            why = "стоп" if sg * (stop - entry) < 0 else ("безубыток" if abs(stop - entry) / R < 0.05 else "трейлинг")
            close_at(stop, why)
            break
        # 2) частичные тейки
        fav = hi[j] if sg > 0 else lo[j]
        for tp in tps:
            if not tp[2] and sg * (fav - entry) >= tp[0] * R:
                realized += tp[1] * (tp[0] - FEE_MAKER * (entry + sg * tp[0] * R) / R)
                qty -= tp[1]
                tp[2] = True
        if qty <= 1e-9:
            qty, exit_reason = 0.0, "все тейки"
            break
        cf = sg * (fav - entry) / R
        if cf > mfe:
            mfe, mfe_t = cf, t[j]
        r_now = sg * (cl[j] - entry) / R
        hours = (t[j] + 300_000 - t[start]) / 3_600_000
        # 3) выходы на закрытии свечи, как в боте
        if mfe >= gb_min and r_now <= mfe - gb_r:
            close_at(cl[j], "защита прибыли")
            break
        if hours >= max_h and r_now < 0.5:
            close_at(cl[j], "тайм-стоп")
            break
        if 0.1 <= r_now < 1.0 and hours >= stall_h and (t[j] - mfe_t) / 3_600_000 >= stall_h:
            close_at(cl[j], "застряла")
            break
        # 4) перенос стопа к следующей свече
        if mfe >= be_r:
            be = entry * (1 + sg * 0.0015)
            stop = max(stop, be) if sg > 0 else min(stop, be)
        if mfe >= gb_min:
            gb = entry + sg * (mfe - gb_r) * R
            stop = max(stop, gb) if sg > 0 else min(stop, gb)
        if r_now >= trail_r:
            lad = entry + sg * (int(r_now) - 1 + 0.2) * R
            stop = max(stop, lad) if sg > 0 else min(stop, lad)
    if qty > 0 and force:
        close_at(cl[-1], "конец окна")
    done = qty <= 0
    return {"done": done, "filled": True, "r": round(realized - fee_in * entry / R, 4) if done else None,
            "exit": exit_reason, "mfe": round(mfe, 3), "mae": round(mae, 3)}


class ShadowBook:
    def __init__(self, settings, journal, exchange):
        self.s, self.j, self.ex = settings, journal, exchange

    def cfg(self):
        return self.s.get("shadow") or {}

    def wants(self, reason, strength, threshold):
        """Писать ли теневую сделку: сигнал прошёл бы, если бы не этот фильтр, или почти дотянул до порога."""
        c = self.cfg()
        if not c.get("enabled", True) or threshold is None:
            return False
        if reason in FILTER_REASONS:
            return strength >= threshold
        if reason == "weak":
            return strength >= threshold - c.get("weak_margin", 0.10)
        return reason == "pullback_not_filled" or reason == "one_per_scan" or reason.startswith("limit_")

    def record(self, symbol, side, reason, strength, threshold, price, atr, regime="", comps=None, ext=0.0,
               news_driven=False, event_key="", ts=None, pull_atr=None):
        c = self.cfg()
        if not c.get("enabled", True) or not price or not atr:
            return None
        ts = int(ts or time.time())
        if self.j.shadow_exists(symbol, side, reason, ts - c.get("dedup_hours", 12) * 3600):
            return None
        e = self.s.get("entry") or {}
        if pull_atr is None:
            pull_atr = e.get("pullback_atr_news", 0.15) if news_driven else e.get("pullback_atr", 0.5)
        stop_dist = self.s.get("risk.stop_atr_mult", 2.5) * atr
        sid = self.j.add_shadow({
            "ts": ts, "symbol": symbol, "side": side, "reason": reason, "strength": round(float(strength), 3),
            "threshold": threshold, "price": price, "atr": atr, "stop_dist": stop_dist, "pull_atr": pull_atr,
            "regime": regime, "comps": json.dumps(comps or {}, ensure_ascii=False), "ext": round(float(ext or 0), 2),
            "news_driven": int(bool(news_driven)), "event_key": event_key or "",
            "next_check_ts": ts + int(c.get("first_check_hours", 3) * 3600),
        })
        log.info("Теневая сделка %s %s (%s, сила %.2f)", symbol, side, REASON_RU.get(reason, reason), strength)
        return sid

    def _bars(self, symbol, start_ms, end_ms):
        """5-минутные свечи от сигнала до end_ms через общий интерфейс биржи (Bybit и Мосбиржа)."""
        need = int((end_ms - start_ms) / 300_000) + 3
        df = self.ex.klines(symbol, "5", min(max(need, 10), 1000), end=int(end_ms))
        if df is None or len(df) == 0:
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close"])
        df = df[["ts", "open", "high", "low", "close"]].astype(float)
        df["ts"] = df["ts"].astype("int64")
        # свеча, начавшаяся до сигнала, не считается: вход только после сигнала
        return df[df["ts"] >= start_ms].reset_index(drop=True)

    def resolve_due(self, now=None):
        """Проверить несколько созревших теневых сделок (бережём лимит запросов биржи)."""
        c = self.cfg()
        now = int(now or time.time())
        rows = self.j.shadows_due(now, c.get("resolve_per_cycle", 2))
        risk = self.s.get("risk") or {}
        wait = (self.s.get("entry") or {}).get("wait_minutes", 60)
        horizon = c.get("horizon_hours", 72) * 3600
        for row in rows:
            try:
                start_ms = (row["ts"] // 300) * 300 * 1000 + 300_000   # первая полная 5-минутная свеча после сигнала
                end_ms = min(now * 1000, (row["ts"] + horizon) * 1000)
                bars = self._bars(row["symbol"], start_ms, end_ms)
                force = now >= row["ts"] + horizon
                m = simulate(bars, row["side"], row["price"], row["atr"], risk, "market",
                             stop_dist=row["stop_dist"], force=force)
                p = simulate(bars, row["side"], row["price"], row["atr"], risk, "pullback",
                             pull_atr=row["pull_atr"], wait_min=wait, stop_dist=row["stop_dist"], force=force)
                if (m["done"] and p["done"]) or force:
                    self.j.resolve_shadow(row["id"], m["r"], p["r"] if p["filled"] else None, m["exit"],
                                          m["mfe"], m["mae"], now)
                else:
                    self.j.shadow_next_check(row["id"], now + int(c.get("recheck_hours", 3) * 3600))
            except Exception as e:  # noqa: BLE001
                log.warning("Теневая сделка %s: %s", row["id"], e)
                self.j.shadow_next_check(row["id"], now + 1800)

    # ---------- статистика ----------
    def stats(self, days=7, since_ts=None):
        rows = self.j.shadows_resolved(since_ts if since_ts is not None else int(time.time()) - days * 86400)
        out = {}
        for r in rows:
            d = out.setdefault(r["reason"], {"n": 0, "market": [], "pull": [], "pull_tries": 0})
            d["n"] += 1
            if r["r_market"] is not None:
                d["market"].append(r["r_market"])
            d["pull_tries"] += 1
            if r["r_pullback"] is not None:
                d["pull"].append(r["r_pullback"])
        res = {}
        for k, d in out.items():
            res[k] = {
                "n": d["n"],
                "market_avg": round(float(np.mean(d["market"])), 3) if d["market"] else None,
                "market_win": round(float(np.mean([x > 0 for x in d["market"]])), 2) if d["market"] else None,
                "pull_filled": len(d["pull"]),
                "pull_avg": round(float(np.mean(d["pull"])), 3) if d["pull"] else None,
                "sum_market": round(float(np.sum(d["market"])), 2) if d["market"] else 0.0,
            }
        return res

    def verdict(self, st):
        """Помогает ли фильтр: отклонённые им сделки в среднем в минус = помогает."""
        n_min = self.cfg().get("min_n_verdict", 10)
        avg = st["pull_avg"] if st["pull_filled"] >= n_min else st["market_avg"]
        if st["n"] < n_min or avg is None:
            return "мало данных"
        if avg <= -0.05:
            return "фильтр помогает"
        if avg >= 0.10:
            return "фильтр мешает"
        return "нейтрально"

    def summary_text(self, days=7, top=5, detailed=False):
        st = self.stats(days)
        open_n = self.j.shadows_open_count()
        if not st:
            return f"за {days} дн. проверенных теневых сделок нет (ждут проверки: {open_n})"
        parts = []
        for k, v in sorted(st.items(), key=lambda kv: -kv[1]["n"])[:top]:
            avg = v["market_avg"]
            line = f"{REASON_RU.get(k, k)}: {v['n']}, в среднем {avg:+.2f}R" if avg is not None else f"{REASON_RU.get(k, k)}: {v['n']}"
            if detailed:
                pa = f"{v['pull_avg']:+.2f}R" if v["pull_avg"] is not None else "нет"
                line += (f" (в плюс {v['market_win'] * 100:.0f}%, с откатом исполнилось {v['pull_filled']}, "
                         f"их результат {pa}) — {self.verdict(v)}")
            else:
                line += f", {self.verdict(v)}"
            parts.append(line)
        sep = "\n• " if detailed else "; "
        head = f"за {days} дн., ждут проверки {open_n}" + (":\n• " if detailed else ": ")
        return head + sep.join(parts)

    def review_block(self, since_ts):
        """Текст для разбора: статистика по причинам за 30 дней и новые результаты с прошлого разбора."""
        st30 = self.stats(30)
        new = self.j.shadows_resolved(since_ts)
        if not st30 and not new:
            return "", 0
        rows = [{"reason": r["reason"], "symbol": r["symbol"], "side": r["side"], "strength": r["strength"],
                 "ext_atr": r["ext"], "regime": r["regime"], "r_market": r["r_market"], "r_pullback": r["r_pullback"],
                 "exit": r["exit_market"]} for r in new[:60]]
        text = ("Теневые сделки (отклонённые сигналы, проверенные на реальных свечах по тем же правилам выхода).\n"
                f"Статистика по причинам отказа за 30 дней: {json.dumps(st30, ensure_ascii=False)}\n"
                f"Новые результаты с прошлого разбора ({len(new)}): {json.dumps(rows, ensure_ascii=False)}")
        return text, len(new)
