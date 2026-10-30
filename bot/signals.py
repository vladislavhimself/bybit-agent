"""Сведение сигналов: новости + теханализ + позиционирование толпы -> оценка и уровень уверенности."""
import re
import time
from dataclasses import dataclass, field

from . import psychology


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
    event_key: str = ""       # новость, на которой построен сигнал (для лимита сделок на одно событие)


def news_score(base, events, min_importance, now=None, playbook=None, df1h=None, psych_cfg=None, mode=None):
    """mode="stock_long_only": в счёт идут только бычьи новости по конкретной бумаге. Архив Мосбиржи 2024-2026
    (25 791 событие): общерыночные и медвежьи новости направление не предсказывают (попадание 48-50%, ход ~0),
    плюс есть только у бычьих новостей по бумаге важностью 7+ (+0.14% за 4 ч, +0.33% за сутки)."""
    now = now or time.time()
    total, reasons, used = 0.0, [], []
    for e in events:
        if e["importance"] < min_importance or e["direction"] == "neutral":
            continue
        direct = base in e["affected"]
        if not direct and not e["market_wide"]:
            continue
        # заголовок про геополитику с девятью тикерами в affected — по сути общерыночный, даже если модель не поставила флаг
        wide = e.get("market_wide") or len(e.get("affected") or []) >= 5
        if mode == "stock_long_only" and (wide or not direct or e["direction"] != "bullish"):
            continue
        age_h = (now - e["ts"]) / 3600
        if age_h > e["horizon_hours"] or age_h < 0:
            continue
        decay = 1 - age_h / e["horizon_hours"]
        w = 1.0 if direct else (0.7 if base in ("BTC", "ETH") else 0.4)
        sign = 1 if e["direction"] == "bullish" else -1
        contrib = sign * e["importance"] / 10 * e["confidence"] * decay * w
        if e.get("market_wide") and base not in (e.get("affected") or []):
            # общерыночная новость (инфляция, переговоры) для бумаги, которую она не называет: половинный вес,
            # и слабые (<6) такие новости сами по себе вход не создают
            if e["importance"] < 6:
                continue
            contrib *= 0.5
        if playbook is not None:
            contrib *= playbook.adjust(e["event_type"], e["direction"])
        if psych_cfg and df1h is not None and direct:
            k1, r1 = psychology.priced_in(e, df1h, psych_cfg)
            k2, r2 = psychology.reaction(e, df1h, psych_cfg, now)
            k3, r3 = psychology.typical_move_done(e, df1h, playbook)
            contrib *= k1 * k2 * min(k3, 1.0 if k2 < 0 else k3)
            reasons += [r for r in (r1, r2, r3) if r]
        cr = e.get("contrarian_risk") or 0.0
        if cr >= 0.5:
            contrib *= (1 - 0.5 * cr)
            reasons.append(f"психология: модель видит риск обратной реакции {cr:.1f}")
        total += contrib
        e = dict(e, age_h=round(age_h, 1))
        used.append(e)
        label = {"listing": "листинг", "delisting": "делистинг", "hack": "взлом", "collapse": "крах",
                 "regulation": "регулятор", "macro": "макро", "etf": "ETF", "partnership": "партнёрство",
                 "whale": "киты", "upgrade": "обновление", "outage": "сбой", "legal": "суд",
                 "token_unlock": "разблокировка", "earnings": "отчёт"}.get(e["event_type"], "событие")
        text = e.get("reasoning") if e["event_type"] == "whale" else e.get("title", "")
        text = " ".join(re.sub(r"[^\w\s$#%().,:;!?'\"/+\-]", "", text or "").split())[:90]
        reasons.append(f"новость ({label}, важность {e['importance']}, {age_h:.1f}ч назад): {text}")
    return max(-1.0, min(1.0, total)), reasons, used


def positioning_score(ticker, oi_hist, ta, whale_net=0.0, mood=0.0):
    score, reasons = 0.0, []
    if abs(mood) >= 0.15:
        score += mood
        reasons.append(f"настроение рынка (страх/жадность, стейблкоины): {mood:+.2f}")
    if abs(whale_net) >= 0.1:
        score += 0.5 * whale_net
        reasons.append(f"потоки китов за 24ч: {'накопление' if whale_net > 0 else 'давление продаж'} ({whale_net:+.2f})")
    f = ticker["funding"]  # ставка за 8 часов, доля (0.0001 = 0.01%)
    if f >= 0.0005:
        score -= 0.6; reasons.append(f"фандинг {f*100:.3f}% — переполнены лонги")
    elif f >= 0.0002:
        score -= 0.3; reasons.append(f"фандинг {f*100:.3f}% — перекос в лонг")
    elif f <= -0.0005:
        score += 0.6; reasons.append(f"фандинг {f*100:.3f}% — переполнены шорты")
    elif f <= -0.0002:
        score += 0.3; reasons.append(f"фандинг {f*100:.3f}% — перекос в шорт")
    wf = ticker.get("whale_imb")
    if wf is not None and ticker.get("whale_n", 0) >= 5 and abs(wf) >= 0.3:
        score += 0.4 * wf
        reasons.append(f"крупные сделки за час: {'покупают' if wf > 0 else 'продают'} ({wf:+.2f}, {ticker.get('whale_n')} шт.)")
    bi = ticker.get("book_imb")
    if bi is not None and abs(bi) >= 0.3:
        score += 0.15 * bi
        reasons.append(f"стакан: перевес {'покупателей' if bi > 0 else 'продавцов'} ({bi:+.2f})")
    cr = ticker.get("crowd")
    if cr:   # единодушная розница на форуме: аргумент против толпы (знак уже учтён)
        score += cr
        reasons.append(ticker.get("crowd_reason") or "толпа на форуме единодушна")
    ls = ticker.get("long_ratio")
    if ls is not None:
        if ls >= 0.68:
            score -= 0.25; reasons.append(f"{ls*100:.0f}% аккаунтов в лонге: толпа перегружена")
        elif ls <= 0.38:
            score += 0.25; reasons.append(f"{ls*100:.0f}% аккаунтов в лонге: толпа в шорте")
    if len(oi_hist) >= 12 and oi_hist[0] > 0:
        oi_chg = (oi_hist[-1] / oi_hist[0] - 1) * 100
        if abs(oi_chg) >= 5:
            # рост OI по тренду = подтверждение, рост OI против движения цены = накопление контр-позиций
            same = 1 if (oi_chg > 0) == (ta["ret_24h"] > 0) else -1
            score += 0.3 * same * (1 if ta["ret_24h"] > 0 else -1)
            reasons.append(f"OI {oi_chg:+.1f}% за сутки при цене {ta['ret_24h']:+.1f}%")
    return max(-1.0, min(1.0, score)), reasons


def _no(diag, reason, strength):
    if diag is not None:
        diag["reason"], diag["strength"] = reason, round(strength, 3)
    return None


def build_signal(symbol, base, ta, ticker, oi_hist, events, params, now=None, playbook=None, whale_net=0.0, mood=0.0,
                 df1h=None, psych_cfg=None, diag=None, factor=0.0, factor_text=""):
    """diag: словарь, куда записывается причина отказа (для сводки «почему нет сделок»)."""
    p = params
    ns, nreasons, used = news_score(base, events, p["min_news_importance"], now,
                                    playbook if p.get("use_playbook", True) else None, df1h, psych_cfg,
                                    mode=p.get("news_mode"))
    ps, preasons = positioning_score(ticker, oi_hist, ta, whale_net, mood)
    crowd_s, crowd_reasons = psychology.crowd(ticker, mood, psych_cfg or {})
    preasons += crowd_reasons
    ts = ta["score"]
    nw, tw, pw = p["news_weight"], p["ta_weight"], p["positioning_weight"]
    # Аддитивное сведение: сильная новость сама по себе даёт вход, ТА и позиционирование усиливают или гасят её.
    # Чистый ТА без подтверждения до порога почти не дотягивает (бэктест показал, что у него нет преимущества).
    total = max(-1.0, min(1.0, nw * ns + tw * ts + pw * ps))

    fw = float(p.get("factor_weight", 0.0) or 0.0)
    if factor and fw:
        total += fw * factor
    if factor_text:
        preasons.append(factor_text + ("" if fw else " (в силу не входит, только учёт)"))
    comps = {"news": round(ns, 3), "ta": round(ts, 3), "positioning": round(ps, 3), "factor": round(factor or 0.0, 3),
             "total": round(total, 3)}
    nonzero = [v for v in (ns, ts, ps) if abs(v) > 0.05] + ([factor] if factor and fw and abs(factor) > 0.05 else [])
    sign = 1 if total > 0 else -1
    agreement = (sum(1 for v in nonzero if (v > 0) == (sign > 0)) / len(nonzero)) if nonzero else 0.0
    strength = abs(total) * (0.7 + 0.3 * agreement)
    strength *= psychology.crowd_adjust(sign, crowd_s, psych_cfg or {})
    # Против дневного тренда ходить дороже: по 15 годам BTC на дневках работает импульс, а не разворот
    td = ta.get("trend_d", 0)
    if (sign < 0 and td > 0) or (sign > 0 and td < 0):
        strength *= 0.85
        preasons.append("против дневного тренда: сила x0.85")
    if ta.get("timing_15m", 0) != 0 and (ta["timing_15m"] > 0) != (sign > 0):
        strength *= 0.85  # 15-минутный импульс против нас: ждём лучшей точки
    ta_long_mult = float((p or {}).get("ta_long_mult", 1.0) or 1.0)
    if sign > 0 and abs(ns) < 0.3 and ta_long_mult != 1.0:
        # Мосбиржа 2019-2026: технические лонги PF 0.83, шорты PF 1.16 — лонгу без новости нужен запас силы
        strength *= ta_long_mult
        preasons.append(f"технический лонг на Мосбирже слабее шорта: сила x{ta_long_mult:g}")

    if diag is not None:
        diag.update(direction="long" if sign > 0 else "short", comps=comps, news_driven=abs(ns) >= 0.3,
                    ext=round(sign * ta["ret_24h"] / ta["atr_pct"], 2) if ta.get("atr_pct") else 0.0)
    reasons = nreasons + preasons + [
        f"ТА: режим {ta['regime']}, тренд1ч {ta['trend_1h']:+d}, тренд4ч {ta['trend_4h']:+d}, "
        f"пробой {ta['breakout']:+d}, RSI {ta['rsi']}, ADX {ta['adx']}, ATR {ta['atr_pct']}%"
    ]

    # Во флэте торгуем только от сильных новостей
    if ta["regime"] == "range" and abs(ns) < 0.3 and not (sign < 0 and p.get("range_shorts")):
        # Мосбиржа 2020–2026: шорты во флэте PF 1.21 (2024–2026 PF 1.4–2.2), лонги во флэте PF 0.9 — пускаем только шорты
        return _no(diag, "range", strength)
    # В экстремальной волатильности стоп 1.5-2 ATR выбивает шумом: без весомой новости не входим
    if ta["regime"] == "high_vol" and abs(ns) < 0.3:
        return _no(diag, "high_vol", strength)
    # Не торгуем против тренда 4ч без весомой новости
    if ta["trend_4h"] != 0 and (ta["trend_4h"] > 0) != (sign > 0) and abs(ns) < 0.5:
        return _no(diag, "counter_4h", strength)

    # Погоня за движением: если за сутки цена уже прошла >15% в нашу сторону и новости нет, не входим.
    if abs(ns) < 0.3 and abs(ta["ret_24h"]) > 15 and (ta["ret_24h"] > 0) == (sign > 0):
        return _no(diag, "chase", strength)

    # Растяжение: цена уже прошла >= max_extension_atr ATR(1ч) за 24ч в нашу сторону = вход в конце движения.
    # На 400 днях истории по 26 парам такие входы убыточны, а на живом счёте 12 из 13 таких сделок закрылись
    # в минус (research/live_rules). Исключение: очень свежая важная новость, движение только начинается.
    ext = sign * ta["ret_24h"] / ta["atr_pct"] if ta.get("atr_pct") else 0.0
    fresh = any(e.get("age_h", 99) * 60 <= p.get("fresh_news_minutes", 20) and e["importance"] >= 7 for e in used)
    ext_lim = p.get("max_extension_atr", 2.0)
    if sign < 0 and p.get("max_extension_atr_short"):
        ext_lim = p["max_extension_atr_short"]   # Мосбиржа, 7 лет: шорты без фильтра растяжения PF 1.32, сделок вдвое больше
    if ext >= ext_lim and not fresh:
        return _no(diag, "extension", strength)

    t = p["entry_threshold"]
    if strength < t:
        return _no(diag, "weak", strength)
    if strength >= p["conviction_threshold"] and agreement >= 0.99 and len(nonzero) >= 2:
        tier = "conviction"
    elif strength >= t + 0.18:
        tier = "high"
    elif strength >= t + 0.08:
        tier = "medium"
    else:
        tier = "low"
    if p.get("tier_by_agreement"):
        # Мосбиржа, сделки и тени: high по новостному баллу −0.48R, low/medium в плюсе; сила 0.7+ в тенях хуже 0.4–0.6.
        # Большой размер только там, где техника и тренд 4ч согласны с направлением
        ta_agree = abs(ts) > 0.05 and (ts > 0) == (sign > 0)
        t4 = ta.get("trend_4h", 0)
        trend_agree = t4 != 0 and (t4 > 0) == (sign > 0) and ta["regime"] in ("trend_up", "trend_down")
        if not ta_agree and tier != "low":
            tier = "low"
            reasons.append("техника не подтверждает направление: только малый размер")
        elif not trend_agree and tier in ("high", "conviction"):
            tier = "medium"
            reasons.append("тренд 4ч не подтверждает: размер не больше среднего")
    if ta["regime"] == "high_vol":
        tier = {"conviction": "high", "high": "medium", "medium": "low", "low": "low"}[tier]
        reasons.append("экстремальная волатильность: размер понижен")
    if abs(ns) < 0.05 and tier != "low":
        # чистый теханализ на истории около нуля по ожиданию: только малый размер
        tier = "low"
        reasons.append("без новостного подтверждения: только малый размер")

    key = ""
    if used and abs(ns) >= 0.3:
        top = max(used, key=lambda e: e["importance"] * e["confidence"])
        key = top.get("news_id") or top.get("title", "")[:40]
    return Signal(symbol=symbol, direction="long" if sign > 0 else "short", strength=round(strength, 3),
                  tier=tier, components=comps, reasons=reasons, regime=ta["regime"],
                  news_driven=abs(ns) >= 0.3, events=used, event_key=key)
