"""Человеческие сообщения для Telegram: вход, выход, баланс, сигнал."""
import re

RU_TYPE = {
    "listing": "листинг", "delisting": "делистинг", "hack": "взлом", "collapse": "крах", "regulation": "регулятор",
    "macro": "макро", "etf": "ETF", "partnership": "партнёрство", "whale": "киты", "upgrade": "обновление сети",
    "outage": "сбой", "legal": "суд", "token_unlock": "разблокировка", "earnings": "отчёт", "other": "событие",
}
REASONS_RU = {
    "range": "флэт без сильной новости", "high_vol": "резкая волатильность без новости",
    "counter_4h": "против тренда 4ч", "chase": "цена уже ушла больше 15%",
    "extension": "цена уже прошла 2+ ATR за сутки (поздно)", "weak": "сигнал слабее порога",
    "no_data": "мало данных", "error": "ошибка данных",
}


def funnel_text(counter, best_weak=None, threshold=None, top=5):
    if not counter:
        return "нет данных"
    s = "; ".join(f"{REASONS_RU.get(k, k)}: {v}" for k, v in counter.most_common(top))
    if best_weak and threshold:
        s += f". Ближе всего к входу: {base_of(best_weak[0])} (сила {best_weak[1]:.2f} при пороге {threshold:.2f})"
    return s


TIER_RU = {"low": "малый", "medium": "средний", "high": "крупный", "conviction": "максимальный"}


CUR = "USDT"   # валюта счёта, задаётся при старте из настроек


def clean(text, limit=90):
    t = re.sub(r"[^\w\s$#%().,:;!?'\"/+\-–—]", "", text or "")
    t = " ".join(t.split())
    return t[:limit] + ("…" if len(t) > limit else "")


def base_of(symbol):
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def short_reasons(sig, ta=None, limit=3):
    """Две-три короткие причины входа из списка reasons сигнала."""
    out = []
    news = [r for r in sig.reasons if r.startswith("новость")]
    psych = [r for r in sig.reasons if r.startswith("психология")]
    pos = [r for r in sig.reasons if r.startswith(("фандинг", "OI", "потоки китов", "настроение")) or "аккаунтов в лонге" in r]
    if news:
        out.append(news[0])
    if ta:
        parts = []
        if ta.get("trend_1h") or ta.get("trend_4h"):
            tf = [x for x, v in (("1ч", ta.get("trend_1h")), ("4ч", ta.get("trend_4h")), ("день", ta.get("trend_d"))) if v]
            direction = "вверх" if (ta.get("trend_4h") or ta.get("trend_1h")) > 0 else "вниз"
            parts.append(f"тренд {direction} ({', '.join(tf)})")
        if ta.get("breakout"):
            parts.append("пробой " + ("вверх" if ta["breakout"] > 0 else "вниз"))
        parts.append(f"RSI {ta.get('rsi', 0):.0f}")
        out.append("ТА: " + ", ".join(parts))
    for r in pos[:1] + psych[:1]:
        out.append(r)
    return out[:limit]


def entry_message(sig, entry, qty, lev, stop, plan, risk_pct, risk_usd, tradable, equity, ta=None, dry=False,
                  mult=1.0, fee=0.0):
    base = base_of(sig.symbol)
    side = "ЛОНГ" if sig.direction == "long" else "ШОРТ"
    notional = qty * entry * mult
    head = "🧪 СИГНАЛ (dry-run, сделка НЕ открыта)" if dry else "🟢 ВХОД"
    tps = " / ".join(f"{l['price']:g}" for l in plan) if plan else "нет, только трейлинг"
    lines = [
        f"{head}: {base}, {side}, размер {TIER_RU.get(sig.tier, sig.tier)}",
        f"Вход {entry:g}, объём {qty:g} {'лот.' if mult != 1.0 else base} (~{notional:.0f} {CUR}), плечо {lev}x"
        + (f", комиссия входа {fee:.2f} {CUR}" if fee else ""),
        f"Стоп {stop:g} (риск {risk_pct}% = {risk_usd:.2f} {CUR}), тейки {tps}",
        "Почему: " + "; ".join(short_reasons(sig, ta)),
        f"Капитал: торговый {tradable:.0f} {CUR}, в сделке {notional / tradable * 100 if tradable else 0:.0f}%; "
        f"весь счёт {equity:.0f} {CUR}",
    ]
    return "\n".join(lines)


def close_message(t, pnl, pnl_r, reason, tradable=None, fees=None):
    base = base_of(t["symbol"])
    side = "ЛОНГ" if t["side"] == "Buy" else "ШОРТ"
    emoji = "✅" if pnl > 0 else "❌"
    fee_txt = f", уже с учётом комиссии {fees:.2f} {CUR}" if fees else " (с учётом комиссии)"
    s = f"{emoji} ВЫХОД: {base}, {side}: {pnl:+.2f} {CUR} ({pnl_r:+.2f}R){fee_txt}\nПричина: {reason}"
    if tradable is not None:
        s += f"\nТорговый капитал теперь {tradable:.2f} {CUR}"
    return s


def balance_message(equity, m, positions, stats, cap, llm=None, next_macro=None, xstream=None, halted_hint=True,
                    pendings=None, why_none=None, shadow_line=None):
    lines = ["💼 БАЛАНС"]
    if cap:
        lines.append(f"Торговый капитал: {m['tradable']:.2f} {CUR} (лимит {cap:.0f}, прибыль с начала {m['tradable'] + m['reserve'] - cap:+.2f})")
    else:
        lines.append(f"Торговый капитал: {m['tradable']:.2f} {CUR}")
    lines.append(f"Резерв прибыли: {m['reserve']:.2f} {CUR} | на счёте: {equity:.0f} {CUR}")
    lines.append(f"Сегодня {m['day_pct']:+.1f}% | неделя {m['week_pct']:+.1f}% | просадка от пика {m['drawdown_pct']:.1f}%")
    if m["halted"] and halted_hint:
        lines.append(f"⛔ Остановлен: {m['halt_reason']}. /resume чтобы продолжить")
    if pendings:
        lines.append("Ждут отката: " + ", ".join(f"{base_of(p['symbol'])} {'ЛОНГ' if p['side'] == 'Buy' else 'ШОРТ'} @ {p['price']:g}"
                                               for p in pendings))
    if positions:
        lines.append("Позиции:")
        for p in positions:
            side = "ЛОНГ" if p["side"] == "Buy" else "ШОРТ"
            lines.append(f"• {base_of(p['symbol'])} {side} {p['qty']:g} @ {p['entry']:g}, сейчас {p['upnl']:+.2f} {CUR}, "
                         f"плечо {p['leverage']:.1f}x, стоп {p['sl']:g}")
    else:
        lines.append("Открытых позиций нет")
    if stats.get("n"):
        lines.append(f"Сделок {stats['n']}: прибыльных {stats['win_rate']}%, средний результат {stats['avg_r']}R, "
                     f"итог {stats['pnl']:+.2f} {CUR} с учётом комиссий ({stats.get('fees', 0):.2f} {CUR})")
    if llm:
        lines.append(f"Модель сегодня: {llm['calls']} вызовов, {llm['usd']:.2f}$ из {llm['budget']:.0f}$")
    if next_macro:
        lines.append(f"Ближайшее макро: {next_macro}")
    if why_none:
        lines.append(f"Почему не было входов (сумма проверок): {why_none}")
    if shadow_line:
        lines.append(f"Теневые сделки {shadow_line}")
    if xstream:
        lines.append(f"Поток X: {xstream}")
    return "\n".join(lines)
