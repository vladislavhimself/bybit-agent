"""Человеческие сообщения для Telegram: вход, выход, баланс, сигнал. Сообщения о сделках и балансе — в HTML
(жирные заголовки блоков), динамический текст экранируется через esc()."""
import html
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
    "trend_gate": "против тренда рынка", "stale_news": "новость слабая (<6) или старше 15 мин",
    "paper_mode": "режим наблюдения (тени в минусе)",
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


NAMES = {}   # тикер -> человеческое название (заполняет main из списка инструментов биржи)


def base_of(symbol):
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def label(symbol):
    """Тикер плюс название: «NVZ6 (Новатэк, фьючерс до 17.12.2026)»."""
    base = base_of(symbol)
    name = NAMES.get(symbol) or NAMES.get(base)
    return f"{base} ({name})" if name else base


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


def esc(x):
    return html.escape(str(x), quote=False)


def money(x, d=None):
    """Числа с пробелами в тысячах: 99 650.17; для сумм от 1000 копейки не показываем."""
    if d is None:
        d = 0 if abs(x) >= 1000 else 2
    return f"{x:,.{d}f}".replace(",", " ")


def signed(x, d=2):
    return f"{x:+,.{d}f}".replace(",", " ")


def block(title, *lines):
    return f"<b>{title}</b>\n" + ("\n".join(l for l in lines if l) or "—")


def entry_message(sig, entry, qty, lev, stop, plan, risk_pct, risk_usd, tradable, equity, ta=None, dry=False,
                  mult=1.0, fee=0.0):
    base = base_of(sig.symbol)
    side = "ЛОНГ" if sig.direction == "long" else "ШОРТ"
    notional = qty * entry * mult
    head = "🧪 СИГНАЛ (dry-run, сделка НЕ открыта)" if dry else "🟢 ВХОД"
    tps = " / ".join(f"{l['price']:g}" for l in plan) if plan else "нет, только трейлинг"
    unit = "лот." if mult != 1.0 else base
    parts = [
        f"{head}: <b>{esc(label(sig.symbol))} {side}</b>",
        block("Направление", f"{side}, размер {TIER_RU.get(sig.tier, sig.tier)}, плечо {lev}x"),
        block("Вход", f"{entry:g}, объём {qty:g} {esc(unit)} (~{money(notional)} {CUR})"
              + (f", комиссия входа {money(fee)} {CUR}" if fee else "")),
        block("Стоп и тейки", f"Стоп {stop:g} (риск {risk_pct}% = {money(risk_usd)} {CUR})", f"Тейки {tps}"),
        block("Почему", *[esc(r) for r in short_reasons(sig, ta)]),
        block("Капитал", f"Торговый {money(tradable)} {CUR}, в сделке {notional / tradable * 100 if tradable else 0:.0f}%",
              f"Весь счёт {money(equity)} {CUR}"),
    ]
    return "\n\n".join(parts)


def close_message(t, pnl, pnl_r, reason, tradable=None, fees=None):
    base = base_of(t["symbol"])
    side = "ЛОНГ" if t["side"] == "Buy" else "ШОРТ"
    emoji = "✅" if pnl > 0 else "❌"
    fee_txt = f"с учётом комиссии {money(fees, 2)} {CUR}" if fees else "с учётом комиссии"
    parts = [
        f"{emoji} ВЫХОД: <b>{esc(label(t['symbol']))} {side}</b>",
        block("Результат", f"{signed(pnl)} {CUR} ({pnl_r:+.2f}R), {fee_txt}"),
        block("Причина", esc(reason)),
    ]
    if tradable is not None:
        parts.append(block("Торговый капитал", f"{money(tradable)} {CUR}"))
    return "\n\n".join(parts)


def balance_message(equity, m, positions, stats, cap, llm=None, next_macro=None, xstream=None, halted_hint=True,
                    pendings=None, why_none=None, shadow_line=None):
    parts = ["💼 <b>БАЛАНС</b>"]
    if cap:
        parts.append(block("Торговый капитал", f"{money(m['tradable'])} {CUR} (лимит {money(cap)})",
                           f"Прибыль с начала: {signed(m['tradable'] + m['reserve'] - cap)} {CUR}"))
    else:
        parts.append(block("Торговый капитал", f"{money(m['tradable'])} {CUR}"))
    parts.append(block("Резерв прибыли", f"{money(m['reserve'])} {CUR} | на счёте {money(equity)} {CUR}"))
    parts.append(block("Динамика", f"Сегодня {m['day_pct']:+.1f}% | неделя {m['week_pct']:+.1f}% | просадка от пика {m['drawdown_pct']:.1f}%"))
    if m["halted"] and halted_hint:
        parts.append(f"⛔ <b>Остановлен</b>: {esc(m['halt_reason'])}. /resume чтобы продолжить")
    if pendings:
        parts.append(block("Ждут отката", *[f"• {esc(label(p['symbol']))} {'ЛОНГ' if p['side'] == 'Buy' else 'ШОРТ'} @ {p['price']:g}"
                                            for p in pendings]))
    if positions:
        lines = []
        for p in positions:
            side = "ЛОНГ" if p["side"] == "Buy" else "ШОРТ"
            notional = p["qty"] * p["entry"] * (p.get("mult") or 1.0)
            pct = f" ({p['upnl'] / notional * 100:+.2f}%)" if notional else ""
            lines.append(f"• {esc(label(p['symbol']))} {side} {p['qty']:g} @ {p['entry']:g}")
            lines.append(f"   сейчас {signed(p['upnl'])} {CUR}{pct}, цена {p['mark']:g}, плечо {p['leverage']:.1f}x, стоп {p['sl']:g}")
        parts.append(block("Позиции", *lines))
    else:
        parts.append(block("Позиции", "открытых нет"))
    if stats.get("n"):
        parts.append(block("Сделки", f"{stats['n']}: прибыльных {stats['win_rate']}%, средний результат {stats['avg_r']}R",
                           f"Итог {signed(stats['pnl'])} {CUR} с учётом комиссий ({money(stats.get('fees', 0), 2)} {CUR})"))
    if llm:
        parts.append(block("Модель сегодня", f"{llm['calls']} вызовов, {llm['usd']:.2f}$ из {llm['budget']:.0f}$"))
    if next_macro:
        parts.append(block("Ближайшее макро", esc(next_macro)))
    if why_none:
        parts.append(block("Почему не было входов", esc(why_none)))
    if shadow_line:
        parts.append(block("Теневые сделки", esc(shadow_line)))
    if xstream:
        parts.append(block("Поток X", esc(xstream)))
    return "\n\n".join(parts)
