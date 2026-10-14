"""Исторический отклик рынка на типы событий (data/playbook.json, строится scripts/build_playbook.py).
Используется как априорная оценка для модели и как поправка к вкладу новости в сигнал."""
import json

from .config import DATA

PATH = DATA / "playbook.json"
PATH_STOCKS = DATA / "playbook_stocks.json"
PATH_RULES = DATA / "ta_rules.json"
PATH_FOMC = DATA / "fomc_hourly.json"
PATH_REGIMES = DATA / "regimes.json"


def _load(p):
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


class Playbook:
    def __init__(self):
        self.data = _load(PATH)
        self.stocks = _load(PATH_STOCKS)
        self.rules = _load(PATH_RULES)
        self.fomc = _load(PATH_FOMC)
        self.regimes = _load(PATH_REGIMES)

    def prior(self, event_type, direction):
        """Вернуть статистику для (тип, направление) или None."""
        return self.data.get("stats", {}).get(f"{event_type}:{direction}")

    def adjust(self, event_type, direction):
        """Множитель к вкладу новости: 0.6..1.3 по исторической доле отработавших событий."""
        p = self.prior(event_type, direction)
        if not p or p["n"] < 4:
            return 1.0
        hit = p["hit_24h"]
        return max(0.6, min(1.3, 0.6 + hit * 0.9))  # hit 0.5 -> 1.05, 0.8 -> 1.32, 0.3 -> 0.87

    def text(self):
        parts = []
        st = self.data.get("stats", {})
        if st:
            lines = ["Реакция BTC на типы событий 2020-2025 (часовые данные; медиана за 24ч / 72ч, доля отработавших):"]
            for k, v in sorted(st.items(), key=lambda kv: -kv[1]["n"]):
                lines.append(f"- {k}: n={v['n']}, 24ч {v['med_24h']:+.1f}%, 72ч {v['med_72h']:+.1f}%, "
                             f"отработало {v['hit_24h'] * 100:.0f}%, типичный размах 1ч {v['mean_abs_1h']:.1f}%")
            parts.append("\n".join(lines))
        for key, title in (("spx", "S&P 500 1973-2025"), ("btc_daily", "BTC 2013-2020, дневные")):
            s2 = self.stocks.get(key, {}).get("stats", {})
            if s2:
                lines = [f"Реакция {title} по типам событий (медиана 1д / 5д / 20д, доля отработавших за 5д):"]
                for k, v in sorted(s2.items(), key=lambda kv: -kv[1]["n"]):
                    lines.append(f"- {k}: n={v['n']}, 1д {v['med_1d']:+.1f}%, 5д {v['med_5d']:+.1f}%, "
                                 f"20д {v['med_20d']:+.1f}%, отработало {v['hit_5d'] * 100:.0f}%")
                parts.append("\n".join(lines))
        f = self.fomc.get("stats")
        if f:
            parts.append(
                f"FOMC и BTC (часовые данные, {f['n']} решений 2022-2026): средний ход за 1ч {f['mean_abs_1h']:.1f}%, "
                f"за 24ч {f['mean_abs_24h']:.1f}%, размах первых 4ч {f['mean_range_4h']:.1f}%; рост через сутки в "
                f"{f['up_share_24h'] * 100:.0f}% случаев; движение первого часа разворачивалось к суткам в "
                f"{f['reversal_share'] * 100:.0f}% случаев (первый импульс после ФРС ненадёжен). "
                f"Повышения ставки: медиана 24ч {f['hikes']['med_24h']:+.1f}% (n={f['hikes']['n']}); "
                f"без изменения: {f['holds']['med_24h']:+.1f}% (n={f['holds']['n']}); "
                f"снижения: {f['cuts']['med_24h']:+.1f}% (n={f['cuts']['n']}).")
        rg = self.regimes
        if rg:
            lines = ["Режимы на дневных данных (доходность через 20д и преимущество над базой):"]
            for key in ("spx_after_vix", "btc_after_vix", "btc_cross"):
                for s in rg.get(key, []):
                    lines.append(f"- {s['label']}: n={s['n']}, 20д {s['mean20']:+.1f}% ({s['hit20'] * 100:.0f}%), "
                                 f"преимущество {s['edge20']:+.1f}%")
            corr = rg.get("btc_corr_daily", {})
            if corr:
                lines.append("- корреляция дневных доходностей BTC: " + "; ".join(
                    f"{k}: с S&P {v['spx']:+.2f}, с долларом {v['dxy']:+.2f}" for k, v in corr.items()))
            dd = rg.get("btc_drawdowns_30")
            if dd:
                lines.append(f"- просадки BTC глубже 30%: {dd['n']} раз, медианная глубина {dd['median_depth']:.0f}%, "
                             f"медиана до восстановления {dd['median_days_to_recover']:.0f} дней")
            parts.append("\n".join(lines))
        for key in ("SPX", "BTC"):
            r = self.rules.get(key)
            if r and r.get("rules"):
                lines = [f"Правила ТА на истории {r['label']} (дневные; доходность через 20д, доля роста, "
                         f"преимущество над базой {r['base']['mean20']:+.2f}%):"]
                top = sorted(r["rules"].items(), key=lambda kv: -abs(kv[1]["edge20"]))[:8]
                for k, v in top:
                    lines.append(f"- {k}: n={v['n']}, 20д {v['mean20']:+.2f}% ({v['hit20'] * 100:.0f}%), "
                                 f"преимущество {v['edge20']:+.2f}%")
                parts.append("\n".join(lines))
        return "\n\n".join(parts)
