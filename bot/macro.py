"""Настроение рынка и ликвидность (бесплатные источники, обновление раз в час):
индекс страха и жадности (alternative.me), совокупное предложение стейблкоинов (DefiLlama) и его изменение за 7 дней."""
import logging
import time

import requests

log = logging.getLogger("macro")


class MarketMood:
    def __init__(self, refresh_sec=3600):
        self.refresh_sec = refresh_sec
        self.last = 0.0
        self.fng = None            # 0..100
        self.fng_label = ""
        self.fng_prev = None
        self.stables_bn = None     # млрд USD
        self.stables_7d_pct = None

    def refresh(self, force=False):
        if not force and time.time() - self.last < self.refresh_sec:
            return
        self.last = time.time()
        try:
            r = requests.get("https://api.alternative.me/fng/?limit=8", timeout=15).json()
            data = r.get("data", [])
            if data:
                self.fng = int(data[0]["value"])
                self.fng_label = data[0].get("value_classification", "")
                self.fng_prev = int(data[-1]["value"]) if len(data) > 1 else None
        except Exception as e:  # noqa: BLE001
            log.debug("fear&greed: %s", e)
        try:
            r = requests.get("https://stablecoins.llama.fi/stablecoins?includePrices=false", timeout=20,
                             headers={"User-Agent": "Mozilla/5.0"}).json()
            total, prev7 = 0.0, 0.0
            for a in r.get("peggedAssets", []):
                cur = (a.get("circulating") or {}).get("peggedUSD") or 0
                p7 = (a.get("circulatingPrevWeek") or {}).get("peggedUSD") or 0
                total += cur
                prev7 += p7
            if total > 0:
                self.stables_bn = total / 1e9
                self.stables_7d_pct = (total / prev7 - 1) * 100 if prev7 > 0 else None
        except Exception as e:  # noqa: BLE001
            log.debug("stablecoins: %s", e)
        log.info("Настроение: F&G %s (%s), стейблкоины %s млрд (%s%% за 7д)", self.fng, self.fng_label,
                 f"{self.stables_bn:.0f}" if self.stables_bn else "?",
                 f"{self.stables_7d_pct:+.2f}" if self.stables_7d_pct is not None else "?")

    def score(self):
        """-1..1 для позиционирования: крайний страх = контртрендовый плюс, крайняя жадность = минус."""
        s = 0.0
        if self.fng is not None:
            if self.fng <= 15:
                s += 0.3
            elif self.fng <= 25:
                s += 0.15
            elif self.fng >= 85:
                s -= 0.3
            elif self.fng >= 75:
                s -= 0.15
        if self.stables_7d_pct is not None:
            if self.stables_7d_pct >= 1.0:
                s += 0.1
            elif self.stables_7d_pct <= -1.0:
                s -= 0.1
        return max(-1.0, min(1.0, s))

    def text(self):
        parts = []
        if self.fng is not None:
            trend = f", неделю назад {self.fng_prev}" if self.fng_prev is not None else ""
            parts.append(f"Индекс страха и жадности: {self.fng} ({self.fng_label}{trend})")
        if self.stables_bn:
            chg = f", {self.stables_7d_pct:+.2f}% за 7 дней" if self.stables_7d_pct is not None else ""
            parts.append(f"Предложение стейблкоинов: {self.stables_bn:.0f} млрд USD{chg}")
        return "\n".join(parts)
