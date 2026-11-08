"""Ожидания и расхождения по правилам, без модели (решение владельца 08.11.2026: «сбор ожиданий из чётких источников — да»).

Что хранится:
  expect_items — ожидания, вытащенные из лент до события: консенсус по ставке ЦБ (Интерфакс/РБК/Ведомости цитируются
                 каналами), прогнозы дивидендов по компании, факт недельной инфляции (ожидание следующей недели = эта).
  surprises    — журнал расхождений: что ждали, что вышло, размер в «типичных» отклонениях, подтверждение рынком,
                 решение бота и результат через две недели. По нему обучаемся: какие источники врут, какие пороги работают.

Пять шагов оценки (согласованы 08.11): 1 источник ожидания (консенсус → рынок → тексты за 7 дней);
2 актуальность (не старше 7 дней, не меньше двух источников — иначе «слабое», порог вдвое выше);
3 размер = (факт − ожидание) / типичный разброс; меньше 0.5 — «как ждали», сделки нет; 4 подтверждение рынком
в первые часы (облигации для ставки, сама бумага для отчёта) — делает bot/eventtrades.py; 5 запись в журнал.
История 2019–2026 (research/news_expectations.py, news_earnings_cpi.py): направление даёт только расхождение, не слова."""
import logging
import re
import time
from collections import Counter
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger("expect")
MSK = ZoneInfo("Europe/Moscow")

FOREIGN = re.compile(r"Турц|ЕЦБ|ФРС|Федрезерв|Бразил|Инди|Украин|Казахстан|Белорус|Банк Англии|Швеци|Норвег|Япони|Китая|Европ|Нацбанк", re.I)
CBR_EXP = re.compile(r"(ожида\w+|прогноз\w*|консенсус\w*|большинств\w+|опрос\w*)[^\n;]{0,120}?(до|на уровне|уровн\w*|составит|в|к|сохран\w+ на|сохранени\w+)?\s?(\d{1,2})[.,]?(\d{0,2})\s?%", re.I)
CBR_WORDS = re.compile(r"ключев|ставк", re.I)
EXP_SOURCES = re.compile(r"Интерфакс|РБК|Ведомост|Reuters|Рейтер|Bloomberg|опрос|консенсус|SberCIB|ВТБ|Альфа|Газпромбанк|БКС|Финам|Т-Инвест|Совкомбанк|Ренессанс", re.I)
NUM = r"(?<![\d,.])(\d{1,3}(?:[  ]\d{3})*(?:[.,]\d{1,4})?)"
DIV_EXP = re.compile(r"(ожида\w+|прогноз\w*|консенсус\w*|может составить|оценива\w+|рассчитыва\w+)[^.\n]{0,100}?дивиденд\w*[^.\n]{0,60}?(в размере|на акцию[:\s–-]*|около|порядка|до|–|-|—)?\s?" + NUM + r"\s?(руб|₽)", re.I)
DIV_EXP2 = re.compile(r"дивиденд\w*[^.\n]{0,80}?(ожида\w+|прогноз\w*|консенсус\w*|может составить|оценива\w+)[^.\n]{0,60}?(в размере|около|порядка|до|–|-|—)?\s?" + NUM + r"\s?(руб|₽)", re.I)
DIV_REC = re.compile(r"рекомендова\w*[^.\n]{0,120}?дивиденд\w*[^.\n]{0,60}?(в размере|на акцию[:\s–-]*|по|–|-|—)\s?" + NUM + r"\s?(руб|₽)", re.I)
DIV_REC2 = re.compile(r"дивиденд\w*[^.\n]{0,40}?(в размере|на акцию[:\s–-]*)\s?" + NUM + r"\s?(руб|₽)[^.\n]{0,80}?рекомендова", re.I)
DIV_NONE = re.compile(r"(не выплачивать|не рекомендова|отказ\w+ от (выплаты )?дивиденд|без дивиденд|дивиденды не будут|отмен\w+ дивиденд)", re.I)
CPI_FACT = re.compile(r"(инфляци\w*|цен\w*)[^.\n]{0,80}?(составил\w*|на уровне|замедлил\w* до|ускорил\w* до|выросл\w* на)\s(-?\d[.,]\d{1,2})\s?%", re.I)
CPI_WEEK = re.compile(r"недел", re.I)
BEAT = re.compile(r"(выше|лучше|превысил\w*|опередил\w*|сильнее)\s(\w+\s){0,3}(консенсус|ожидани|прогноз)", re.I)
MISS = re.compile(r"(ниже|хуже|не оправдал\w*|не дотянул\w*|слабее|разочаров\w*)\s(\w+\s){0,3}(консенсус|ожидани|прогноз)", re.I)
INLINE = re.compile(r"(совпал\w*|в рамках|в соответствии|на уровне|в русле)\s(\w+\s){0,2}(консенсус|ожидани|прогноз)", re.I)
REPORT = re.compile(r"отчита|отчёт|отчет|результат|МСФО|РСБУ|прибыл|выручк|EBITDA", re.I)
# консенсус по чистой прибыли с числом (Интерфакс в лентах, cbrstocks #Прогноз) и факт из отчёта — для РАЗМЕРА расхождения
BIGNUM = r"(\d{1,4}(?:[  ]\d{3})*(?:[.,]\d{1,3})?)"
NP_CONS = re.compile(r"консенсус[^\n]{0,200}?(чист\w+ прибыл\w*|прибыл\w*)[^\n]{0,100}?" + BIGNUM + r"\s?(млрд|трлн)", re.I)
NP_CONS2 = re.compile(r"(чист\w+ прибыл\w*)[^\n]{0,80}?(прогнозиру\w+|ожида\w+|консенсус\w*)[^\n]{0,100}?" + BIGNUM + r"\s?(млрд|трлн)", re.I)
NP_FACT = re.compile(r"чист\w+ прибыл\w*[^\n]{0,120}?(составил\w*|выросл\w* до|увеличил\w* до|снизил\w* до|сократил\w* до|упал\w* до|в размере|достигл\w*)\s?" + BIGNUM + r"\s?(млрд|трлн)", re.I)
NP_PERIOD = re.compile(r"МСФО|РСБУ|квартал|полугод|9 месяц|девять месяц|за год", re.I)
HASHTAG = re.compile(r"#([A-Z]{3,5})\b")
NAMES = {"SBER": r"Сбер\w*", "GAZP": r"Газпром\b|Газпрома\b|Газпрому\b", "LKOH": r"Лукойл", "ROSN": r"Роснефт", "NVTK": r"Новат[эе]к", "GMKN": r"Норникел|Норильск",
         "PLZL": r"Полюс", "X5": r"\bX5\b|Пятёрочк|Пятерочк|ИКС 5", "YDEX": r"Яндекс", "TATN": r"Татнефт", "SNGS": r"Сургутнефтегаз", "MGNT": r"Магнит\b",
         "VTBR": r"\bВТБ\b", "T": r"Т-Банк|Тинькофф|Т-Технологи|TCS\b", "ALRS": r"Алроса|АЛРОСА", "CHMF": r"Северстал", "NLMK": r"\bНЛМК\b", "MAGN": r"\bММК\b",
         "RUAL": r"Русал|РУСАЛ", "AFLT": r"Аэрофлот", "MTSS": r"\bМТС\b", "RTKM": r"Ростелеком", "PHOR": r"Фосагро|ФосАгро", "MOEX": r"Мосбирж\w* (объяв|отчит|рекоменд|дивиденд)",
         "SMLT": r"Самолет|Самолёт", "PIKK": r"\bПИК\b", "SVCB": r"Совкомбанк", "POSI": r"Positive|Позитив", "ASTR": r"Астра\b", "HEAD": r"HeadHunter|Хедхантер|\bhh\.ru",
         "OZON": r"\bOzon\b|Озон\b", "VKCO": r"\bVK\b|ВКонтакте", "SIBN": r"Газпром нефт", "IRAO": r"Интер РАО", "HYDR": r"РусГидро|Русгидро", "MTLR": r"Мечел",
         "SGZH": r"Сегеж", "TRNFP": r"Транснефт", "BSPB": r"Банк Санкт-Петербург|БСПБ", "FLOT": r"Совкомфлот", "NMTP": r"\bНМТП\b", "FEES": r"Россети\b|ФСК",
         "ENPG": r"Эн\+|En\+", "UPRO": r"Юнипро", "AFKS": r"АФК Система", "RENI": r"Ренессанс страхован", "LENT": r"Лента\b", "MVID": r"М\.Видео",
         "BELU": r"Белуга|Novabev", "ETLN": r"Эталон", "LSRG": r"\bЛСР\b", "SELG": r"Селигдар", "UGLD": r"ЮГК|Южуралзолото", "CNRU": r"Циан|ЦИАН", "DOMRF": r"ДОМ\.РФ"}
NAME_RX = {k: re.compile(v, re.I) for k, v in NAMES.items()}
# типичный разброс расхождения: ставка в п.п., дивиденд в долях, инфляция в п.п. за неделю (research/news_expectations.py, news_earnings_cpi.py)
TYPICAL = {"cbr": 0.5, "div": 0.15, "cpi": 0.05, "np": 0.15}   # np — чистая прибыль против консенсуса, в долях


def bignum(s, unit):
    v = float(str(s).replace(" ", "").replace(" ", "").replace(",", "."))
    return v * 1000 if unit.lower().startswith("трлн") else v

SCHEMA = """
CREATE TABLE IF NOT EXISTS expect_items(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, kind TEXT, base TEXT, value REAL,
  source TEXT, title TEXT, news_id TEXT, UNIQUE(kind, base, source, value, ts));
CREATE INDEX IF NOT EXISTS expect_items_k ON expect_items(kind, base, ts);
CREATE TABLE IF NOT EXISTS surprises(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, kind TEXT, base TEXT, expected REAL, actual REAL,
  surprise REAL, size REAL, n_sources INTEGER, exp_age_h REAL, confirm_ts INTEGER, confirm_val REAL, confirmed INTEGER,
  decision TEXT, trade_id INTEGER, news_id TEXT, title TEXT, result_ts INTEGER, result_pct REAL, note TEXT);
"""


def num(s):
    return float(str(s).replace(" ", "").replace(" ", "").replace(",", "."))


def company_of(text, affected=None):
    """Компания новости: один тикер от модели → он; хэштег #SBER → он; одно название в первых 300 символах → оно; иначе None."""
    if affected and len(affected) == 1:
        return affected[0]
    t = (text or "")[:300]
    tags = [h for h in HASHTAG.findall(t) if h in NAME_RX]
    if len(set(tags)) == 1:
        return tags[0]
    found = [k for k, rx in NAME_RX.items() if rx.search(t)]
    return found[0] if len(found) == 1 else None


def source_of(text):
    m = EXP_SOURCES.search(text or "")
    return m.group(0) if m else "лента"


class ExpectRules:
    def __init__(self, settings, journal):
        self.s, self.j = settings, journal
        with self.j.lock:
            self.j.db.executescript(SCHEMA)
            self.j.db.commit()

    # ---------- шаг 1: сбор ожиданий из сырых новостей ----------
    def ingest(self, item):
        """Вызывается на каждую новость до модели. Возвращает число записанных ожиданий."""
        text = f"{item.get('title', '')}. {item.get('summary', '')}"[:1200]
        ts = int(item.get("published_ts") or time.time())
        n = 0
        head = text[:150]
        if CBR_WORDS.search(text) and not FOREIGN.search(head):
            cur = self.current_rate()
            for m in CBR_EXP.finditer(text):
                v = float(f"{m.group(3)}.{m.group(4) or 0}")
                if 3 <= v <= 30 and (cur is None or abs(v - cur) <= 3.0):
                    n += self._add(ts, "cbr", "CBR", v, source_of(text), item.get("title", ""), item.get("id"))
                    break
        if "дивиденд" in text.lower():
            base = company_of(text)
            if base:
                m = DIV_EXP.search(text) or DIV_EXP2.search(text)
                if m:
                    v = num(m.group(3))
                    if v > 0:
                        n += self._add(ts, "div", base, v, source_of(text), item.get("title", ""), item.get("id"))
        if "консенсус" in text.lower() and "прибыл" in text.lower():
            base = company_of(text)
            m = NP_CONS.search(text[:700]) or NP_CONS2.search(text[:700])
            if base and m:
                v = bignum(m.group(2) if m.re is NP_CONS else m.group(3), m.group(3) if m.re is NP_CONS else m.group(4))
                if v > 0:
                    n += self._add(ts, "np", base, v, source_of(text), item.get("title", ""), item.get("id"))
        if "инфляци" in text.lower() and CPI_WEEK.search(text) and not FOREIGN.search(head):
            m = CPI_FACT.search(text)
            if m:
                v = num(m.group(3))
                if -0.5 <= v <= 1.5:
                    n += self._add(ts, "cpi", "CPI", v, "Росстат", item.get("title", ""), item.get("id"))
        return n

    def _add(self, ts, kind, base, value, source, title, news_id):
        try:
            self.j._run("INSERT OR IGNORE INTO expect_items(ts,kind,base,value,source,title,news_id) VALUES(?,?,?,?,?,?,?)",
                        (ts, kind, base, float(value), source, (title or "")[:200], news_id or ""))
            return 1
        except Exception as e:  # noqa: BLE001
            log.debug("ожидание не записано: %s", e)
            return 0

    def current_rate(self):
        v = self.s.get("moex.cbr_current_rate")
        try:
            return float(v) if v else None
        except (TypeError, ValueError):
            return None

    # ---------- шаг 2: ожидание с актуальностью ----------
    def expectation(self, kind, base, now=None, days=7):
        """(значение, число источников, возраст самого свежего в часах) или (None, 0, None). Значение — самое частое за окно."""
        now = now or time.time()
        rows = self.j._run("SELECT value, source, ts FROM expect_items WHERE kind=? AND base=? AND ts BETWEEN ? AND ? ORDER BY ts DESC",
                           (kind, base, int(now - days * 86400), int(now)), "all")
        if not rows:
            return None, 0, None
        if kind == "cpi":
            return float(rows[0]["value"]), 1, (now - rows[0]["ts"]) / 3600
        cnt = Counter(round(float(r["value"]), 2) for r in rows)
        value = cnt.most_common(1)[0][0]
        srcs = {r["source"] for r in rows if round(float(r["value"]), 2) == value}
        return value, max(1, len(srcs)), (now - rows[0]["ts"]) / 3600

    def cbr_consensus(self, now=None):
        """Консенсус по ставке: из лент за 7 дней; если там пусто — из config (moex.cbr_consensus). (значение, источник-строка, сильное ли)."""
        v, n, age = self.expectation("cbr", "CBR", now)
        if v is not None:
            return v, f"ленты: {n} источн., {age:.0f} ч назад", n >= 2
        cfg = self.s.get("moex.cbr_consensus")
        if cfg:
            return float(cfg), "config", False
        return None, "", False

    # ---------- шаг 3: размер расхождения ----------
    @staticmethod
    def size_of(kind, surprise):
        t = TYPICAL.get(kind)
        return abs(surprise) / t if t else abs(surprise)

    def threshold(self, strong):
        return 1.0 if strong else 2.0   # слабое ожидание (один источник) — порог вдвое выше

    # ---------- шаг 5: журнал ----------
    def record(self, kind, base, expected, actual, surprise, n_sources=0, exp_age_h=None, news_id="", title="", note=""):
        size = self.size_of(kind, surprise) if surprise is not None else None
        sid = self.j._run("INSERT INTO surprises(ts,kind,base,expected,actual,surprise,size,n_sources,exp_age_h,news_id,title,note,decision) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (int(time.time()), kind, base, expected, actual, surprise, size, int(n_sources or 0), exp_age_h, news_id or "",
                           (title or "")[:200], note or "", "ждём подтверждения"))
        log.info("расхождение %s %s: ждали %s, факт %s, размер %s", kind, base, expected, actual, None if size is None else round(size, 2))
        return sid

    def confirm(self, sid, value, confirmed, decision, trade_id=None):
        self.j._run("UPDATE surprises SET confirm_ts=?, confirm_val=?, confirmed=?, decision=?, trade_id=? WHERE id=?",
                    (int(time.time()), value, int(bool(confirmed)), decision, trade_id, sid))

    def set_result(self, sid, pct):
        self.j._run("UPDATE surprises SET result_ts=?, result_pct=? WHERE id=?", (int(time.time()), pct, sid))

    def pending_results(self, after_hours=24 * 14):
        """Расхождения без результата старше двух недель — для записи результата (ход инструмента)."""
        return self.j._run("SELECT * FROM surprises WHERE result_ts IS NULL AND ts < ?", (int(time.time() - after_hours * 3600),), "all")

    # ---------- событие от модели: отчёт / дивиденд ----------
    def on_event(self, ev, title, news_id, ts, source=""):
        """Расхождение из разобранной новости. Возвращает dict {kind, base, surprise, size, sid} или None.
        Отчёт: расхождение берём из формулировки ленты («лучше/хуже ожиданий»), число не нужно.
        Дивиденд: рекомендация (сумма из текста) против прогнозов из лент за 45 дней."""
        text = title or ""
        base = company_of(text, ev.get("affected_symbols"))
        if not base:
            return None
        head = text[:400]
        # отчёт с числом: факт чистой прибыли против консенсуса из лент за 30 дней → размер расхождения в типичных отклонениях (15%)
        mf = NP_FACT.search(head) if "прибыл" in head.lower() and NP_PERIOD.search(head) else None
        if mf:
            exp, n, age = self.expectation("np", base, ts, days=30)
            if exp:
                fact = bignum(mf.group(2), mf.group(3))
                if fact > 0 and 0.2 <= fact / exp <= 5:
                    surprise = (fact - exp) / exp
                    sid = self.record("np", base, exp, fact, surprise, n, age, news_id, title, "прибыль против консенсуса")
                    return {"kind": "np", "base": base, "surprise": surprise, "size": self.size_of("np", surprise), "sid": sid, "strong": n >= 1}
        if REPORT.search(head) and (BEAT.search(head) or MISS.search(head) or INLINE.search(head)):
            sign = 1 if BEAT.search(head) else (-1 if MISS.search(head) else 0)
            sid = self.record("earn", base, None, None, float(sign), n_sources=1, news_id=news_id, title=title,
                              note="по формулировке ленты" + ("" if source != "disclosure" else " (раскрытие)"))
            return {"kind": "earn", "base": base, "surprise": float(sign), "size": abs(sign) * 1.0, "sid": sid, "strong": True}
        if "дивиденд" in text.lower() and (ev.get("event_type") == "earnings" or source == "disclosure"):
            if DIV_NONE.search(head):
                exp, n, age = self.expectation("div", base, ts, days=45)
                if exp:
                    sid = self.record("div", base, exp, 0.0, -1.0, n, age, news_id, title, "отмена при ожидавшемся дивиденде")
                    return {"kind": "div", "base": base, "surprise": -1.0, "size": self.size_of("div", -1.0), "sid": sid, "strong": n >= 2}
                return None
            m = DIV_REC.search(head) or DIV_REC2.search(head)
            if not m:
                return None
            actual = num(m.group(2))
            exp, n, age = self.expectation("div", base, ts, days=45)
            if not exp or actual <= 0 or not (0.2 <= actual / exp <= 5):
                return None
            surprise = (actual - exp) / exp
            sid = self.record("div", base, exp, actual, surprise, n, age, news_id, title)
            return {"kind": "div", "base": base, "surprise": surprise, "size": self.size_of("div", surprise), "sid": sid, "strong": n >= 2}
        return None

    # ---------- отчёт владельцу ----------
    def text(self, days=14):
        now = time.time()
        lines = []
        v, src, strong = self.cbr_consensus(now)
        lines.append(f"Ставка ЦБ: консенсус {v:g}% ({src}{', сильный' if strong else ', слабый'})" if v is not None else "Ставка ЦБ: консенсуса нет")
        cpi, _, age = self.expectation("cpi", "CPI", now, days=10)
        if cpi is not None:
            lines.append(f"Недельная инфляция: прошлая неделя {cpi:+.2f}% ({age / 24:.0f} дн. назад) — это и есть ожидание на среду")
        rows = self.j._run("SELECT base, value, source, ts FROM expect_items WHERE kind='div' AND ts>? ORDER BY ts DESC LIMIT 12",
                           (int(now - 45 * 86400),), "all")
        if rows:
            seen = set()
            parts = []
            for r in rows:
                if r["base"] in seen:
                    continue
                seen.add(r["base"])
                parts.append(f"{r['base']} ~{r['value']:g} ₽ ({r['source']})")
            lines.append("Дивиденды, прогнозы из лент: " + ", ".join(parts[:8]))
        sur = self.j._run("SELECT * FROM surprises WHERE ts>? ORDER BY ts DESC LIMIT 12", (int(now - days * 86400),), "all")
        if sur:
            lines.append("")
            lines.append(f"Расхождения за {days} дн.:")
            for s in sur:
                d = datetime.fromtimestamp(s["ts"], MSK).strftime("%d.%m")
                if s["kind"] == "earn":
                    what = {1.0: "отчёт лучше ожиданий", -1.0: "отчёт хуже ожиданий", 0.0: "отчёт как ждали"}.get(float(s["surprise"] or 0), "отчёт")
                elif s["kind"] == "cbr":
                    what = f"ставка: ждали {s['expected']:g}, факт {s['actual']:g} ({s['surprise']:+.2f} п.п.)"
                elif s["kind"] == "np":
                    what = f"прибыль: консенсус {s['expected']:.0f} млрд, факт {s['actual']:.0f} ({s['surprise'] * 100:+.0f}%)"
                else:
                    what = f"{s['kind']}: ждали {s['expected']}, факт {s['actual']}"
                res = f", результат {s['result_pct']:+.1f}%" if s["result_pct"] is not None else ""
                lines.append(f"{d} {s['base']}: {what}; размер {s['size'] or 0:.1f}; {s['decision'] or '—'}{res}")
        else:
            lines.append("")
            lines.append(f"Расхождений за {days} дн. не было")
        return lines
