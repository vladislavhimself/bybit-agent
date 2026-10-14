"""Обёртка над Bybit V5 (pybit). Рыночные данные всегда с mainnet (кроме testnet),
торговля идёт на demo / testnet / live в зависимости от BOT_MODE."""
import logging
import time
from decimal import Decimal

import pandas as pd
from pybit.exceptions import InvalidRequestError
from pybit.unified_trading import HTTP

log = logging.getLogger("exchange")


class Exchange:
    def __init__(self, settings):
        self.s = settings
        mode = settings.mode
        key, sec = settings.bybit_key, settings.bybit_secret
        kw = dict(api_key=key or None, api_secret=sec or None, recv_window=15000)
        if mode == "demo":
            self.trade = HTTP(demo=True, **kw)
            self.market = HTTP()
        elif mode == "testnet":
            self.trade = HTTP(testnet=True, **kw)
            self.market = HTTP(testnet=True)
        elif mode == "live":
            self.trade = HTTP(**kw)
            self.market = self.trade
        else:
            raise ValueError(f"BOT_MODE={mode!r}: допустимо demo | testnet | live")
        self.mode = mode
        self._instruments = {}
        self._instruments_ts = 0.0

    # ---------- справочник инструментов ----------
    def instruments(self, max_age=3600):
        if self._instruments and time.time() - self._instruments_ts < max_age:
            return self._instruments
        out, cursor = {}, None
        while True:
            params = dict(category="linear", limit=1000)
            if cursor:
                params["cursor"] = cursor
            r = self.market.get_instruments_info(**params)
            for it in r["result"]["list"]:
                if it.get("quoteCoin") != "USDT" or it.get("status") != "Trading":
                    continue
                if it.get("contractType") != "LinearPerpetual":
                    continue
                # Токенизированные акции, ETF, золото, форекс не трогаем: это не крипта,
                # новостной анализ и ончейн-логика к ним не применимы.
                if it.get("symbolType") not in ("", None, "innovation"):
                    continue
                lot, pf, lf = it["lotSizeFilter"], it["priceFilter"], it["leverageFilter"]
                out[it["symbol"]] = {
                    "base": it["baseCoin"],
                    "qty_step": Decimal(lot["qtyStep"]),
                    "min_qty": Decimal(lot["minOrderQty"]),
                    "tick": Decimal(pf["tickSize"]),
                    "max_leverage": float(lf["maxLeverage"]),
                }
            cursor = r["result"].get("nextPageCursor")
            if not cursor:
                break
        self._instruments, self._instruments_ts = out, time.time()
        return out

    def universe(self):
        """Самые ликвидные USDT-перпетуалы + обязательные пары."""
        cfg = self.s.get("universe")
        inst = self.instruments()
        r = self.market.get_tickers(category="linear")
        rows = []
        for t in r["result"]["list"]:
            sym = t["symbol"]
            if sym not in inst or sym in (cfg.get("blacklist") or []):
                continue
            turnover = float(t.get("turnover24h") or 0)
            if turnover >= cfg["min_turnover_24h_usd"]:
                rows.append((turnover, sym))
        rows.sort(reverse=True)
        syms = [s for _, s in rows[: cfg["top_n_by_turnover"]]]
        for s in cfg.get("always") or []:
            if s in inst and s not in syms:
                syms.append(s)
        return syms

    def symbols_for_base(self, base):
        return [s for s, i in self.instruments().items() if i["base"] == base.upper()]

    # ---------- рыночные данные ----------
    def klines(self, symbol, interval="60", limit=200, end=None):
        params = dict(category="linear", symbol=symbol, interval=str(interval), limit=limit)
        if end:
            params["end"] = int(end)
        r = self.market.get_kline(**params)
        rows = r["result"]["list"]
        cols = ["ts", "open", "high", "low", "close", "volume", "turnover"]
        if not rows:
            return pd.DataFrame(columns=cols)
        df = pd.DataFrame(rows, columns=cols).astype(float)
        df["ts"] = df["ts"].astype("int64")
        return df.iloc[::-1].reset_index(drop=True)

    def ticker(self, symbol):
        r = self.market.get_tickers(category="linear", symbol=symbol)
        t = r["result"]["list"][0]

        def f(k):
            return float(t.get(k) or 0)

        return {
            "last": f("lastPrice"), "mark": f("markPrice"), "funding": f("fundingRate"),
            "oi_value": f("openInterestValue"), "pct24h": f("price24hPcnt") * 100,
            "turnover24h": f("turnover24h"), "high24h": f("highPrice24h"), "low24h": f("lowPrice24h"),
        }

    def open_interest(self, symbol, hours=24):
        try:
            r = self.market.get_open_interest(category="linear", symbol=symbol, intervalTime="1h", limit=hours + 1)
            vals = [float(x["openInterest"]) for x in r["result"]["list"]]
            return vals[::-1]  # старые -> новые
        except Exception as e:  # noqa: BLE001
            log.debug("OI %s: %s", symbol, e)
            return []

    def announcements(self, limit=20):
        r = self.market.get_announcement(locale="en-US", limit=limit)
        return r["result"]["list"]

    # ---------- аккаунт ----------
    def equity(self):
        r = self.trade.get_wallet_balance(accountType="UNIFIED")
        acc = r["result"]["list"][0]
        return float(acc["totalEquity"]), float(acc.get("totalAvailableBalance") or 0)

    def positions(self):
        r = self.trade.get_positions(category="linear", settleCoin="USDT")
        out = []
        for p in r["result"]["list"]:
            if float(p.get("size") or 0) <= 0:
                continue
            out.append({
                "symbol": p["symbol"], "side": p["side"], "qty": float(p["size"]),
                "entry": float(p["avgPrice"]), "mark": float(p.get("markPrice") or 0),
                "upnl": float(p.get("unrealisedPnl") or 0), "leverage": float(p.get("leverage") or 0),
                "sl": float(p.get("stopLoss") or 0), "tp": float(p.get("takeProfit") or 0),
                "liq": float(p.get("liqPrice") or 0),
            })
        return out

    def set_leverage(self, symbol, lev):
        lev = str(int(max(1, lev)))
        try:
            self.trade.set_leverage(category="linear", symbol=symbol, buyLeverage=lev, sellLeverage=lev)
        except InvalidRequestError as e:
            if "110043" in str(e) or "not modified" in str(e).lower():
                return
            raise

    def place_market(self, symbol, side, qty, sl=None, tp=None, reduce_only=False):
        params = dict(category="linear", symbol=symbol, side=side, orderType="Market",
                      qty=self.fmt_qty(symbol, qty), positionIdx=0)
        if reduce_only:
            params["reduceOnly"] = True
        if sl:
            params["stopLoss"] = self.fmt_price(symbol, sl)
            params["slTriggerBy"] = "MarkPrice"
        if tp:
            params["takeProfit"] = self.fmt_price(symbol, tp)
            params["tpTriggerBy"] = "MarkPrice"
        r = self.trade.place_order(**params)
        return r["result"]["orderId"]

    def place_limit(self, symbol, side, qty, price, reduce_only=True):
        r = self.trade.place_order(category="linear", symbol=symbol, side=side, orderType="Limit",
                                   qty=self.fmt_qty(symbol, qty), price=self.fmt_price(symbol, price),
                                   timeInForce="GTC", reduceOnly=reduce_only, positionIdx=0)
        return r["result"]["orderId"]

    def cancel_all(self, symbol):
        try:
            self.trade.cancel_all_orders(category="linear", symbol=symbol)
        except InvalidRequestError as e:
            log.debug("cancel_all %s: %s", symbol, e)

    def open_orders(self, symbol):
        r = self.trade.get_open_orders(category="linear", symbol=symbol)
        return r["result"]["list"]

    def close_position(self, symbol):
        for p in self.positions():
            if p["symbol"] == symbol:
                side = "Sell" if p["side"] == "Buy" else "Buy"
                return self.place_market(symbol, side, p["qty"], reduce_only=True)
        return None

    def set_stop_loss(self, symbol, sl):
        self.trade.set_trading_stop(category="linear", symbol=symbol, positionIdx=0,
                                    stopLoss=self.fmt_price(symbol, sl), slTriggerBy="MarkPrice")

    def closed_pnl(self, symbol=None, start_ms=None, limit=50):
        params = dict(category="linear", limit=limit)
        if symbol:
            params["symbol"] = symbol
        if start_ms:
            params["startTime"] = int(start_ms)
        r = self.trade.get_closed_pnl(**params)
        out = []
        for x in r["result"]["list"]:
            out.append({
                "symbol": x["symbol"], "side": x["side"], "qty": float(x["qty"]),
                "entry": float(x["avgEntryPrice"]), "exit": float(x["avgExitPrice"]),
                "pnl": float(x["closedPnl"]), "ts": int(x["updatedTime"]),
            })
        return out

    # ---------- округления ----------
    def fmt_qty(self, symbol, qty):
        inst = self.instruments()[symbol]
        step = inst["qty_step"]
        q = (Decimal(str(qty)) / step).to_integral_value(rounding="ROUND_DOWN") * step
        return format(q.normalize(), "f")

    def fmt_price(self, symbol, price):
        inst = self.instruments()[symbol]
        tick = inst["tick"]
        p = (Decimal(str(price)) / tick).to_integral_value(rounding="ROUND_HALF_UP") * tick
        return format(p.normalize(), "f")

    def qty_ok(self, symbol, qty, price):
        inst = self.instruments()[symbol]
        q = Decimal(self.fmt_qty(symbol, qty))
        return q >= inst["min_qty"] and float(q) * price >= self.s.get("risk.min_notional_usd", 6)
