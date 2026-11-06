"""Московская биржа через T-Invest API (REST, песочница или боевой счёт). Тот же интерфейс, что у bot/exchange.py.

Единицы: symbol = тикер (SBER, SBERF, SRZ6, MXZ6). Количество везде в ЛОТАХ. Цена: у акций в рублях за акцию,
у фьючерсов в пунктах. mult(symbol) = рублей за 1 пункт цены на 1 лот: у акций размер лота, у фьючерсов стоимость
шага цены / шаг. Риск сделки = |вход − стоп| × mult × лоты, объём = цена × mult × лоты."""
import json
import logging
import threading
import zlib
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
import requests

from . import net
from .config import DATA

log = logging.getLogger("moex")
ROOT = Path(__file__).resolve().parent.parent
CA_BUNDLE = str(ROOT / "certs" / "ca_bundle.pem")
PKG = "tinkoff.public.invest.api.contract.v1."
INTERVALS = {"1": ("CANDLE_INTERVAL_1_MIN", 1), "5": ("CANDLE_INTERVAL_5_MIN", 5), "15": ("CANDLE_INTERVAL_15_MIN", 15),
             "60": ("CANDLE_INTERVAL_HOUR", 60), "240": ("CANDLE_INTERVAL_4_HOUR", 240), "D": ("CANDLE_INTERVAL_DAY", 1440)}
# максимальный период одного запроса свечей по документации
MAX_SPAN_MIN = {"1": 1440, "5": 1440, "15": 1440, "60": 7 * 1440, "240": 30 * 1440, "D": 365 * 1440}


def q2f(x):
    if x is None:
        return 0.0
    return float(x.get("units", 0) or 0) + float(x.get("nano", 0) or 0) / 1e9


def f2q(v):
    v = Decimal(str(v))
    units = int(v)
    nano = int((v - units) * 1_000_000_000)
    return {"units": str(units), "nano": nano}


def iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MoexExchange:
    def __init__(self, settings):
        self.s = settings
        self.mode = settings.mode   # demo = песочница, live = боевой счёт
        tok = settings.tinvest_token(self.mode)
        if not tok:
            raise RuntimeError("нет токена T-Invest: TINVEST_SANDBOX_TOKEN для demo или TINVEST_TOKEN для live")
        host = "https://sandbox-invest-public-api.tbank.ru" if self.mode != "live" else "https://invest-public-api.tbank.ru"
        self.url = host + "/rest/" + PKG
        self.h = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json", "Accept": "application/json"}
        self.http = net.session()   # сама решает: напрямую, через RU_PROXY или через ретранслятор в России
        self.account = None
        self._inst = {}
        self._inst_ts = 0.0
        self._uid2sym = {}
        self._figi2sym = {}
        self._turn_cache, self._turn_ts, self._iss_down_until, self._turn_thread = {}, 0.0, 0.0, None
        self._realized, self._realized_ts = 0.0, 0.0
        self._ccache, self._clock = {}, threading.Lock()   # свечи в памяти: (uid, интервал) -> серия
        self._px = {}   # последние цены инструментов (заполняются сканом): нужны, чтобы не брать в список контракт, который не открыть
        self._ensure_account()
        if not net.relay_enabled() and not net.ru_proxy():   # напрямую из-за границы: держим соединение тёплым; через ретранслятор не нужно
            threading.Thread(target=self._keepalive, daemon=True, name="tinvest-keepalive").start()

    def _keepalive(self):
        """Держим соединение с API тёплым: новое соединение с сервера устанавливается с нескольких попыток."""
        while True:
            time.sleep(15)
            try:
                self.http.get(self.url, timeout=(3, 10), verify=CA_BUNDLE)
            except requests.RequestException:
                pass

    # ---------- низкоуровневое ----------
    def call(self, svc, method, body=None, retries=8):
        # С иностранного IP T-Bank отбрасывает около половины новых TCP-соединений, а установленное живёт долго:
        # короткий таймаут на подключение, много быстрых повторов, keep-alive через общую сессию.
        for attempt in range(retries):
            try:
                r = self.http.post(self.url + svc + "/" + method, headers=self.h, json=body or {}, timeout=(3, 30),
                                   verify=CA_BUNDLE)
            except requests.RequestException as e:
                if attempt == retries - 1:
                    raise
                (log.debug if isinstance(e, requests.ConnectTimeout) else log.warning)("%s.%s: %s, повтор", svc, method, e)
                time.sleep(0.3)
                continue
            if r.status_code == 429:
                time.sleep(2 + attempt * 2)
                continue
            if r.status_code != 200:
                raise RuntimeError(f"{svc}.{method} -> {r.status_code}: {r.text[:300]}")
            return r.json()
        raise RuntimeError(f"{svc}.{method}: лимит запросов")

    def _ensure_account(self):
        cache = DATA / f"moex_account_{self.mode}.txt"
        try:
            self._ensure_account_online()
            cache.write_text(self.account, encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            if not cache.exists():
                raise
            # брокер/ретранслятор недоступен при старте: поднимаемся по сохранённому номеру счёта,
            # чтобы бот отвечал на команды и сообщал о проблеме, а не падал в перезапуски
            self.account = cache.read_text(encoding="utf-8").strip()
            log.error("T-Invest недоступен при старте (%s), счёт %s из кэша", str(e)[:100], self.account)

    def _ensure_account_online(self):
        if self.mode == "live":
            accs = self.call("UsersService", "GetAccounts").get("accounts", [])
            accs = [a for a in accs if a.get("status") == "ACCOUNT_STATUS_OPEN" and a.get("type") == "ACCOUNT_TYPE_TINKOFF"]
            if not accs:
                raise RuntimeError("нет открытого брокерского счёта")
            self.account = accs[0]["id"]
        else:
            accs = self.call("SandboxService", "GetSandboxAccounts").get("accounts", [])
            accs = [a for a in accs if a.get("status") == "ACCOUNT_STATUS_OPEN"]
            if not accs:
                self.call("SandboxService", "OpenSandboxAccount", {"name": "bot"})
                accs = self.call("SandboxService", "GetSandboxAccounts")["accounts"]
            self.account = accs[0]["id"]
            start = float(self.s.get("moex.sandbox_start_rub") or 0)
            if start and q2f(self.call("OperationsService", "GetPortfolio", {"accountId": self.account})
                             .get("totalAmountPortfolio")) < 1:
                self.call("SandboxService", "SandboxPayIn", {"accountId": self.account,
                                                             "amount": {"currency": "rub", "units": str(int(start)), "nano": 0}})
                log.info("Песочница пополнена на %.0f руб", start)
        log.info("T-Invest %s, счёт %s", "песочница" if self.mode != "live" else "БОЕВОЙ", self.account)

    # ---------- инструменты ----------
    def instruments(self, max_age=6 * 3600):
        if self._inst and time.time() - self._inst_ts < max_age:
            return self._inst
        if self._inst:
            # справочник уже есть: при сбое связи оставляем старый, а не роняем весь цикл
            try:
                return self._load_instruments()
            except Exception as e:  # noqa: BLE001
                log.warning("обновление справочника инструментов не удалось (%s), работаю по старому", str(e)[:100])
                self._inst_ts = time.time() - max_age + 300   # повторить через 5 минут
                return self._inst
        return self._load_instruments()

    def _load_instruments(self):
        out = {}
        shares = self.call("InstrumentsService", "Shares", {"instrumentStatus": "INSTRUMENT_STATUS_BASE"})["instruments"]
        for it in shares:
            if it.get("classCode") != "TQBR" or not it.get("apiTradeAvailableFlag"):
                continue
            step = q2f(it.get("minPriceIncrement")) or 0.01
            out[it["ticker"]] = {
                "kind": "share", "base": it["ticker"], "name": it.get("name", ""), "uid": it["uid"], "figi": it.get("figi"),
                "lot": int(it.get("lot") or 1), "tick": Decimal(str(step)), "mult": float(it.get("lot") or 1),
                "qty_step": Decimal("1"), "min_qty": Decimal("1"), "max_leverage": 1.0,
                "short_ok": bool(it.get("shortEnabledFlag")), "expiration": None, "launch_ts": 0.0,
            }
        futs = self.call("InstrumentsService", "Futures", {"instrumentStatus": "INSTRUMENT_STATUS_BASE"})["instruments"]
        for it in futs:
            if not it.get("apiTradeAvailableFlag") or it.get("currency") != "rub":
                continue
            step = q2f(it.get("minPriceIncrement")) or 1.0
            step_amt = q2f(it.get("minPriceIncrementAmount")) or step
            base = it.get("basicAsset") or ""
            if "ндекс" in base or base == "IMOEX":
                base = "IMOEX"
            if it["ticker"].startswith("RI") and "RTS" in (it.get("name") or ""):
                base = "RTS"
            exp = it.get("expirationDate", "")
            perpetual = exp.startswith("2099")
            out[it["ticker"]] = {
                "kind": "future", "base": base, "name": it.get("name", ""), "uid": it["uid"], "figi": it.get("figi"),
                "lot": 1, "tick": Decimal(str(step)), "mult": step_amt / step, "qty_step": Decimal("1"), "min_qty": Decimal("1"),
                "max_leverage": 6.0, "short_ok": True, "expiration": None if perpetual else exp[:10], "perpetual": perpetual,
                "launch_ts": 0.0, "futures_type": it.get("futuresType", ""),
            }
        self._inst, self._inst_ts = out, time.time()
        self._uid2sym = {v["uid"]: k for k, v in out.items()}
        self._figi2sym = {v["figi"]: k for k, v in out.items() if v.get("figi")}
        return out

    def mult(self, symbol):
        return self.instruments()[symbol]["mult"]

    def symbols_for_base(self, base):
        return [s for s, i in self.instruments().items() if i["base"] == base.upper()]

    def _turnover_today(self):
        """Оборот за день по акциям и фьючерсам: тикер -> рубли.
        Сначала ISS Мосбиржи (бесплатно, одним запросом), но с иностранных адресов ISS не отвечает (гео-блок):
        тогда считаем по дневным свечам T-Invest и держим результат в кэше 45 минут."""
        now = time.time()
        if self._turn_cache and now - self._turn_ts < 3 * 3600:
            return self._turn_cache
        # обороты считаются в фоне всегда (по свечам это 5-10 минут при бережном ритме): главный цикл не ждёт.
        # Пока среза нет, вселенная = обязательный список (always) и бумаги из новостей.
        if not (self._turn_thread and self._turn_thread.is_alive()):
            self._turn_thread = threading.Thread(target=self._refresh_turnover, daemon=True, name="turnover")
            self._turn_thread.start()
        return self._turn_cache

    def _refresh_turnover(self):
        now = time.time()
        out = {}
        if now >= self._iss_down_until:
            for url in ("https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities.json?iss.meta=off&iss.only=marketdata&marketdata.columns=SECID,VALTODAY",
                        "https://iss.moex.com/iss/engines/futures/markets/forts/boards/RFUD/securities.json?iss.meta=off&iss.only=marketdata&marketdata.columns=SECID,VALTODAY"):
                try:
                    md = self.http.get(url, timeout=8).json()["marketdata"]
                    cols = md["columns"]
                    # VALTODAY — оборот с начала дня: в 13:00 он вдвое меньше дневного, и половина бумаг не проходила
                    # порог. Приводим к полному дню по доле прошедшей сессии (10:00–23:50 МСК).
                    msk = self._msk_now()
                    frac = (msk.hour * 60 + msk.minute - 600) / (23 * 60 + 50 - 600)
                    scale = 1.0 / min(1.0, max(0.15, frac)) if 0 < frac < 1 else 1.0
                    for row in md["data"]:
                        d = dict(zip(cols, row))
                        out[d["SECID"]] = float(d.get("VALTODAY") or 0) * scale
                except Exception as e:  # noqa: BLE001
                    log.warning("ISS оборот недоступен (%s), час считаем по свечам T-Invest", str(e)[:80])
                    self._iss_down_until = now + 3600
                    out = {}
                    break
        if out:
            out = self._with_full_day(out)
        if not out:
            out = self._turnover_tinvest()
        if out:
            self._turn_cache, self._turn_ts = out, now
        return out

    @staticmethod
    def _msk_now():
        return datetime.now(timezone.utc) + timedelta(hours=3)

    def _with_full_day(self, today):
        """Оборот неполного дня не должен затирать оборот полного. 03.11 до 10:00 МСК в скане было 8 бумаг, до 13:00 — 16:
        утром сегодняшний оборот — это минуты утренней сессии, и почти ничто не проходило дневной порог.
        В будни вечером (после 90% сессии) срез сохраняется как оборот полного дня; на следующий день список строится
        по максимуму из него и сегодняшней оценки (оценке верим после 15% сессии, то есть с ~12:00 МСК).
        В выходные торгуется малая часть бумаг: там, как и раньше, только сегодняшний оборот."""
        msk = self._msk_now()
        if msk.weekday() >= 5:
            return today
        frac = (msk.hour * 60 + msk.minute - 600) / (23 * 60 + 50 - 600)
        path = DATA / "moex_turnover_full.json"
        full = {}
        try:
            if path.exists():
                saved = json.loads(path.read_text(encoding="utf-8"))
                age = (msk.date() - datetime.strptime(saved["date"], "%Y-%m-%d").date()).days
                if 0 <= age <= 5:
                    full = saved.get("data") or {}
        except (OSError, ValueError, KeyError):
            full = {}
        if frac >= 0.9:
            try:
                path.write_text(json.dumps({"date": msk.strftime("%Y-%m-%d"), "data": today}), encoding="utf-8")
            except OSError as e:
                log.warning("срез оборота полного дня не сохранён: %s", e)
        if not full:
            return today
        reliable = frac >= 0.15
        return {k: max(float(full.get(k) or 0.0), float(today.get(k) or 0.0) if reliable else 0.0) for k in set(full) | set(today)}

    def _turnover_tinvest(self):
        """Оборот по дневным свечам T-Invest: максимум за последние два торговых дня (утром сегодняшний ещё пуст).
        Один запрос на инструмент, поэтому только кандидаты: акции TQBR и фьючерсы на акции/индексы."""
        inst = self.instruments()
        mcfg = self.s.get("moex") or {}
        min_days = int(mcfg.get("min_days_to_expiry", 20))
        today = datetime.now(timezone.utc).date()
        share_bases = {i["base"] for i in inst.values() if i["kind"] == "share"}
        allowed_idx = set(mcfg.get("allowed_future_bases") or ["IMOEX", "RTS"])
        cands = []
        for sym, it in inst.items():
            if it["kind"] == "future":
                if it["base"] not in share_bases and it["base"] not in allowed_idx:
                    continue
                if it["expiration"] and (datetime.strptime(it["expiration"], "%Y-%m-%d").date() - today).days < min_days:
                    continue
            cands.append(sym)
        out, t0 = {}, time.time()
        end_ts = int(time.time())
        for sym in cands:
            it = inst[sym]
            try:
                r = self.call("MarketDataService", "GetCandles", {"instrumentId": it["uid"], "interval": "CANDLE_INTERVAL_DAY",
                                                                  "from": iso(end_ts - 6 * 86400), "to": iso(end_ts)}, retries=1)
                vals = [float(c.get("volume") or 0) * q2f(c["close"]) * it["mult"] for c in r.get("candles", [])[-4:]]
                out[sym] = max(vals) if vals else 0.0
            except Exception as e:  # noqa: BLE001
                log.debug("оборот %s: %s", sym, e)
            time.sleep(1.0)    # фоновый расчёт не должен занимать общий лимит ретранслятора: ~60 запросов/мин
        log.info("Оборот по свечам T-Invest: %d инструментов за %.0f с", len(out), time.time() - t0)
        return out

    def universe(self):
        """Инструменты для торговли. Фьючерсы в приоритете: на один базовый актив берём один инструмент."""
        cfg = self.s.get("universe") or {}
        mcfg = self.s.get("moex") or {}
        inst = self.instruments()
        turnover = self._turnover_today()
        min_t = float(cfg.get("min_turnover_24h_usd") or 0)   # для moex это рубли
        min_days = int(mcfg.get("min_days_to_expiry", 20))
        today = datetime.now(timezone.utc).date()
        chosen = {}   # base -> (priority, turnover, symbol)
        share_bases = {i["base"] for i in inst.values() if i["kind"] == "share"}
        allowed_idx = set(mcfg.get("allowed_future_bases") or ["IMOEX", "RTS"])
        black = set(cfg.get("blacklist") or [])
        for sym, it in inst.items():
            if sym in black or it["base"] in black:   # стоп-лист действует и на фьючерс этой бумаги
                continue
            if self._too_expensive(sym, it):
                continue
            t = turnover.get(sym, 0.0)
            if it["kind"] == "future":
                # только фьючерсы на акции и индексы: валюта, нефть, золото живут по другим новостям
                if it["base"] not in share_bases and it["base"] not in allowed_idx:
                    continue
                if it["expiration"]:
                    days = (datetime.strptime(it["expiration"], "%Y-%m-%d").date() - today).days
                    if days < min_days:
                        continue
                    prio = 2
                else:
                    prio = 3   # вечный фьючерс: нет экспирации, торгуется вечером и в выходные
                if t < min_t * mcfg.get("futures_turnover_mult", 0.3):
                    continue
            else:
                prio = 1
                if t < min_t:
                    continue
            if mcfg.get("prefer", "futures") != "futures":
                prio = {1: 3, 2: 2, 3: 1}[prio]
            cur = chosen.get(it["base"])
            if cur is None or (prio, t) > (cur[0], cur[1]):
                chosen[it["base"]] = (prio, t, sym)
        ranked = sorted(chosen.values(), key=lambda x: -x[1])
        syms = [x[2] for x in ranked[: cfg.get("top_n_by_turnover", 25)]]
        for s in cfg.get("always") or []:
            if s in inst and s not in syms:
                syms.append(s)
        return syms

    def _too_expensive(self, sym, it):
        """Песочница списывает полную стоимость контракта: если он дороже max_margin_share капитала (фьючерс на РТС ~152 тыс.,
        на Лукойл ~62 тыс. при капитале 96 тыс.), открыть его нельзя. Такой контракт занимал место компании в списке,
        и акция Лукойла из-за него не сканировалась. На реальном счёте нужно только гарантийное обеспечение — там не действует."""
        if self.mode == "live" or it["kind"] != "future":
            return False
        cap = float(self.s.get("risk.capital_cap_usd") or 0) * float(self.s.get("risk.max_margin_share") or 0.6)
        px = getattr(self, "_px", {}).get(sym)
        return bool(cap and px and px * it["mult"] > cap)

    def news_pick(self, base):
        """Один инструмент на компанию для бумаги из новости: вечный фьючерс, иначе квартальный, иначе акция.
        Стоп-лист, срок до экспирации и минимальная ликвидность действуют и здесь: раньше новость важности 6+ про бумагу
        из стоп-листа добавляла её в скан на 72 часа, иногда ближайшим контрактом за пару дней до экспирации."""
        cfg = self.s.get("universe") or {}
        mcfg = self.s.get("moex") or {}
        black = set(cfg.get("blacklist") or [])
        if base in black:
            return None
        inst = self.instruments()
        turn = self._turn_cache or {}
        floor = float(cfg.get("min_turnover_24h_usd") or 0) * float(mcfg.get("news_turnover_mult", 0.2))
        min_days = int(mcfg.get("min_days_to_expiry", 20))
        today = datetime.now(timezone.utc).date()
        prefer_fut = mcfg.get("prefer", "futures") == "futures"
        cands = []
        for sym in self.symbols_for_base(base):
            it = inst[sym]
            if sym in black or self._too_expensive(sym, it):
                continue
            need = floor
            if it["kind"] == "future":
                if it.get("expiration"):
                    if (datetime.strptime(it["expiration"], "%Y-%m-%d").date() - today).days < min_days:
                        continue
                    prio = 2
                else:
                    prio = 3
                need = floor * float(mcfg.get("futures_turnover_mult", 0.3))
            else:
                prio = 1
            if turn and float(turn.get(sym) or 0.0) < need:
                continue   # неликвид: спред и проскальзывание съедят идею
            cands.append((prio if prefer_fut else 4 - prio, float(turn.get(sym) or 0.0), sym))
        return max(cands)[2] if cands else None

    # ---------- рыночные данные ----------
    def klines(self, symbol, interval="60", limit=200, end=None):
        it = self.instruments()[symbol]
        return self.candles_uid(it["uid"], interval, limit, end, it["mult"])

    def candles_uid(self, uid, interval="60", limit=200, end=None, mult=1.0):
        """Свечи по uid инструмента: акции, фьючерсы и индикативы (Brent, RGBI, курс доллара).
        Для «живых» запросов (end не задан) свечи хранятся в памяти и докачивается только свежий хвост: раньше каждый скан
        заново качал 300 часовых свечей четырьмя запросами и 100 пятнадцатиминутных тремя — 10–15 запросов на бумагу,
        200–270 секунд на 45 бумаг. Раз в 60–90 минут каждая серия перекачивается целиком (сверка с брокером)."""
        interval = str(interval)
        if end is None and self.s.get("moex.candle_cache", True):
            return self._candles_cached(uid, interval, limit, mult)
        return self._candles_fetch(uid, interval, limit, end, mult).tail(limit).reset_index(drop=True)

    def _candles_cached(self, uid, interval, limit, mult):
        key, now = (uid, interval), time.time()
        mins = INTERVALS[interval][1]
        with self._clock:
            ent = self._ccache.get(key)
        if ent is None or len(ent["df"]) < limit or now - ent["full_ts"] > ent["full_every"]:
            df = self._candles_fetch(uid, interval, limit, None, mult).tail(max(limit, 320)).reset_index(drop=True)
            # перекачка целиком у разных бумаг в разное время, чтобы не попадала на один скан
            ent = {"df": df, "full_ts": now, "tail_ts": now, "full_every": 3600 + zlib.crc32(f"{uid}:{interval}".encode()) % 1800}
        elif now - ent["tail_ts"] >= 20:
            # хвост: от предпоследней сохранённой свечи до сейчас (незакрытая свеча и последняя закрытая обновляются)
            start = int(ent["df"]["ts"].iloc[-1] / 1000) - 2 * mins * 60
            new = self._candles_fetch(uid, interval, 0, None, mult, start_ts=start)
            df = pd.concat([ent["df"], new]).drop_duplicates("ts", keep="last").sort_values("ts")
            df = df.tail(max(limit, 320)).reset_index(drop=True)
            ent = dict(ent, df=df, tail_ts=now)
        with self._clock:
            self._ccache[key] = ent
            if len(self._ccache) > 2000:
                self._ccache.clear()
        return ent["df"].tail(limit).reset_index(drop=True).copy()

    def _candles_fetch(self, uid, interval, limit, end, mult, start_ts=None):
        """Запрос свечей у брокера кусками по допустимому периоду. start_ts задан — всё от него до конца; иначе с запасом под limit."""
        iv, mins = INTERVALS[str(interval)]
        end_ts = int(end / 1000) if end else int(time.time())
        if start_ts is None:
            # торги не круглосуточные: берём запас по времени, чтобы набрать нужное число свечей
            need_sec = int(limit * mins * 60 * (2.2 if mins >= 1440 else 1.9)) + 86400
            start_ts = end_ts - need_sec
        span = MAX_SPAN_MIN[str(interval)] * 60
        rows = []
        cur = start_ts
        while cur < end_ts:
            to = min(end_ts, cur + span)
            r = self.call("MarketDataService", "GetCandles", {"instrumentId": uid, "interval": iv,
                                                              "from": iso(cur), "to": iso(to)})
            for c in r.get("candles", []):
                ts = int(datetime.strptime(c["time"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000)
                vol = float(c.get("volume") or 0)
                o, h, l, cl = q2f(c["open"]), q2f(c["high"]), q2f(c["low"]), q2f(c["close"])
                rows.append([ts, o, h, l, cl, vol, vol * cl * mult])
            cur = to
        cols = ["ts", "open", "high", "low", "close", "volume", "turnover"]
        return pd.DataFrame(rows, columns=cols).drop_duplicates("ts").sort_values("ts").reset_index(drop=True)

    def last_price(self, symbol):
        it = self.instruments()[symbol]
        r = self.call("MarketDataService", "GetLastPrices", {"instrumentId": [it["uid"]]})
        px = q2f(r["lastPrices"][0]["price"]) if r.get("lastPrices") else 0.0
        if px:
            self._px[symbol] = px
        return px

    def ticker(self, symbol):
        it = self.instruments()[symbol]
        last = self.last_price(symbol)
        d = self.klines(symbol, "60", 30)
        prev = float(d["close"].iloc[-25]) if len(d) >= 25 else (float(d["close"].iloc[0]) if len(d) else last)
        turnover = float(d["turnover"].tail(24).sum()) if len(d) else 0.0
        return {"last": last, "mark": last, "funding": 0.0, "oi_value": 0.0,
                "pct24h": (last / prev - 1) * 100 if prev else 0.0, "turnover24h": turnover,
                "high24h": float(d["high"].tail(24).max()) if len(d) else last,
                "low24h": float(d["low"].tail(24).min()) if len(d) else last, "kind": it["kind"]}

    def open_interest(self, symbol, hours=24):
        return []

    def long_short_ratio(self, symbol):
        return None

    def announcements(self, limit=20):
        return []

    # ---------- киты: крупные сделки и перекос стакана ----------
    def whale_flow(self, symbol, minutes=60, big_rub=None):
        """Доля крупных сделок: (покупки − продажи)/всего по сделкам крупнее big_rub за последние minutes."""
        it = self.instruments()[symbol]
        mcfg = self.s.get("moex") or {}
        big = big_rub or (mcfg.get("big_trade_rub_future", 3_000_000) if it["kind"] == "future" else mcfg.get("big_trade_rub_share", 2_000_000))
        now = time.time()
        try:
            r = self.call("MarketDataService", "GetLastTrades", {"instrumentId": it["uid"], "from": iso(now - minutes * 60), "to": iso(now)})
        except Exception as e:  # noqa: BLE001
            log.debug("whale_flow %s: %s", symbol, e)
            return None
        buy = sell = n_big = 0.0
        for t in r.get("trades", []):
            rub = q2f(t["price"]) * float(t.get("quantity") or 0) * it["mult"]
            if rub < big:
                continue
            n_big += 1
            if t.get("direction") == "TRADE_DIRECTION_BUY":
                buy += rub
            else:
                sell += rub
        tot = buy + sell
        return {"imbalance": (buy - sell) / tot if tot else 0.0, "big_rub": tot, "n_big": int(n_big),
                "big_share": None}

    def price_limits(self, symbol):
        """Границы ценового коридора на сегодня (limitDown, limitUp) или None: заявка вне коридора биржа отклоняет."""
        it = self.instruments()[symbol]
        ob = self.call("MarketDataService", "GetOrderBook", {"instrumentId": it["uid"], "depth": 1})
        lo, hi = q2f(ob.get("limitDown")), q2f(ob.get("limitUp"))
        return (lo, hi) if lo and hi and hi > lo else None

    def book_imbalance(self, symbol, depth=20):
        it = self.instruments()[symbol]
        try:
            ob = self.call("MarketDataService", "GetOrderBook", {"instrumentId": it["uid"], "depth": depth})
        except Exception as e:  # noqa: BLE001
            log.debug("book %s: %s", symbol, e)
            return 0.0
        b = sum(float(x.get("quantity") or 0) for x in ob.get("bids", []))
        a = sum(float(x.get("quantity") or 0) for x in ob.get("asks", []))
        return (b - a) / (b + a) if (a + b) else 0.0

    def trading_open(self, symbol):
        it = self.instruments()[symbol]
        try:
            r = self.call("MarketDataService", "GetTradingStatus", {"instrumentId": it["uid"]})
            return r.get("tradingStatus") == "SECURITY_TRADING_STATUS_NORMAL_TRADING" and bool(r.get("marketOrderAvailableFlag"))
        except Exception as e:  # noqa: BLE001
            log.debug("trading status %s: %s", symbol, e)
            return False

    # ---------- счёт ----------
    price_based_pnl = True   # результат сделки считаем по ценам входа и выхода, а не по платежам брокера

    def balances(self):
        """Капитал = деньги + акции по рынку + вариационная маржа открытых фьючерсов.
        totalAmountPortfolio не годится: в нём фьючерсы учтены по полной стоимости контракта."""
        inst = self.instruments()   # заполняет карты figi/uid -> тикер до их использования
        p = self.call("OperationsService", "GetPortfolio", {"accountId": self.account, "currency": "RUB"})
        cash = shares_upnl = fut_upnl = 0.0
        for x in p.get("positions", []):
            kind = x.get("instrumentType")
            if kind == "currency":
                cash += q2f(x.get("quantity"))
            elif kind == "futures":
                if x.get("varMargin") is not None:
                    fut_upnl += q2f(x.get("varMargin"))
                else:
                    sym = self._figi2sym.get(x.get("figi")) or self._uid2sym.get(x.get("instrumentUid"))
                    mult = inst[sym]["mult"] if sym in inst else 1.0
                    fut_upnl += (q2f(x.get("currentPrice")) - q2f(x.get("averagePositionPrice"))) * q2f(x.get("quantity")) * mult
            else:
                shares_upnl += q2f(x.get("expectedYield"))
        shares = q2f(p.get("totalAmountShares")) + q2f(p.get("totalAmountEtf")) + q2f(p.get("totalAmountBonds"))
        # Проверено по операциям песочницы 22.10: деньги = пополнение + платежи по акциям + комиссии + вариационная
        # маржа (списывается при клиринге в 21:00). Платежи по фьючерсам в деньги не попадают, а их стоимость
        # в totalAmountPortfolio — фикция. Поэтому капитал = деньги + акции по рынку + вармаржа открытых фьючерсов.
        equity = cash + shares + fut_upnl
        upnl = fut_upnl + shares_upnl
        return {"equity": equity, "wallet": equity - upnl, "unrealised": upnl, "available": cash, "total_equity": equity}

    def _realized_futures(self, portfolio):
        """Песочница: сумма платежей по сделкам с фьючерсами плюс стоимость открытых контрактов по цене входа
        = реализованный результат, который настоящий брокер провёл бы по деньгам при клиринге. Кэш 60 с."""
        now = time.time()
        if now - self._realized_ts < 60:
            return self._realized
        inst = self.instruments()
        r = self.call("OperationsService", "GetOperations", {"accountId": self.account, "from": "2026-10-01T00:00:00Z",
                                                              "to": iso(now + 60), "state": "OPERATION_STATE_EXECUTED"})
        total = 0.0
        for op in r.get("operations", []):
            if op.get("instrumentType") == "futures" and op.get("operationType") in ("OPERATION_TYPE_BUY", "OPERATION_TYPE_SELL"):
                total += q2f(op.get("payment"))
        for x in portfolio.get("positions", []):
            if x.get("instrumentType") == "futures":
                sym = self._figi2sym.get(x.get("figi")) or self._uid2sym.get(x.get("instrumentUid"))
                mult = inst[sym]["mult"] if sym in inst else 1.0
                total += q2f(x.get("quantity")) * q2f(x.get("averagePositionPrice")) * mult
        self._realized, self._realized_ts = total, now
        return total

    def equity(self):
        b = self.balances()
        return b["equity"], b["available"]

    def _stop_orders(self, strict=False):
        """strict=True: при ошибке запроса исключение, а не пустой список (пустой список = «стопов нет», и решения по нему опасны)."""
        try:
            return self.call("StopOrdersService", "GetStopOrders", {"accountId": self.account}).get("stopOrders", [])
        except Exception as e:  # noqa: BLE001
            if strict:
                raise
            log.debug("stop orders: %s", e)
            return []

    def cancel_stop(self, stop_id):
        self.call("StopOrdersService", "CancelStopOrder", {"accountId": self.account, "stopOrderId": stop_id})

    def orders_view(self):
        """Все стоп-заявки и активные заявки счёта для сверки с журналом. Ошибка запроса — исключение."""
        self.instruments()
        so = self.call("StopOrdersService", "GetStopOrders", {"accountId": self.account}).get("stopOrders", [])
        od = self.call("OrdersService", "GetOrders", {"accountId": self.account}).get("orders", [])

        def sym_of(x):
            return self._figi2sym.get(x.get("figi")) or self._uid2sym.get(x.get("instrumentUid"))

        stops = [{"symbol": sym_of(x), "id": x["stopOrderId"], "qty": float(x.get("lotsRequested") or 0), "price": q2f(x.get("stopPrice")),
                  "side": "Buy" if "BUY" in (x.get("direction") or "") else "Sell"} for x in so if sym_of(x)]
        limits = [{"symbol": sym_of(x), "id": x["orderId"], "qty": float(x.get("lotsRequested") or 0) - float(x.get("lotsExecuted") or 0),
                   "price": q2f(x.get("initialSecurityPrice")), "side": "Buy" if "BUY" in (x.get("direction") or "") else "Sell"}
                  for x in od if sym_of(x)]
        return {"stops": stops, "limits": limits}

    def positions(self):
        p = self.call("OperationsService", "GetPortfolio", {"accountId": self.account})
        inst = self.instruments()
        stops = {}
        for so in self._stop_orders():
            sym = self._figi2sym.get(so.get("figi")) or self._uid2sym.get(so.get("instrumentUid"))
            if sym:
                stops[sym] = q2f(so.get("stopPrice"))
        out = []
        eq = self.balances()["equity"] or 1.0   # totalAmountPortfolio завышен на стоимость контрактов
        for x in p.get("positions", []):
            if x.get("instrumentType") not in ("share", "futures"):
                continue
            sym = self._figi2sym.get(x.get("figi")) or self._uid2sym.get(x.get("instrumentUid"))
            if not sym:
                continue
            lots = q2f(x.get("quantityLots")) if x.get("quantityLots") else q2f(x.get("quantity")) / inst[sym]["lot"]
            if abs(lots) < 1e-9:
                continue
            entry = q2f(x.get("averagePositionPrice")) or q2f(x.get("averagePositionPriceFifo"))
            mark = q2f(x.get("currentPrice")) or entry
            mult = inst[sym]["mult"]
            if self.price_based_pnl:
                # по ценам, как и результат сделки: varMargin песочницы по фьючерсу через клиринг завышает/занижает плюс
                upnl = (mark - entry) * lots * mult
            elif x.get("instrumentType") == "futures":
                upnl = q2f(x.get("varMargin")) if x.get("varMargin") is not None else (mark - entry) * lots * mult
            else:
                upnl = q2f(x.get("expectedYield")) if x.get("expectedYield") is not None else (mark - entry) * lots * mult
            out.append({"symbol": sym, "side": "Buy" if lots > 0 else "Sell", "qty": abs(lots), "entry": entry, "mark": mark,
                        "mark_ok": bool(q2f(x.get("currentPrice"))),   # False: брокер не прислал цену, mark = цена входа
                        "upnl": upnl, "mult": mult, "leverage": abs(lots) * mark * mult / eq,
                        "sl": stops.get(sym, 0.0), "tp": 0.0, "liq": 0.0})
        return out

    def set_leverage(self, symbol, lev):
        return   # плечо на Мосбирже задаётся брокером через маржу, менять нечего

    def free_margin(self):
        """Песочница: ордер проходит только в пределах свободных денег (и для акций, и для фьючерсов)."""
        if self.mode == "live":
            return None
        try:
            return self.balances()["available"]
        except Exception:  # noqa: BLE001
            return None

    def margin_per_lot(self, symbol, side="Buy"):
        it = self.instruments()[symbol]
        if self.mode != "live" and it["kind"] != "future":
            try:
                return self.last_price(symbol) * it["lot"]   # песочница списывает за акции полную стоимость
            except Exception:  # noqa: BLE001
                return None
        if it["kind"] != "future":
            return None
        if self.mode != "live":
            # песочница не знает гарантийного обеспечения: контракт списывает полную стоимость с денег,
            # иначе ордер отвергается с «Not enough balance». Считаем ГО = стоимость контракта.
            try:
                return self.last_price(symbol) * it["mult"] * it["lot"]
            except Exception:  # noqa: BLE001
                return None
        try:
            m = self.call("InstrumentsService", "GetFuturesMargin", {"instrumentId": it["uid"]})
            return q2f(m.get("initialMarginOnBuy" if side == "Buy" else "initialMarginOnSell"))
        except Exception as e:  # noqa: BLE001
            log.debug("margin %s: %s", symbol, e)
            return None

    # ---------- ордера ----------
    def _order(self, symbol, side, qty, order_type, price=None):
        it = self.instruments()[symbol]
        body = {"instrumentId": it["uid"], "quantity": str(int(Decimal(self.fmt_qty(symbol, qty)))),
                "direction": "ORDER_DIRECTION_BUY" if side == "Buy" else "ORDER_DIRECTION_SELL",
                "accountId": self.account, "orderType": order_type, "orderId": str(uuid.uuid4())}
        if price is not None:
            body["price"] = f2q(self.fmt_price(symbol, price))
        r = self.call("OrdersService", "PostOrder", body)
        return r

    def place_market(self, symbol, side, qty, sl=None, tp=None, reduce_only=False):
        r = self._order(symbol, side, qty, "ORDER_TYPE_MARKET")
        oid = r["orderId"]
        self._last_fill = {"price": q2f(r.get("executedOrderPrice")), "commission": q2f(r.get("executedCommission")),
                           "lots": int(r.get("lotsExecuted") or 0)}
        if sl:
            try:
                self.set_stop_loss(symbol, sl, side_closing="Sell" if side == "Buy" else "Buy")
            except Exception as e:  # noqa: BLE001
                log.warning("%s: стоп-заявка не выставлена: %s", symbol, e)
        return oid

    def place_limit(self, symbol, side, qty, price, reduce_only=True):
        r = self._order(symbol, side, qty, "ORDER_TYPE_LIMIT", price)
        return r["orderId"]

    def order_status(self, symbol, order_id):
        try:
            o = self.call("OrdersService", "GetOrderState", {"accountId": self.account, "orderId": order_id})
        except Exception as e:  # noqa: BLE001
            log.debug("order_status %s: %s", order_id, e)
            return None
        st = o.get("executionReportStatus", "")
        status = {"EXECUTION_REPORT_STATUS_FILL": "Filled", "EXECUTION_REPORT_STATUS_REJECTED": "Rejected",
                  "EXECUTION_REPORT_STATUS_CANCELLED": "Cancelled", "EXECUTION_REPORT_STATUS_NEW": "New",
                  "EXECUTION_REPORT_STATUS_PARTIALLYFILL": "PartiallyFilled"}.get(st, st)
        return {"status": status, "filled": float(o.get("lotsExecuted") or 0), "avg": q2f(o.get("averagePositionPrice")),
                "qty": float(o.get("lotsRequested") or 0), "commission": q2f(o.get("executedCommission"))}

    def cancel_order(self, symbol, order_id):
        try:
            self.call("OrdersService", "CancelOrder", {"accountId": self.account, "orderId": order_id})
        except Exception as e:  # noqa: BLE001
            log.debug("cancel_order %s: %s", order_id, e)

    def open_orders_strict(self, symbol):
        """Как open_orders, но при ошибке запроса исключение: пустой список означал бы «заявок нет», и тейк выставился бы дублем."""
        it = self.instruments()[symbol]
        lst = self.call("OrdersService", "GetOrders", {"accountId": self.account}).get("orders", [])
        return [o for o in lst if o.get("instrumentUid") == it["uid"] or o.get("figi") == it["figi"]]

    def open_orders(self, symbol):
        it = self.instruments()[symbol]
        try:
            lst = self.call("OrdersService", "GetOrders", {"accountId": self.account}).get("orders", [])
        except Exception:  # noqa: BLE001
            return []
        return [o for o in lst if o.get("instrumentUid") == it["uid"] or o.get("figi") == it["figi"]]

    def cancel_all(self, symbol):
        for o in self.open_orders(symbol):
            self.cancel_order(symbol, o["orderId"])
        it = self.instruments()[symbol]
        for so in self._stop_orders():
            if so.get("instrumentUid") == it["uid"] or so.get("figi") == it["figi"]:
                try:
                    self.call("StopOrdersService", "CancelStopOrder", {"accountId": self.account, "stopOrderId": so["stopOrderId"]})
                except Exception as e:  # noqa: BLE001
                    log.debug("cancel stop %s: %s", so["stopOrderId"], e)

    def set_stop_loss(self, symbol, sl, side_closing=None):
        """Стоп-заявка на всю позицию. Для шорта закрывающая сторона Buy.
        Сначала ставится новая заявка, потом снимаются старые: если новая не принята, прежняя защита остаётся
        (раньше старую снимали первой, и при сбое позиция оставалась без стопа). Список старых заявок получаем строго:
        если брокер его не отдал, ничего не меняем — иначе рядом остались бы две стоп-заявки."""
        it = self.instruments()[symbol]
        pos = [p for p in self.positions() if p["symbol"] == symbol]
        if side_closing is None:
            if not pos:
                return
            side_closing = "Sell" if pos[0]["side"] == "Buy" else "Buy"
            qty = pos[0]["qty"]
        else:
            qty = pos[0]["qty"] if pos else (getattr(self, "_last_fill", None) or {}).get("lots", 1)
        old = [so for so in self._stop_orders(strict=True) if so.get("instrumentUid") == it["uid"] or so.get("figi") == it["figi"]]

        def post(price, lots):
            self.call("StopOrdersService", "PostStopOrder", {
                "instrumentId": it["uid"], "quantity": str(int(lots)), "stopPrice": f2q(self.fmt_price(symbol, price)),
                "direction": "STOP_ORDER_DIRECTION_SELL" if side_closing == "Sell" else "STOP_ORDER_DIRECTION_BUY",
                "accountId": self.account, "expirationType": "STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL",
                "stopOrderType": "STOP_ORDER_TYPE_STOP_LOSS"})

        def cancel_old():
            for so in old:
                try:
                    self.cancel_stop(so["stopOrderId"])
                except Exception as e:  # noqa: BLE001
                    log.warning("%s: старая стоп-заявка не снята (%s), снимет сверка заявок", symbol, str(e)[:80])

        try:
            post(sl, qty)
        except Exception as e1:  # noqa: BLE001
            if not old:
                raise
            # брокер не принял вторую стоп-заявку при живой первой: меняем по-старому, а при неудаче возвращаем прежний уровень
            log.warning("%s: новая стоп-заявка рядом со старой не принята (%s), меняю через снятие", symbol, str(e1)[:100])
            cancel_old()
            try:
                post(sl, qty)
            except Exception:
                try:
                    post(q2f(old[0].get("stopPrice")), qty)
                except Exception as e3:  # noqa: BLE001
                    log.error("%s: стоп-заявка не восстановлена: %s", symbol, str(e3)[:100])
                raise
            return
        cancel_old()

    def close_position(self, symbol):
        for p in self.positions():
            if p["symbol"] == symbol:
                side = "Sell" if p["side"] == "Buy" else "Buy"
                return self.place_market(symbol, side, p["qty"], reduce_only=True)
        return None

    def closed_pnl(self, symbol=None, start_ms=None, limit=50, entry=None, side=None, end_ms=None):
        """Результат по инструменту с момента start_ms, в рублях, уже с учётом комиссии.
        Если переданы entry и side сделки: pnl = сумма по закрывающим сделкам (цена − вход) × лоты × шаг − комиссии.
        Иначе (разведка): продажи − покупки − комиссии по платежам брокера за период."""
        start = int(start_ms / 1000) if start_ms else int(time.time()) - 7 * 86400
        r = self.call("OperationsService", "GetOperations", {"accountId": self.account, "from": iso(start),
                                                              "to": iso(end_ms / 1000 if end_ms else time.time() + 60),
                                                              "state": "OPERATION_STATE_EXECUTED"})
        closing = "OPERATION_TYPE_SELL" if side == "Buy" else "OPERATION_TYPE_BUY"
        by = {}
        for op in r.get("operations", []):
            sym = self._figi2sym.get(op.get("figi")) or self._uid2sym.get(op.get("instrumentUid"))
            if not sym or (symbol and sym != symbol):
                continue
            d = by.setdefault(sym, {"symbol": sym, "pnl": 0.0, "fees": 0.0, "exit": 0.0, "ts": 0, "side": "", "qty": 0.0, "entry": entry or 0.0})
            pay = q2f(op.get("payment"))
            typ = op.get("operationType", "")
            if typ == "OPERATION_TYPE_BROKER_FEE" or "FEE" in typ or "COMMISSION" in typ:
                d["fees"] += -pay
                d["pnl"] += pay
            elif entry is None:
                d["pnl"] += pay
            ts = int(datetime.strptime(op["date"][:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000)
            if typ in ("OPERATION_TYPE_BUY", "OPERATION_TYPE_SELL"):
                price, qty = q2f(op.get("price")), float(op.get("quantity") or 0)
                if entry is not None and typ == closing:
                    # у акций quantity в операции — штуки (множитель лота уже внутри), у фьючерсов — контракты × стоимость шага
                    per_unit = self.mult(sym) if self.instruments()[sym]["kind"] == "future" else 1.0
                    d["pnl"] += (price - entry) * qty * per_unit * (1 if side == "Buy" else -1)
                    d["qty"] += qty
                if (entry is None or typ == closing) and ts >= d["ts"]:
                    d["ts"], d["exit"] = ts, price
                    d["side"] = "Sell" if typ == "OPERATION_TYPE_SELL" else "Buy"
                    if entry is None:
                        d["qty"] = qty
        return [d for d in by.values() if d["ts"]]

    # ---------- округления ----------
    def fmt_qty(self, symbol, qty):
        return str(int(Decimal(str(qty)).to_integral_value(rounding="ROUND_DOWN")))

    def fmt_price(self, symbol, price):
        tick = self.instruments()[symbol]["tick"]
        p = (Decimal(str(price)) / tick).to_integral_value(rounding="ROUND_HALF_UP") * tick
        return float(p)

    def qty_ok(self, symbol, qty, price):
        it = self.instruments()[symbol]
        lots = int(Decimal(str(qty)).to_integral_value(rounding="ROUND_DOWN"))
        return lots >= 1 and lots * price * it["mult"] >= float(self.s.get("risk.min_notional_usd") or 0)
