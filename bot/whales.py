"""Активность китов: разбор сообщений Whale Alert (Telegram-канал, без ключей) в события без участия модели.
Правила: перевод на биржу = давление продаж (bearish для монеты), с биржи = накопление (bullish),
минт стейблкоина = приток ликвидности (bullish для рынка), сжигание = отток. Размер определяет важность."""
import logging
import re
import time

log = logging.getLogger("whales")

EXCHANGES = ("binance", "coinbase", "kraken", "okx", "okex", "bybit", "bitfinex", "huobi", "htx", "kucoin", "gemini",
             "bitstamp", "gate", "bitget", "mexc", "crypto.com", "cryptocom", "upbit", "bithumb", "robinhood", "deribit",
             "hyperliquid", "bitmex", "poloniex", "bittrex", "lbank", "whitebit")
STABLES = ("USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "PYUSD", "BUSD", "USD1", "RLUSD")

RE_TX = re.compile(
    r"([\d,]+(?:\.\d+)?)\s*[$#]?([A-Za-z0-9]{2,10})\s*\(([\d,]+)\s*USD\)\s*"
    r"(minted|burned|transferred|locked|unlocked)(?:\s+(?:at|from)\s+(.+?))?(?:\s+to\s+(.+?))?(?:\s+Details)?\s*$",
    re.I)


def _is_exchange(name):
    n = (name or "").lower()
    return any(x in n for x in EXCHANGES)


def _importance(usd):
    if usd >= 1_000_000_000:
        return 8
    if usd >= 300_000_000:
        return 7
    if usd >= 100_000_000:
        return 6
    if usd >= 50_000_000:
        return 5
    return 0


def parse(text):
    """Вернуть событие (dict как у аналитика) или None, если это не крупная транзакция."""
    t = " ".join((text or "").replace("Read Analysis", "").split())
    t = re.sub(r"[^\w\s$#().,'-]", " ", t)   # убираем эмодзи
    m = RE_TX.search(" ".join(t.split()))
    if not m:
        return None
    amount, asset, usd_s, action, src, dst = m.groups()
    try:
        usd = float(usd_s.replace(",", ""))
    except ValueError:
        return None
    asset = asset.upper()
    imp = _importance(usd)
    if imp == 0:
        return None
    action = action.lower()
    src, dst = (src or "").strip(" ."), (dst or "").strip(" .")
    stable = asset in STABLES
    direction, market_wide, affected, note = "neutral", False, [asset], ""
    if action == "minted":
        if stable:
            direction, market_wide, affected, note = "bullish", True, [], "минт стейблкоина: приток ликвидности"
        else:
            return None
    elif action == "burned":
        if stable:
            direction, market_wide, affected, note = "bearish", True, [], "сжигание стейблкоина: отток ликвидности"
            imp = max(0, imp - 1)
        else:
            return None
    elif action == "transferred":
        s_ex, d_ex = _is_exchange(src), _is_exchange(dst)
        if s_ex and d_ex:
            return None   # между биржами: шум
        if not s_ex and not d_ex:
            return None   # кошелёк -> кошелёк: нечитаемо
        if d_ex:
            if stable:
                direction, market_wide, affected, note = "bullish", True, [], f"стейблкоины заведены на {dst}: покупательная способность"
            else:
                direction, note = "bearish", f"{asset} заведён на {dst}: вероятная продажа"
        else:
            if stable:
                return None
            direction, note = "bullish", f"{asset} выведен с {src}: накопление"
    else:
        return None
    if imp == 0 or direction == "neutral":
        return None
    return {
        "relevant": True, "event_type": "whale", "importance": imp, "direction": direction,
        "confidence": 0.45 if imp < 7 else 0.55, "affected_symbols": affected, "market_wide": market_wide,
        "horizon_hours": 6, "already_priced": False, "is_rumor": False,
        "reasoning": f"{note} ({usd / 1e6:,.0f} млн USD)", "usd": usd, "asset": asset,
        "kind": "to_exchange" if (action == "transferred" and _is_exchange(dst)) else
                ("from_exchange" if action == "transferred" else action),
    }


class WhaleFlows:
    """Агрегат потоков за 24 часа для контекста модели и сигналов. Хранится в журнале (kv)."""

    def __init__(self, journal):
        self.j = journal

    def add(self, ev):
        rows = self.j.kv_get("whale_flows", [])
        rows.append({"ts": int(time.time()), "asset": ev["asset"], "usd": ev["usd"], "kind": ev["kind"]})
        cutoff = int(time.time()) - 48 * 3600
        rows = [r for r in rows if r["ts"] >= cutoff][-500:]
        self.j.kv_set("whale_flows", rows)

    def summary(self, hours=24):
        rows = [r for r in self.j.kv_get("whale_flows", []) if r["ts"] >= time.time() - hours * 3600]
        if not rows:
            return {}
        out = {}
        for r in rows:
            a = out.setdefault(r["asset"], {"to_exchange": 0.0, "from_exchange": 0.0, "minted": 0.0, "burned": 0.0})
            a[r["kind"]] = a.get(r["kind"], 0.0) + r["usd"]
        return out

    def text(self, hours=24):
        s = self.summary(hours)
        if not s:
            return ""
        lines = [f"Потоки китов за {hours} ч (Whale Alert, переводы от 50 млн USD):"]
        for asset, a in sorted(s.items(), key=lambda kv: -sum(kv[1].values())):
            parts = []
            if a["to_exchange"]:
                parts.append(f"на биржи {a['to_exchange'] / 1e6:,.0f} млн")
            if a["from_exchange"]:
                parts.append(f"с бирж {a['from_exchange'] / 1e6:,.0f} млн")
            if a.get("minted"):
                parts.append(f"минт {a['minted'] / 1e6:,.0f} млн")
            if a.get("burned"):
                parts.append(f"сжигание {a['burned'] / 1e6:,.0f} млн")
            lines.append(f"- {asset}: " + ", ".join(parts))
        return "\n".join(lines)

    def net_score(self, base, hours=24):
        """-1..1: положительно = накопление/приток, отрицательно = давление продаж. Для сигналов."""
        s = self.summary(hours)
        a = s.get(base)
        if not a:
            return 0.0
        net = a["from_exchange"] - a["to_exchange"]
        scale = 500e6 if base in ("BTC", "ETH") else 50e6
        return max(-1.0, min(1.0, net / scale))
