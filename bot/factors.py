"""Макрофакторы для бумаг Мосбиржи: Brent, доллар/рубль, индекс гособлигаций RGBI.

Бот читал новости про нефть, но не видел саму нефть. Здесь по часовым свечам считается балл −1..+1 на фактор
(ход за сутки относительно обычной волатильности плюс положение к EMA20), а по карте сектор → факторы
получается балл для конкретной бумаги: нефтегазу хорошо, когда растёт Brent и слабеет рубль; банкам и внутреннему
спросу — когда растут облигации (ставки вниз); экспортёрам металлов — слабый рубль.

Пока вес фактора в сигнале 0 (signals.factor_weight): балл только записывается в компоненты сигнала и теней,
чтобы /analytics показал, помогает ли он. Включать вес — после проверки на тенях."""
import logging
import time

import numpy as np

from . import ta as talib

log = logging.getLogger("factors")

# тикеры индикативов T-Invest (InstrumentsService.Indicatives): непрерывные ряды без экспираций
INDICATIVES = {"brent": "LCOc1", "rgbi": "RGBI", "usdrub": "IUSD2"}


class Factors:
    def __init__(self, settings, exchange):
        self.s, self.ex = settings, exchange
        self.uids = {}
        self.cache = {}     # name -> (ts, score, text)
        self.ttl = 900

    def _resolve(self):
        if self.uids:
            return
        try:
            r = self.ex.call("InstrumentsService", "Indicatives", {})
            by = {i.get("ticker"): i for i in r.get("instruments", [])}
            for name, tick in INDICATIVES.items():
                if tick in by:
                    self.uids[name] = by[tick]["uid"]
            # доллар: вечный фьючерс USDRUBF торгуется и в выходные, он предпочтительнее индикатива
            inst = self.ex.instruments()
            if "USDRUBF" in inst:
                self.uids["usdrub"] = inst["USDRUBF"]["uid"]
        except Exception as e:  # noqa: BLE001
            log.warning("факторы: не удалось получить индикативы: %s", e)

    def _score(self, name):
        c = self.cache.get(name)
        if c and time.time() - c[0] < self.ttl:
            return c[1], c[2]
        self._resolve()
        uid = self.uids.get(name)
        if not uid:
            return 0.0, ""
        try:
            d = self.ex.candles_uid(uid, "60", 120)
        except Exception as e:  # noqa: BLE001
            log.debug("факторы %s: %s", name, e)
            return 0.0, ""
        if d is None or len(d) < 30:
            return 0.0, ""
        close = d["close"]
        rets = np.diff(np.log(close.values))
        vol24 = float(np.std(rets[-120:])) * np.sqrt(24) if len(rets) >= 24 else 0.0
        ret24 = float(close.iloc[-1] / close.iloc[-25] - 1) if len(close) >= 25 else 0.0
        z = ret24 / (2 * vol24) if vol24 > 0 else 0.0
        trend = 1.0 if close.iloc[-1] > talib.ema(close, 20).iloc[-1] else -1.0
        score = float(max(-1.0, min(1.0, 0.7 * max(-1.0, min(1.0, z)) + 0.3 * trend)))
        label = {"brent": "Brent", "usdrub": "доллар/рубль", "rgbi": "RGBI"}[name]
        text = f"{label} за сутки {ret24 * 100:+.1f}%"
        self.cache[name] = (time.time(), score, text)
        return score, text

    def score_for(self, base):
        """Балл фактора для базового актива по карте секторов: (балл −1..1, текст для причин)."""
        sectors = self.s.get("risk.sectors") or {}
        fmap = self.s.get("signals.factor_map") or {}
        sec = next((k for k, v in sectors.items() if base in v), None)
        weights = fmap.get(sec) if sec else None
        if not weights:
            return 0.0, ""
        total, wsum, parts = 0.0, 0.0, []
        for name, w in weights.items():
            sc, txt = self._score(name)
            if not txt:
                continue
            total += w * sc
            wsum += abs(w)
            parts.append(txt)
        if not wsum:
            return 0.0, ""
        score = max(-1.0, min(1.0, total / wsum))
        return round(score, 3), f"факторы ({sec}): " + ", ".join(parts) + f" → балл {score:+.2f}"

    def text(self):
        out = []
        for name in INDICATIVES:
            sc, txt = self._score(name)
            if txt:
                out.append(f"{txt} (балл {sc:+.2f})")
        return out
