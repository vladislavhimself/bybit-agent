"""Психология участников: три измеримых эффекта, которые ломают «хорошая новость = рост».

1. Заложено в цену. Если перед событием цена уже прошла большой путь в сторону новости, участники зафиксируют
   прибыль на факте («покупай слух, продавай новость»). Сила новости режется.
2. Реакция цены. Через 30-60 минут после новости цена говорит больше, чем сама новость: если на хорошей новости
   рынок не растёт или падает, это сигнал слабости, и наоборот (капитуляция на плохой новости уже прошла).
3. Толпа на одной стороне. Перекос фандинга, доли лонгов и жадности: когда все уже в позиции, двигать цену
   дальше некому, зато есть кому сдаваться. Вход вместе с толпой режется, вход против толпы усиливается.
"""
import time


def _ret_pct(df1h, ts_from, ts_to=None):
    """Доходность % между часом ts_from и ts_to (сек, UTC) по часовым свечам."""
    if df1h is None or len(df1h) < 3:
        return None
    ms_from = int(ts_from * 1000) // 3_600_000 * 3_600_000
    row = df1h[df1h["ts"] == ms_from]
    if row.empty:
        return None
    p0 = float(row["open"].iloc[0])
    if ts_to is None:
        p1 = float(df1h["close"].iloc[-1])
    else:
        ms_to = int(ts_to * 1000) // 3_600_000 * 3_600_000
        r2 = df1h[df1h["ts"] == ms_to]
        if r2.empty:
            return None
        p1 = float(r2["close"].iloc[0])
    return (p1 / p0 - 1) * 100 if p0 > 0 else None


def priced_in(event, df1h, cfg):
    """Множитель 0.3..1.15 и причина: насколько новость уже отыграна ростом/падением до неё."""
    hours = cfg.get("runup_hours", 48)
    thresh = cfg.get("runup_pct", 8.0)
    run = _ret_pct(df1h, event["ts"] - hours * 3600, event["ts"])
    if run is None:
        return 1.0, None
    same = (run > 0) == (event["direction"] == "bullish")
    if same and abs(run) >= thresh:
        k = max(0.3, 0.7 - (abs(run) - thresh) / (2 * thresh))
        return k, f"психология: до новости цена уже прошла {run:+.1f}% за {hours}ч, новость во многом в цене (x{k:.2f})"
    if not same and abs(run) >= thresh:
        return 1.15, f"психология: до новости цена шла против неё ({run:+.1f}%), новость неожиданна (x1.15)"
    return 1.0, None


def reaction(event, df1h, cfg, now=None):
    """Множитель по реакции цены после новости. Отрицательный множитель = разворот сигнала (продажа на новости)."""
    now = now or time.time()
    age_min = (now - event["ts"]) / 60
    min_wait = cfg.get("reaction_min_minutes", 30)
    if age_min < min_wait:
        return 1.0, None
    r = _ret_pct(df1h, event["ts"], None)
    if r is None:
        return 1.0, None
    expected_up = event["direction"] == "bullish"
    strong = cfg.get("reaction_strong_pct", 1.0) * (1 + event["importance"] / 10)
    went_with = (r > 0) == expected_up
    if went_with and abs(r) >= strong:
        late_h = cfg.get("late_entry_hours", 2)
        late_pct = cfg.get("late_entry_pct", 2.0)
        if age_min >= late_h * 60 and abs(r) >= late_pct:
            # движение уже состоялось: входить сейчас = догонять поезд
            return 0.4, f"психология: новость старше {late_h}ч и уже отыграна ({r:+.1f}%), вход поздний (x0.4)"
        return 1.1, f"психология: цена подтвердила новость ({r:+.1f}% после выхода)"
    if not went_with and abs(r) >= strong:
        # «хорошая новость, плохая реакция»: сила толпы иссякла, разворачиваем
        return -0.6, f"психология: продажа на новости, цена идёт против неё ({r:+.1f}%), сигнал развёрнут"
    if abs(r) < strong * 0.3 and age_min >= min_wait * 2:
        return 0.6, f"психология: рынок не отреагировал на новость за {age_min:.0f} мин (x0.6)"
    return 1.0, None


def crowd(ticker, mood_score, cfg):
    """-1..1: положительно = толпа в шорте/страхе (топливо для роста), отрицательно = толпа в лонге/жадности."""
    s, reasons = 0.0, []
    f = ticker.get("funding") or 0.0
    lr = ticker.get("long_ratio")
    if f >= 0.0003:
        s -= 0.35
    elif f <= -0.0003:
        s += 0.35
    if lr is not None:
        if lr >= 0.65:
            s -= 0.3
        elif lr <= 0.4:
            s += 0.3
    s += mood_score * 0.8
    s = max(-1.0, min(1.0, s))
    if abs(s) >= cfg.get("crowd_threshold", 0.5):
        reasons.append("психология: толпа " + ("в лонге/жадности, двигать цену вверх некому" if s < 0
                                             else "в шорте/страхе, топливо для роста") + f" ({s:+.2f})")
    return s, reasons


def crowd_adjust(direction_sign, crowd_score, cfg):
    """Множитель к силе сигнала: вместе с перегруженной толпой режем, против неё усиливаем."""
    t = cfg.get("crowd_threshold", 0.5)
    if abs(crowd_score) < t:
        return 1.0
    with_crowd = (direction_sign > 0 and crowd_score < 0) or (direction_sign < 0 and crowd_score > 0)
    return 0.7 if with_crowd else 1.15
