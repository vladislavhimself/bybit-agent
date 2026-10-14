"""Сведение сигналов: новости + теханализ + позиционирование толпы -> оценка и уровень уверенности."""
import time
from dataclasses import dataclass, field


@dataclass
class Signal:
    symbol: str
    direction: str            # long | short
    strength: float           # 0..1
    tier: str                 # low | medium | high | conviction
    components: dict
    reasons: list = field(default_factory=list)
    regime: str = ""
    news_driven: bool = False
    events: list = field(default_factory=list)


def news_score(base, events, min_importance, now=None, playbook=None):
    now = now or time.time()
    total, reasons, used = 0.0, [], []
    for e in events:
        if e["importance"] < min_importance or e["direction"] == "neutral":
            continue
        direct = base in e["affected"]
        if not direct and not e["market_wide"]:
            continue
        age_h = (now - e["ts"]) / 3600
        if age_h > e["horizon_hours"] or age_h < 0:
            continue
        decay = 1 - age_h / e["horizon_hours"]
        w = 1.0 if direct else (0.7 if base in ("BTC", "ETH") else 0.4)
        sign = 1 if e["direction"] == "bullish" else -1
        contrib = sign * e["importance"] / 10 * e["confidence"] * decay * w
        if playbook is not None:
            contrib *= playbook.adjust(e["event_type"], e["direction"])
        total += contrib
        e = dict(e, age_h=round(age_h, 1))
        used.append(e)
        reasons.append(f"новость[{e['event_type']}, imp {e['importance']}, {e['direction']}, {age_h:.1f}ч]: {e['title'][:70]}")
    return max(-1.0, min(1.0, total)), reasons, used


def positioning_score(ticker, oi_hist, ta):
    score, reasons = 0.0, []
    f = ticker["funding"]  # ставка за 8 часов, доля (0.0001 = 0.01%)
    if f >= 0.0005:
        score -= 0.6; reasons.append(f"фандинг {f*100:.3f}% — переполнены лонги")
    elif f >= 0.0002:
        score -= 0.3; reasons.append(f"фандинг {f*100:.3f}% — перекос в лонг")
    elif f <= -0.0005:
        score += 0.6; reasons.append(f"фандинг {f*100:.3f}% — переполнены шорты")
    elif f <= -0.0002:
        score += 0.3; reasons.append(f"фандинг {f*100:.3f}% — перекос в шорт")
    if len(oi_hist) >= 12 and oi_hist[0] > 0:
        oi_chg = (oi_hist[-1] / oi_hist[0] - 1) * 100
        if abs(oi_chg) >= 5:
            # рост OI по тренду = подтверждение, рост OI против движения цены = накопление контр-позиций
            same = 1 if (oi_chg > 0) == (ta["ret_24h"] > 0) else -1
            score += 0.3 * same * (1 if ta["ret_24h"] > 0 else -1)
            reasons.append(f"OI {oi_chg:+.1f}% за сутки при цене {ta['ret_24h']:+.1f}%")
    return max(-1.0, min(1.0, score)), reasons


def build_signal(symbol, base, ta, ticker, oi_hist, events, params, now=None, playbook=None):
    p = params
    ns, nreasons, used = news_score(base, events, p["min_news_importance"], now,
                                    playbook if p.get("use_playbook", True) else None)
    ps, preasons = positioning_score(ticker, oi_hist, ta)
    ts = ta["score"]
    nw, tw, pw = p["news_weight"], p["ta_weight"], p["positioning_weight"]
    # Аддитивное сведение: сильная новость сама по себе даёт вход, ТА и позиционирование усиливают или гасят её.
    # Чистый ТА без подтверждения до порога почти не дотягивает (бэктест показал, что у него нет преимущества).
    total = max(-1.0, min(1.0, nw * ns + tw * ts + pw * ps))

    comps = {"news": round(ns, 3), "ta": round(ts, 3), "positioning": round(ps, 3), "total": round(total, 3)}
    nonzero = [v for v in (ns, ts, ps) if abs(v) > 0.05]
    sign = 1 if total > 0 else -1
    agreement = (sum(1 for v in nonzero if (v > 0) == (sign > 0)) / len(nonzero)) if nonzero else 0.0
    strength = abs(total) * (0.7 + 0.3 * agreement)
    if ta.get("timing_15m", 0) != 0 and (ta["timing_15m"] > 0) != (sign > 0):
        strength *= 0.85  # 15-минутный импульс против нас: ждём лучшей точки

    reasons = nreasons + preasons + [
        f"ТА: режим {ta['regime']}, тренд1ч {ta['trend_1h']:+d}, тренд4ч {ta['trend_4h']:+d}, "
        f"пробой {ta['breakout']:+d}, RSI {ta['rsi']}, ADX {ta['adx']}, ATR {ta['atr_pct']}%"
    ]

    # Во флэте торгуем только от сильных новостей
    if ta["regime"] == "range" and abs(ns) < 0.3:
        return None
    # Не торгуем против тренда 4ч без весомой новости
    if ta["trend_4h"] != 0 and (ta["trend_4h"] > 0) != (sign > 0) and abs(ns) < 0.5:
        return None

    # Погоня за движением: если за сутки цена уже прошла >15% в нашу сторону и новости нет, не входим.
    if abs(ns) < 0.3 and abs(ta["ret_24h"]) > 15 and (ta["ret_24h"] > 0) == (sign > 0):
        return None

    t = p["entry_threshold"]
    if strength < t:
        return None
    if strength >= p["conviction_threshold"] and agreement >= 0.99 and len(nonzero) >= 2:
        tier = "conviction"
    elif strength >= t + 0.18:
        tier = "high"
    elif strength >= t + 0.08:
        tier = "medium"
    else:
        tier = "low"
    if ta["regime"] == "high_vol":
        tier = {"conviction": "high", "high": "medium", "medium": "low", "low": "low"}[tier]
        reasons.append("экстремальная волатильность: размер понижен")
    if abs(ns) < 0.05 and tier in ("high", "conviction"):
        tier = "medium"  # чистый теханализ без новостного подтверждения не получает повышенный размер
        reasons.append("без новостного подтверждения: размер не выше medium")

    return Signal(symbol=symbol, direction="long" if sign > 0 else "short", strength=round(strength, 3),
                  tier=tier, components=comps, reasons=reasons, regime=ta["regime"],
                  news_driven=abs(ns) >= 0.3, events=used)
