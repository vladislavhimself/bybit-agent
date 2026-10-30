"""Еженедельный перерасчёт параметров на истории (walk-forward), вместо «режима наблюдения».

Раз в неделю бот: 1) дотягивает часовую историю по бумагам из data/history через брокера; 2) гоняет симулятор
research/moex_sim.py по сетке параметров (стоп, безубыток, растяжение, пауза, флэтовые шорты, время в сделке);
3) выбирает лучший набор на обучающем окне (последние 2 года без последних 3 месяцев) по t-статистике
среднего R; 4) проверяет его на последних 3 месяцах, которых подбор не видел (вне выборки); 5) применяет через
Settings.set_learned (в границах TUNABLE_BOUNDS) только если вне выборки набор не хуже текущего и в плюсе;
6) присылает отчёт в Telegram. Команда /wf запускает вручную."""
import itertools
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone

import pandas as pd

from . import fmt
from .config import DATA

log = logging.getLogger("walkforward")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "research"))

GRID = {
    "signals.entry_threshold": [0.50, 0.55, 0.60],   # 7 лет: 0.45 -> PF 1.03, 0.55 -> PF 1.30; модель-ревьюер опускала до 0.45
    "risk.stop_atr_mult": [2.0, 2.5],
    "risk.breakeven_r": [0.7, 1.0],
    "signals.max_extension_atr": [2.5, 3.0, 3.5],
    "risk.symbol_cooldown_hours": [3, 6],
    "signals.range_shorts": [1, 0],
    "risk.max_hours_in_trade": [72, 96],
}


class WalkForward:
    def __init__(self, settings, journal, exchange, tg):
        self.s, self.j, self.ex, self.tg = settings, journal, exchange, tg
        self.lock = threading.Lock()
        self.hist = DATA / "history"
        self.last_path = DATA / "walkforward_last.json"

    def cfg(self):
        return self.s.get("learning.walkforward") or {}

    # ---------- история ----------
    def symbols(self):
        inst = self.ex.instruments()
        syms = []
        for p in self.hist.glob("*_60.csv"):
            sym = p.name[:-7]
            if sym in inst:
                syms.append(sym)
        return sorted(syms)

    def update_history(self, max_symbols=40):
        """Дотянуть свежие часовые свечи через брокера (ISS с иностранных и дата-центровых адресов не отвечает)."""
        n_ok = 0
        for sym in self.symbols()[:max_symbols]:
            p = self.hist / f"{sym}_60.csv"
            try:
                df = pd.read_csv(p)
                last = int(df.ts.max())
                last_s = last // 1000 if last > 1e11 else last
                hours = int((time.time() - last_s) / 3600) + 3
                if hours < 2:
                    continue
                new = self.ex.klines(sym, "60", min(hours, 900))
                if new is None or new.empty:
                    continue
                new = new.copy()
                new["ts"] = (new["ts"] // 1000) if last <= 1e11 else new["ts"]   # в CSV Мосбиржи ts в секундах
                new = new[new.ts > last][["ts", "open", "high", "low", "close", "volume", "turnover"]]
                if len(new):
                    pd.concat([df, new]).drop_duplicates("ts").sort_values("ts").to_csv(p, index=False)
                    n_ok += 1
                time.sleep(0.4)
            except Exception as e:  # noqa: BLE001
                log.warning("история %s: %s", sym, e)
        log.info("walk-forward: история обновлена по %d бумагам", n_ok)
        return n_ok

    # ---------- перебор ----------
    def _params_to_sim(self, p):
        gates = {"ext_long": p["signals.max_extension_atr"],
                 "ext_short": self.s.get("signals.max_extension_atr_short") or p["signals.max_extension_atr"],
                 "range_short": bool(p["signals.range_shorts"]), "busy": int(p["risk.symbol_cooldown_hours"]),
                 "thresh": float(p["signals.entry_threshold"])}
        tiers = tuple(tuple(x) for x in (self.s.get("risk.giveback_tiers") or [(1.5, 1.5), (3.0, 1.0), (5.0, 0.6)]))
        exits = dict(stop_mult=p["risk.stop_atr_mult"], be_r=p["risk.breakeven_r"], max_h=int(p["risk.max_hours_in_trade"]), tiers=tiers)
        return gates, exits

    def current_params(self):
        return {k: self.s.get(k) for k in GRID}

    def run(self, apply=True, notify=True):
        if not self.lock.acquire(blocking=False):
            return "перерасчёт уже идёт"
        try:
            return self._run(apply, notify)
        finally:
            self.lock.release()

    def _run(self, apply, notify):
        import moex_sim as ms
        c = self.cfg()
        t0 = time.time()
        try:
            self.update_history()
        except Exception as e:  # noqa: BLE001
            log.warning("walk-forward: обновление истории: %s", e)
        syms = self.symbols()
        is_days, oos_days = int(c.get("is_days", 730)), int(c.get("oos_days", 90))
        feats = ms.load(str(self.hist), syms, is_days + oos_days + 30)
        if len(feats) < 5:
            return f"мало истории: {len(feats)} бумаг"
        split = pd.Timestamp.utcnow().tz_localize(None) - pd.Timedelta(days=oos_days)
        results = []
        keys = list(GRID)
        for combo in itertools.product(*[GRID[k] for k in keys]):
            p = dict(zip(keys, combo))
            gates, exits = self._params_to_sim(p)
            t = ms.run(feats, gates, **exits)
            is_s, oos_s = ms.stats(t[t.t < split]), ms.stats(t[t.t >= split])
            results.append((p, is_s, oos_s))
        cur = self.current_params()
        cur_row = next((r for r in results if all(abs(float(r[0][k]) - float(cur[k] or 0)) < 1e-9 for k in keys)), None)
        if cur_row is None:
            gates, exits = self._params_to_sim({k: (cur[k] if cur[k] is not None else GRID[k][0]) for k in keys})
            t = ms.run(feats, gates, **exits)
            cur_row = ({k: cur[k] for k in keys}, ms.stats(t[t.t < split]), ms.stats(t[t.t >= split]))
        min_n = int(c.get("min_is_trades", 150))
        cands = [r for r in results if r[1]["n"] >= min_n]
        best = max(cands, key=lambda r: r[1]["score"]) if cands else cur_row
        ok_oos = (best[2]["n"] >= 10 and best[2]["avg_r"] > 0 and best[2]["pf"] >= 1.0
                  and best[2]["avg_r"] >= cur_row[2]["avg_r"] - float(c.get("oos_tolerance_r", 0.03)))
        changes = {k: v for k, v in best[0].items() if cur[k] is None or abs(float(v) - float(cur[k])) > 1e-9}
        applied, msgs = [], []
        if apply and ok_oos and changes:
            for k, v in changes.items():
                ok, msg = self.s.set_learned(k, v, max_step=None, source="walkforward")
                msgs.append(msg)
                if ok:
                    applied.append(k)
        report = {"ts": int(time.time()), "symbols": len(feats), "is_days": is_days, "oos_days": oos_days,
                  "current": {"params": cur_row[0], "is": cur_row[1], "oos": cur_row[2]},
                  "best": {"params": best[0], "is": best[1], "oos": best[2]}, "ok_oos": ok_oos,
                  "changes": changes, "applied": applied, "msgs": msgs, "seconds": round(time.time() - t0)}
        self.last_path.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        text = self.text(report)
        if notify:
            self.tg.send(text, html=True)
        log.info("walk-forward: %s", json.dumps({k: report[k] for k in ("ok_oos", "changes", "applied", "seconds")}, ensure_ascii=False))
        return text

    # ---------- отчёт ----------
    def text(self, rep=None):
        if rep is None:
            if not self.last_path.exists():
                return "перерасчёт ещё не выполнялся, запусти /wf"
            rep = json.loads(self.last_path.read_text(encoding="utf-8"))
        import moex_sim as ms
        short = {"signals.entry_threshold": "порог силы", "risk.stop_atr_mult": "стоп ATR", "risk.breakeven_r": "безубыток R", "signals.max_extension_atr": "растяжение ATR",
                 "risk.symbol_cooldown_hours": "пауза ч", "signals.range_shorts": "флэт-шорты", "risk.max_hours_in_trade": "макс. часов"}
        def pline(p):
            return ", ".join(f"{short.get(k, k)} {p[k]:g}" if isinstance(p[k], (int, float)) else f"{short.get(k, k)} {p[k]}" for k in GRID)
        cur, best = rep["current"], rep["best"]
        when = datetime.fromtimestamp(rep["ts"], timezone.utc).strftime("%d.%m %H:%M UTC")
        parts = [f"🧪 <b>ПЕРЕРАСЧЁТ НА ИСТОРИИ</b> ({when}, {rep['symbols']} бумаг, обучение {rep['is_days']} дн., проверка {rep['oos_days']} дн.)",
                 fmt.block("Текущие параметры", fmt.esc(pline(cur["params"])),
                           "обучение: " + fmt.esc(ms.fmt_stats(cur["is"])), "вне выборки: " + fmt.esc(ms.fmt_stats(cur["oos"]))),
                 fmt.block("Лучший набор на обучении", fmt.esc(pline(best["params"])),
                           "обучение: " + fmt.esc(ms.fmt_stats(best["is"])), "вне выборки: " + fmt.esc(ms.fmt_stats(best["oos"])))]
        if not rep["changes"]:
            parts.append(fmt.block("Решение", "текущие параметры и есть лучшие — без изменений"))
        elif rep["ok_oos"] and rep["applied"]:
            parts.append(fmt.block("Решение", "применено: " + fmt.esc(", ".join(f"{short.get(k, k)} → {rep['changes'][k]}" for k in rep["applied"]))))
        elif rep["ok_oos"]:
            parts.append(fmt.block("Решение", "набор прошёл проверку, но применение выключено"))
        else:
            parts.append(fmt.block("Решение", "лучший набор не подтвердился вне выборки — оставляю текущие (защита от подгонки)"))
        return "\n\n".join(parts)
