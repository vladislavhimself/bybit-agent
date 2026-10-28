"""Дивидендный календарь Мосбиржи (InstrumentsService.GetDividends в T-Invest).

Лонг акции через отсечку теряет дивидендный гэп целиком (цена открывается ниже на размер дивиденда),
шорт через отсечку обязан выплатить дивиденд. Поэтому за risk.div_block_days до последнего дня покупки
новые входы по акции запрещены в обе стороны. Фьючерсы не трогаем: дивиденд уже в их цене."""
import logging
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger("divcal")


def _q(x):
    return (int(x.get("units", 0)) + int(x.get("nano", 0)) / 1e9) if isinstance(x, dict) else 0.0


class DivCalendar:
    def __init__(self, settings, exchange):
        self.s, self.ex = settings, exchange
        self.next = {}      # base -> {"record", "last_buy", "div", "yield"}
        self.ts = 0.0

    def refresh(self, force=False):
        if not force and time.time() - self.ts < 6 * 3600:
            return
        self.ts = time.time()
        try:
            inst = self.ex.instruments()
        except Exception as e:  # noqa: BLE001
            log.warning("дивиденды: инструменты: %s", e)
            return
        now = datetime.now(timezone.utc)
        frm, to = now.strftime("%Y-%m-%dT00:00:00Z"), (now + timedelta(days=60)).strftime("%Y-%m-%dT00:00:00Z")
        found = {}
        for sym, it in inst.items():
            if it.get("kind") != "share" or not it.get("figi"):
                continue
            try:
                r = self.ex.call("InstrumentsService", "GetDividends", {"figi": it["figi"], "from": frm, "to": to})
            except Exception as e:  # noqa: BLE001
                log.debug("дивиденды %s: %s", sym, e)
                continue
            for d in r.get("dividends", []):
                lb = (d.get("lastBuyDate") or d.get("recordDate") or "")[:10]
                if not lb:
                    continue
                price = _q(d.get("closePrice")) or 0.0
                div = _q(d.get("dividendNet"))
                rec = {"record": (d.get("recordDate") or "")[:10], "last_buy": lb, "div": div,
                       "yield": round(div / price * 100, 2) if price else 0.0}
                if sym not in found or rec["last_buy"] < found[sym]["last_buy"]:
                    found[sym] = rec
            time.sleep(0.05)
        self.next = found
        if found:
            log.info("дивиденды: ближайшие отсечки %s", ", ".join(f"{k} {v['last_buy']}" for k, v in sorted(found.items(), key=lambda x: x[1]["last_buy"])[:6]))

    def blocks(self, symbol, inst=None):
        """Причина запрета входа по акции перед отсечкой или пусто."""
        days = int(self.s.get("risk.div_block_days", 2) or 0)
        if not days:
            return ""
        it = (inst or {}).get(symbol) or {}
        if it.get("kind") not in (None, "share"):
            return ""
        rec = self.next.get(symbol)
        if not rec:
            return ""
        try:
            lb = datetime.strptime(rec["last_buy"], "%Y-%m-%d").date()
        except ValueError:
            return ""
        today = datetime.now(timezone.utc).date()
        if today <= lb <= today + timedelta(days=days):
            return f"дивидендная отсечка: последний день покупки {rec['last_buy']}, дивиденд {rec['div']:g} ({rec['yield']:g}%)"
        return ""

    def text(self, days=30):
        today = datetime.now(timezone.utc).date()
        out = []
        for sym, rec in sorted(self.next.items(), key=lambda x: x[1]["last_buy"]):
            try:
                lb = datetime.strptime(rec["last_buy"], "%Y-%m-%d").date()
            except ValueError:
                continue
            if 0 <= (lb - today).days <= days:
                out.append(f"{sym}: купить до {rec['last_buy']}, дивиденд {rec['div']:g} ({rec['yield']:g}%)")
        return out
