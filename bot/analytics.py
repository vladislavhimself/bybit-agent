"""Аналитика сделок и теневых сделок: что работает, что нет, с цифрами.
Разрезы: причина отказа (помогает ли фильтр), час дня, режим рынка, сила сигнала, инструмент, новостные/технические,
ширина стопа (MAE победителей: стоп выбивает шумом или нет), куда идёт цена после входа (MFE).
Используется командой /analytics и разбором (review) как структурированный вход для модели."""
import json
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import numpy as np

from . import fmt
from .shadow import REASON_RU

MSK = timezone(timedelta(hours=3))


def _bucket(x, edges, labels):
    for e, l in zip(edges, labels):
        if x < e:
            return l
    return labels[-1]


def _agg(rows, key_fn, val_fn):
    g = defaultdict(list)
    for r in rows:
        v = val_fn(r)
        if v is None:
            continue
        g[key_fn(r)].append(v)
    out = {}
    for k, vals in g.items():
        a = np.array(vals, dtype=float)
        out[k] = {"n": int(len(a)), "avg_r": round(float(a.mean()), 2), "win": round(float((a > 0).mean()), 2),
                  "sum_r": round(float(a.sum()), 1)}
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["n"]))


class Analytics:
    def __init__(self, journal, settings):
        self.j, self.s = journal, settings

    # ---------- данные ----------
    def shadows(self, days):
        rows = [dict(r) for r in self.j.shadows_resolved(int(time.time()) - days * 86400)]
        for r in rows:
            r["r"] = r["r_pullback"] if r.get("r_pullback") is not None else r.get("r_market")
            r["hour"] = datetime.fromtimestamp(r["ts"], MSK).hour
        return rows

    def trades(self, days):
        cutoff = int(time.time()) - days * 86400
        rows = [dict(r) for r in self.j.closed_trades(500) if (r["closed_ts"] or 0) >= cutoff]
        for r in rows:
            r["hour"] = datetime.fromtimestamp(r["opened_ts"], MSK).hour
            r["hold_h"] = round(((r["closed_ts"] or r["opened_ts"]) - r["opened_ts"]) / 3600, 1)
            r["stop_atr"] = round(abs(r["entry"] - r["stop_init"]) / r["atr"], 2) if r.get("atr") else None
        return rows

    # ---------- разрезы ----------
    def report(self, days=30):
        sh, tr = self.shadows(days), self.trades(days)
        rep = {"days": days, "shadows_n": len(sh), "trades_n": len(tr)}
        if sh:
            def _fsplit(r):
                try:
                    f = (json.loads(r.get('comps') or '{}') if isinstance(r.get('comps'), str) else (r.get('comps') or {})).get('factor')
                except Exception:
                    f = None
                if f is None or abs(f) < 0.15:
                    return 'фактор нейтрален/нет'
                return 'фактор за' if (f > 0) == (r.get('side') == 'long') else 'фактор против'
            rep["shadow_by_factor"] = _agg(sh, _fsplit, lambda r: r["r"]) if sh else {}

            rep["shadow_by_reason"] = _agg(sh, lambda r: r["reason"], lambda r: r["r"])
            rep["shadow_by_hour"] = _agg(sh, lambda r: f"{r['hour']:02d}", lambda r: r["r"])
            rep["shadow_by_regime"] = _agg(sh, lambda r: r.get("regime") or "?", lambda r: r["r"])
            rep["shadow_by_strength"] = _agg(sh, lambda r: _bucket(r["strength"], [0.4, 0.5, 0.6, 0.7], ["<0.4", "0.4-0.5", "0.5-0.6", "0.6-0.7", "0.7+"]), lambda r: r["r"])
            rep["shadow_by_symbol"] = dict(list(_agg(sh, lambda r: r["symbol"], lambda r: r["r"]).items())[:12])
            rep["shadow_news_vs_ta"] = _agg(sh, lambda r: "новостные" if r.get("news_driven") else "технические", lambda r: r["r"])
            # ширина стопа: насколько глубоко против входа уходила цена у тех, кто в итоге в плюсе
            win = [r for r in sh if r["r"] is not None and r["r"] > 0 and r.get("mae") is not None and r.get("stop_dist")]
            if win:
                mae_frac = np.array([abs(r["mae"]) / r["stop_dist"] for r in win])
                rep["stop_width"] = {"winners_n": len(win), "mae_share_of_stop_median": round(float(np.median(mae_frac)), 2),
                                     "winners_touching_70pct_of_stop": round(float((mae_frac > 0.7).mean()), 2)}
            mfe = [r["mfe"] / r["stop_dist"] for r in sh if r.get("mfe") is not None and r.get("stop_dist")]
            if mfe:
                rep["mfe_r_median"] = round(float(np.median(mfe)), 2)
        if tr:
            rep["trades"] = {"n": len(tr), "avg_r": round(float(np.mean([r["pnl_r"] for r in tr])), 2),
                             "win": round(float(np.mean([r["pnl"] > 0 for r in tr])), 2),
                             "pnl": round(float(sum(r["pnl"] for r in tr)), 2), "fees": round(float(sum(r.get("fees") or 0 for r in tr)), 2),
                             "avg_hold_h": round(float(np.mean([r["hold_h"] for r in tr])), 1)}
            rep["trades_by_tier"] = _agg(tr, lambda r: r.get("tier") or "?", lambda r: r["pnl_r"])
            rep["trades_by_hour"] = _agg(tr, lambda r: f"{r['hour']:02d}", lambda r: r["pnl_r"])
            rep["trades_by_symbol"] = _agg(tr, lambda r: r["symbol"], lambda r: r["pnl_r"])
            rep["trades_news_vs_ta"] = _agg(tr, lambda r: "новостные" if r.get("news_driven") else "технические", lambda r: r["pnl_r"])
            rep["trades_by_exit"] = _agg(tr, lambda r: (r.get("close_reason") or "?")[:40], lambda r: r["pnl_r"])
            mfe = [r["mfe_r"] for r in tr if r.get("mfe_r") is not None]
            if mfe:
                rep["trades_mfe_r_median"] = round(float(np.median(mfe)), 2)
                losers_mfe = [r["mfe_r"] for r in tr if r.get("mfe_r") is not None and r["pnl"] <= 0]
                if losers_mfe:
                    rep["losers_mfe_r_median"] = round(float(np.median(losers_mfe)), 2)   # убыточные, которые были в плюсе
            st = [r["stop_atr"] for r in tr if r.get("stop_atr")]
            if st:
                rep["stop_atr_median"] = round(float(np.median(st)), 2)
        return rep

    # ---------- выводы ----------
    def findings(self, rep):
        """Короткие выводы с цифрами, по правилам (без модели), для Telegram и как подсказка разбору."""
        out = []
        n_min = (self.s.get("shadow") or {}).get("min_n_verdict", 10)
        for k, v in (rep.get("shadow_by_reason") or {}).items():
            if v["n"] >= n_min:
                name = REASON_RU.get(k, k)
                if v["avg_r"] >= 0.10:
                    out.append(f"фильтр «{name}» мешает: отклонённые {v['n']} сигналов дали бы {v['avg_r']:+.2f}R в среднем")
                elif v["avg_r"] <= -0.05:
                    out.append(f"фильтр «{name}» работает: отклонённые {v['n']} дали бы {v['avg_r']:+.2f}R")
        sw = rep.get("stop_width")
        if sw and sw["winners_n"] >= n_min and sw["winners_touching_70pct_of_stop"] >= 0.3:
            out.append(f"стоп узкий: {sw['winners_touching_70pct_of_stop'] * 100:.0f}% прибыльных теней уходили глубже 70% стопа до разворота")
        if rep.get("losers_mfe_r_median") is not None and rep["losers_mfe_r_median"] >= 0.5:
            out.append(f"убыточные сделки были в плюсе на {rep['losers_mfe_r_median']:.1f}R медианно — выходить раньше или подтягивать стоп агрессивнее")
        for key, title in (("shadow_by_hour", "тени"), ("trades_by_hour", "сделки")):
            hours = {k: v for k, v in (rep.get(key) or {}).items() if v["n"] >= n_min}
            if len(hours) >= 3:
                best = max(hours.items(), key=lambda kv: kv[1]["avg_r"])
                worst = min(hours.items(), key=lambda kv: kv[1]["avg_r"])
                if best[1]["avg_r"] - worst[1]["avg_r"] >= 0.3:
                    out.append(f"{title} по часам (МСК): лучший час {best[0]} ({best[1]['avg_r']:+.2f}R), худший {worst[0]} ({worst[1]['avg_r']:+.2f}R)")
        nv = rep.get("shadow_news_vs_ta") or {}
        if all(k in nv and nv[k]["n"] >= n_min for k in ("новостные", "технические")):
            out.append(f"тени: новостные {nv['новостные']['avg_r']:+.2f}R ({nv['новостные']['n']}), технические {nv['технические']['avg_r']:+.2f}R ({nv['технические']['n']})")
        return out

    def text(self, days=30):
        rep = self.report(days)
        parts = [f"📊 <b>АНАЛИТИКА за {days} дн.</b>"]
        t = rep.get("trades")
        if t:
            parts.append(fmt.block("Сделки", f"{t['n']}: средний {t['avg_r']:+.2f}R, в плюс {t['win'] * 100:.0f}%, итог {fmt.signed(t['pnl'])} {fmt.CUR} (комиссии {fmt.money(t['fees'], 2)})",
                                   f"Среднее удержание {t['avg_hold_h']} ч" + (f", стоп {rep['stop_atr_median']} ATR" if rep.get("stop_atr_median") else "")))
            if rep.get("trades_by_tier"):
                parts.append(fmt.block("По размеру", *[f"{fmt.TIER_RU.get(k, k)}: {v['n']}, {v['avg_r']:+.2f}R, в плюс {v['win'] * 100:.0f}%" for k, v in rep["trades_by_tier"].items()]))
            if rep.get("trades_by_exit"):
                parts.append(fmt.block("По причине выхода", *[f"{fmt.esc(k)}: {v['n']}, {v['avg_r']:+.2f}R" for k, v in list(rep["trades_by_exit"].items())[:6]]))
        else:
            parts.append(fmt.block("Сделки", "закрытых нет"))
        if rep.get("shadow_by_factor"):
            parts.append(fmt.block("Тени по макрофактору (Brent/рубль/RGBI)", *[f"{k}: {v['n']}, {v['avg_r']:+.2f}R, в плюс {v['win'] * 100:.0f}%" for k, v in rep["shadow_by_factor"].items()]))
        if rep.get("shadow_by_reason"):
            parts.append(fmt.block("Тени по причине отказа", *[f"{REASON_RU.get(k, k)}: {v['n']}, {v['avg_r']:+.2f}R, в плюс {v['win'] * 100:.0f}%" for k, v in rep["shadow_by_reason"].items()]))
            parts.append(fmt.block("Тени по силе сигнала", *[f"{k}: {v['n']}, {v['avg_r']:+.2f}R" for k, v in sorted(rep["shadow_by_strength"].items())]))
            hours = sorted(rep["shadow_by_hour"].items())
            parts.append(fmt.block("Тени по часам МСК", " ".join(f"{k}:{v['avg_r']:+.1f}({v['n']})" for k, v in hours)))
        else:
            parts.append(fmt.block("Теневые сделки", f"проверенных нет (всего {rep['shadows_n']})"))
        f = self.findings(rep)
        parts.append(fmt.block("Выводы", *[f"• {fmt.esc(x)}" for x in f]) if f else fmt.block("Выводы", "пока мало данных для уверенных выводов"))
        return "\n\n".join(parts)

    def review_json(self, days=30):
        rep = self.report(days)
        rep["findings"] = self.findings(rep)
        return json.dumps(rep, ensure_ascii=False)
