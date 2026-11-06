"""Календарь ожидаемых событий по компаниям и сделки «на ожидании» в теневом режиме.

Зачем. 06.11.2026 X5 объявила дивиденды 204 руб. — ровно как ждал рынок, и бумага упала на 3% за три минуты. До этого
за две недели она выросла на 9% (рынок на 3%): 22.10 компания назначила собрание по дивидендам, 29.10 вышел сильный
отчёт. Бот этих дат не знал. Психология простая: покупают на ожидании, продают на факте.

Что делает модуль:
1. Собирает будущие даты по каждой бумаге: из текста новостей и раскрытий по правилам (без модели), из ответа модели
   (поля future_event_date / future_event_kind) и из дивидендного календаря брокера (отсечки).
2. Отдаёт календарь аналитику в контекст и в команду /calendar.
3. Ведёт сделки «на ожидании» в ТЕНЯХ (без денег): событие назначено, до него 3–20 дней, бумага сильнее рынка —
   записывается вход по текущей цене; выход за торговый день до события или по стопу 2 ATR(день).
   На истории это правило проверить нечем (дат рекомендаций в архиве нет), поэтому сначала статистика, потом решение.
"""
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger("corpcal")
MSK = timezone(timedelta(hours=3))
MONTHS = {m: i + 1 for i, m in enumerate("января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split())}
D_TEXT = re.compile(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")(?:\s+(20\d\d))?", re.I)
D_NUM = re.compile(r"\b(\d{1,2})\.(\d{2})\.(20\d\d)\b")
# слова, которые делают дату событием; само слово «дивиденд» рядом с датой не годится (это может быть дата выплаты или любая другая)
KEY = re.compile(r"ВОСА|ГОСА|собрани\w+ акционеров|совет\w* директоров|наблюдательн\w+ совет|набсовет|\bСД\b|заседани|рассмотр|"
                 r"отч[её]т|МСФО|РСБУ|опублику\w+ (финансов|операцион)|день инвестора|отсечк|последний день покупки|закрыти\w+ реестра", re.I)
K_CUTOFF = re.compile(r"последний день покупки|отсечк|закрыти\w+ реестра|дата фиксации", re.I)
K_DIV = re.compile(r"дивиденд", re.I)
K_REPORT = re.compile(r"отч[её]т|МСФО|РСБУ|результат", re.I)
K_MEET = re.compile(r"ВОСА|ГОСА|собрани", re.I)
KIND_RU = {"dividend": "решение по дивидендам", "report": "отчёт", "meeting": "собрание акционеров", "board": "совет директоров",
           "cutoff": "дивидендная отсечка", "other": "событие",
           # события всего рынка и секторов
           "geo": "переговоры и геополитика", "sanctions": "санкции и ультиматумы", "cb": "Банк России", "macro": "статистика",
           "fed": "ФРС", "opec": "ОПЕК+", "speech": "выступление", "expiry": "экспирация", "index": "пересмотр индекса",
           "sector": "решение по отрасли", "offering": "размещение акций", "buyback": "оферта или выкуп", "court": "суд", "lockup": "конец запрета продаж"}
# Чего рынок ждёт от события, если модель не сказала иначе: hope — ждут хорошего (перед событием покупают, шорт против течения),
# fear — ждут плохого (зеркально), unknown — исход неизвестен (вход прямо перед событием — лотерея)
EXPECT_DEFAULT = {"geo": "hope", "dividend": "hope", "buyback": "hope", "sanctions": "fear", "offering": "fear", "lockup": "fear"}
MARKET = "MARKET"
# (вид, ожидание, шаблон). Ожидание «надежда» — только у мирных переговоров и перемирия: саммит ЕС или встреча лидеров сами по себе
# шортам не мешают. Санкции — только пакет, введение, вступление в силу, ультиматум и дедлайн (слово «санкции» есть в каждой второй новости).
# Разговор О переговорах — не переговоры: саммит, где «обсудят, кто будет представлять ЕС на переговорах» (13.11.2026), исход неизвестен.
# Это правило стоит первым, иначе слово «переговоры» рядом с датой давало «ждут хорошего» и закрывало шорты на лишний день.
M_UNSURE = re.compile(r"саммит|обсуд\w+[^.]{0,80}переговор|(вопрос|тем\w+|повестк\w+)\s[^.]{0,30}переговор", re.I)
M_RULES = [("geo", "unknown", M_UNSURE),
           ("geo", "hope", re.compile(r"переговор|перемири|мирн\w+ (план|соглашен|урегулирован|договор|сделк)|прекращени\w+ огня", re.I)),
           ("sanctions", "fear", re.compile(r"пакет\w* санкц|санкци\w* (вступ|введ|примут|принят|утверд)|(введ|вступ|примут|принят|утверд)\w* [^.]{0,30}санкци|ультиматум|дедлайн", re.I)),
           ("geo", "unknown", re.compile(r"саммит|встреч\w+ (лидер|президент|делегац|глав)|проведут встречу", re.I)),
           ("cb", "unknown", re.compile(r"(заседани\w+|решени\w+)[^.]{0,40}(Банка России|ЦБ РФ|\bЦБ\b)|по ключевой ставке", re.I)),
           ("fed", "unknown", re.compile(r"ФРС|FOMC", re.I)),
           ("opec", "unknown", re.compile(r"ОПЕК", re.I)),
           ("speech", "unknown", re.compile(r"послани\w+ (президент|Путин|Федеральному)|прямая линия|ПМЭФ|Валдай|ВЭФ", re.I)),
           ("index", "unknown", re.compile(r"(баз\w+ расч[её]та|пересмотр\w*|ребалансир\w+)[^.]{0,40}индекс", re.I))]


TRADE_KINDS = ("dividend", "report", "meeting", "board")     # перед отсечкой ход изучен отдельно (research/moex_dividend_events.py)


def _future_date(d, m, y, today, max_days=90):
    try:
        cand = date(int(y), m, d) if y else date(today.year, m, d)
        if not y and cand < today:
            cand = date(today.year + 1, m, d)
    except ValueError:
        return None
    return cand if 1 <= (cand - today).days <= max_days else None


KINDS = [("cutoff", re.compile(r"последний день покупки|отсечк", re.I)),
         ("record", re.compile(r"закрыти\w+ реестра|дата фиксации|выплат", re.I)),      # дата реестра и выплаты — не событие для цены
         ("report", re.compile(r"отч[её]т|МСФО|РСБУ|опублику", re.I)),
         ("meeting", re.compile(r"ВОСА|ГОСА|собрани", re.I)),
         ("board", re.compile(r"совет\w* директоров|наблюдательн\w+ совет|набсовет|\bСД\b|заседани|рассмотр|день инвестора", re.I))]


def _kind_at(text, a, b):
    """Вид события для даты на позициях a..b: по ближайшему ключевому слову. Слово вплотную ПЕРЕД датой («отсечка - 15 декабря»,
    «Последний день покупки: 10.12.2026») важнее слова после неё («30 ноября - X5 - ВОСА»)."""
    best = None
    lo, hi = max(0, a - 60), min(len(text), b + 110)
    for kind, rx in KINDS:
        for m in rx.finditer(text, lo, hi):
            if m.end() <= a:
                dist = a - m.end()
                score = dist if dist <= 15 else dist + 40
            elif m.start() >= b:
                score = m.start() - b + 20
            else:
                continue
            if best is None or score < best[0]:
                best = (score, kind)
    return best[1] if best else None


def extract(text, bases, today=None, base=None):
    """Будущие события из текста по правилам: [(тикер, дата, вид)]. Только даты в будущем (от завтра до 90 дней) и только
    рядом со словами про собрание, совет директоров, отчёт или отсечку. Тикер — из хэштега (#X5); без хэштега событий не
    берём: в статьях и утренних подборках латинские слова и чужие даты дают мусор. Больше двух тикеров — подборка, не берём.
    base — тикер известен заранее (раскрытие эмитента)."""
    text = (text or "")[:1500 if base else 700]
    today = today or datetime.now(MSK).date()
    if base:
        syms = [base]
    else:
        syms = list(dict.fromkeys(t for t in re.findall(r"#([A-Z][A-Z0-9]{0,5})\b", text) if t in bases))
    if not syms or len(syms) > 2:
        return []
    has_div = bool(K_DIV.search(text[:700]))
    found = []
    for m in list(D_TEXT.finditer(text)) + list(D_NUM.finditer(text)):
        if m.re is D_TEXT:
            dt = _future_date(int(m.group(1)), MONTHS[m.group(2).lower()], m.group(3), today)
        else:
            dt = _future_date(int(m.group(1)), int(m.group(2)), m.group(3), today) if 1 <= int(m.group(2)) <= 12 else None
        if not dt:
            continue
        k = _kind_at(text, m.start(), m.end())
        if k in (None, "record"):
            continue
        if k in ("meeting", "board") and has_div:
            k = "dividend"       # собрание или совет директоров в сообщении про дивиденды = решение по дивидендам
        for sym in syms:
            if (sym, dt, k) not in found:
                found.append((sym, dt, k))
    return found


def extract_market(text, today=None):
    """События всего рынка из текста по правилам: [(дата, вид, ожидание)]. Дата в будущем (до 45 дней) и рядом с ней слово про переговоры,
    санкции, заседание ЦБ или ФРС, ОПЕК, большое выступление, пересмотр индекса. Только короткие сообщения: в длинных статьях
    и подборках даты относятся к чему угодно."""
    text = (text or "")[:500]
    today = today or datetime.now(MSK).date()
    found = []
    for m in list(D_TEXT.finditer(text)) + list(D_NUM.finditer(text)):
        if m.re is D_TEXT:
            dt = _future_date(int(m.group(1)), MONTHS[m.group(2).lower()], m.group(3), today, 45)
        else:
            dt = _future_date(int(m.group(1)), int(m.group(2)), m.group(3), today, 45) if 1 <= int(m.group(2)) <= 12 else None
        if not dt:
            continue
        ctx = text[max(0, m.start() - 110):m.end() + 110]
        for kind, expect, rx in M_RULES:
            if rx.search(ctx):
                if not any(f[0] == dt and f[1] == kind for f in found):
                    found.append((dt, kind, expect))
                break
    return found


def trading_days_until(d, today=None):
    """Сколько торговых дней от сегодня до даты: 0 — сегодня, 1 — следующий торговый день; прошедшая дата — −1."""
    today = today or datetime.now(MSK).date()
    if d < today:
        return -1
    return sum(1 for i in range(1, (d - today).days + 1) if (today + timedelta(days=i)).weekday() < 5)


class CorpCalendar:
    def __init__(self, settings, journal, exchange):
        self.s, self.j, self.ex = settings, journal, exchange
        self.j._run("""CREATE TABLE IF NOT EXISTS corp_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, base TEXT, date TEXT, kind TEXT, title TEXT, source TEXT, created_ts INTEGER,
            UNIQUE(base, date, kind))""")
        self.j._run("""CREATE TABLE IF NOT EXISTS pre_event (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER UNIQUE, base TEXT, event_date TEXT, kind TEXT,
            entry_ts INTEGER, entry REAL, stop REAL, idx_entry REAL, exit_ts INTEGER, exit REAL, idx_exit REAL,
            pct REAL, exc_pct REAL, status TEXT, why TEXT)""")
        for col in ("expect TEXT DEFAULT ''", "hhmm TEXT DEFAULT ''"):
            try:
                self.j._run(f"ALTER TABLE corp_events ADD COLUMN {col}")
            except Exception:  # noqa: BLE001
                pass
        self._bases, self._bases_ts = set(), 0.0
        self._tick_ts = 0.0

    def cfg(self):
        return self.s.get("corpcal") or {}

    def bases(self):
        if not self._bases or time.time() - self._bases_ts > 6 * 3600:
            try:
                self._bases = {i["base"] for i in self.ex.instruments().values() if i.get("kind") == "share"}
                self._bases_ts = time.time()
            except Exception as e:  # noqa: BLE001
                log.debug("календарь: инструменты: %s", e)
        return self._bases

    # ---------- сбор ----------
    def add(self, base, dt, kind, title="", source="", expect="", hhmm=""):
        """True, если событие новое. dt — date или строка ГГГГ-ММ-ДД. base — тикер, MARKET (весь рынок) или SECTOR:<название>."""
        ds = dt.isoformat() if isinstance(dt, date) else str(dt)[:10]
        try:
            d = datetime.strptime(ds, "%Y-%m-%d").date()
        except ValueError:
            return False
        today = datetime.now(MSK).date()
        scope_ok = base == MARKET or base in self.bases() or (base.startswith("SECTOR:") and base[7:] in (self.s.get("risk.sectors") or {}))
        if not scope_ok or not (0 <= (d - today).days <= 120):
            return False
        kind = kind if kind in KIND_RU else "other"
        expect = expect if expect in ("hope", "fear", "unknown") else EXPECT_DEFAULT.get(kind, "unknown")
        if self.j._run("SELECT 1 FROM corp_events WHERE base=? AND date=? AND kind=?", (base, ds, kind), "one"):
            return False
        self.j._run("INSERT OR IGNORE INTO corp_events(base,date,kind,title,source,created_ts,expect,hhmm) VALUES(?,?,?,?,?,?,?,?)",
                    (base, ds, kind, (title or "")[:200], source, int(time.time()), expect, hhmm or ""))
        log.info("календарь: %s %s — %s, ожидание %s (%s)", base, ds, KIND_RU[kind], expect, source)
        return True

    def ingest(self, item):
        """Новость или раскрытие из потока: даты по правилам, до фильтров и без модели. Возвращает число новых событий."""
        text = (item.get("title") or "") + " " + (item.get("summary") or "")[:1500]
        n = 0
        for base, dt, kind in extract(text, self.bases(), base=item.get("base")):
            n += self.add(base, dt, kind, item.get("title") or "", item.get("source") or "")
        if not item.get("base") and (item.get("source") or "").startswith("tg:"):
            # события всего рынка: только из телеграм-каналов (короткие сообщения), не из статей
            for dt, kind, expect in extract_market(text):
                n += self.add(MARKET, dt, kind, item.get("title") or "", item.get("source") or "", expect)
        return n

    def ingest_model(self, ev, title, source):
        """Дата будущего события из ответа модели (поля future_event_date / future_event_kind)."""
        ds, kind = (ev.get("future_event_date") or "").strip(), (ev.get("future_event_kind") or "").strip().lower()
        if not re.fullmatch(r"20\d\d-\d\d-\d\d", ds):
            return 0
        kind = {"dividends": "dividend", "earnings": "report", "agm": "meeting", "egm": "meeting", "talks": "geo", "geopolitics": "geo",
                "rate": "cb", "cbr": "cb", "fomc": "fed"}.get(kind, kind)
        expect = (ev.get("future_event_expect") or "").strip().lower()
        if kind == "geo" and M_UNSURE.search(title or ""):
            expect = "unknown"      # саммит или обсуждение переговоров: «ждут хорошего» не ставим, даже если так сказала модель
        scope = (ev.get("future_event_scope") or "").strip().lower()
        syms = [x for x in (ev.get("affected_symbols") or []) if x in self.bases()][:2]
        if scope == "market" or (ev.get("market_wide") and scope != "company") or (not syms and kind in ("geo", "sanctions", "cb", "fed", "opec", "macro", "speech", "index")):
            if kind not in ("geo", "sanctions", "cb", "fed", "opec", "macro", "speech", "index", "expiry"):
                return 0      # событие компании без тикера (собрание, отчёт) к рынку в целом не относится
            return int(self.add(MARKET, ds, kind, title, f"модель/{source}", expect))
        if scope == "sector":
            sectors = self.s.get("risk.sectors") or {}
            secs = {k for k, v in sectors.items() for x in (ev.get("affected_symbols") or []) if x in v}
            return sum(self.add("SECTOR:" + k, ds, kind if kind in KIND_RU else "sector", title, f"модель/{source}", expect) for k in list(secs)[:2])
        return sum(self.add(b, ds, kind, title, f"модель/{source}", expect) for b in syms)

    def sync_cutoffs(self, divcal):
        for base, rec in (getattr(divcal, "next", None) or {}).items():
            if rec.get("last_buy"):
                self.add(base, rec["last_buy"], "cutoff", f"последний день покупки под дивиденд {rec.get('div', 0):g} руб. ({rec.get('yield', 0)}%)", "брокер")

    def sync_schedule(self, cal=None):
        """Регулярные события без новостей: недельная инфляция (среда, 19:00 МСК), экспирация индексных фьючерсов,
        релизы высокой важности из макро-календаря (ЦБ, ФРС, статистика США)."""
        today = datetime.now(MSK).date()
        wed = today + timedelta(days=(2 - today.weekday()) % 7)
        self.add(MARKET, wed, "macro", "Недельная инфляция (Росстат)", "расписание", "unknown", "19:00")
        try:
            exp = sorted({i["expiration"] for i in self.ex.instruments().values() if i.get("kind") == "future" and i.get("expiration")
                          and i.get("base") in ("IMOEX", "RTS")})
            for e in exp[:2]:
                self.add(MARKET, e, "expiry", "Экспирация фьючерсов на индексы", "брокер", "unknown")
        except Exception as e:  # noqa: BLE001
            log.debug("календарь: экспирации: %s", e)
        for e in (getattr(cal, "events", None) or []):
            try:
                if e.get("impact") != "high" or not (0 <= e["ts"] - time.time() <= 14 * 86400):
                    continue
                dt = datetime.fromtimestamp(e["ts"], MSK)
                src = (e.get("source") or "").lower()
                ttl = e.get("title", "")
                kind = "fed" if e.get("kind") == "fomc" or "fed" in src or "FOMC" in ttl else ("cb" if "ЦБ" in ttl or "Банк" in ttl and "Росси" in ttl else "macro")
                self.add(MARKET, dt.date(), kind, e.get("title", ""), "макро-календарь", "unknown", dt.strftime("%H:%M"))
            except Exception:  # noqa: BLE001
                continue

    # ---------- поведение перед событием ----------
    def _scope_events(self, base, sector, days):
        keys = {MARKET, base}
        if sector:
            keys.add("SECTOR:" + sector)
        return [r for r in self.upcoming(days + 4) if r["base"] in keys]

    def entry_block(self, base, sector, direction, now=None):
        """Причина запрета входа из-за события в календаре или пусто.
        Ждут хорошего: за pre_days торговых дней новые шорты закрыты (06.11 бот шортил банк и индекс за два дня до мирных
        переговоров, о которых знал неделю); в день события по компании или сектору — новых лонгов нет (на факте продают).
        Ждут плохого — зеркально. Исход неизвестен и время известно: за 30 минут до и 15 после входов нет."""
        c = self.cfg()
        pre = int(c.get("pre_days", 3))
        now_msk = datetime.now(MSK) if now is None else datetime.fromtimestamp(now, MSK)
        for ev in self._scope_events(base, sector, pre):
            d = trading_days_until(datetime.strptime(ev["date"], "%Y-%m-%d").date(), now_msk.date())
            if d < 0 or d > pre:
                continue
            exp, name = ev.get("expect") or "unknown", f"{KIND_RU.get(ev['kind'], ev['kind'])} {ev['date'][8:]}.{ev['date'][5:7]}"
            against = "short" if exp == "hope" else ("long" if exp == "fear" else None)
            if against and direction == against:
                return f"впереди событие, которого рынок ждёт с {'надеждой' if exp == 'hope' else 'опаской'} ({name}): {'шорт' if against == 'short' else 'лонг'} против течения"
            if against and d == 0 and ev["base"] != MARKET and direction != against:
                return f"день события ({name}): на ожидавшемся факте {'покупать поздно' if exp == 'hope' else 'продавать поздно'}"
            if exp == "unknown" and d == 0 and ev.get("hhmm"):
                try:
                    hh, mm = map(int, ev["hhmm"].split(":"))
                    t_ev = now_msk.replace(hour=hh, minute=mm, second=0, microsecond=0)
                    delta = (now_msk - t_ev).total_seconds() / 60
                    if -float(c.get("unknown_before_min", 30)) <= delta <= float(c.get("unknown_after_min", 15)):
                        return f"окно события с неизвестным исходом ({name} в {ev['hhmm']})"
                except ValueError:
                    pass
        return ""

    def side_cap(self, direction):
        """Сколько позиций в эту сторону можно держать, пока впереди событие с ожиданием против неё (любой области): несколько
        шортов перед событием, которого ждут с надеждой, — одна большая ставка против рынка. None — без ограничения."""
        c = self.cfg()
        pre = int(c.get("pre_days", 3))
        want = "hope" if direction == "short" else "fear"
        today = datetime.now(MSK).date()
        for ev in self.upcoming(pre + 4):
            if (ev.get("expect") or "") == want and 0 <= trading_days_until(datetime.strptime(ev["date"], "%Y-%m-%d").date(), today) <= pre:
                return int(c.get("side_cap", 2))
        return None

    def tighten_for(self, base, sector, side):
        """Событие, из-за которого стоп по открытой позиции надо подтянуть: до него не больше одного торгового дня,
        а ожидание рынка против позиции. side — Buy/Sell."""
        want = "hope" if side == "Sell" else "fear"
        today = datetime.now(MSK).date()
        for ev in self._scope_events(base, sector, 1):
            if (ev.get("expect") or "") == want and 0 <= trading_days_until(datetime.strptime(ev["date"], "%Y-%m-%d").date(), today) <= 1:
                return ev
        return None

    # ---------- чтение ----------
    def upcoming(self, days=21, base=None):
        today = datetime.now(MSK).date()
        rows = self.j._run("SELECT * FROM corp_events WHERE date>=? AND date<=? ORDER BY date, base",
                           (today.isoformat(), (today + timedelta(days=days)).isoformat()), "all")
        return [r for r in rows if base is None or r["base"] == base]

    def soon(self, base, within_days=1, kinds=TRADE_KINDS):
        """Ближайшее событие по компании в пределах within_days календарных дней (сегодня = 0) или None."""
        for r in self.upcoming(within_days, base):
            if r["kind"] in kinds:
                return r
        return None

    def text(self, days=14, limit=25):
        out = []
        exp_ru = {"hope": "ждут хорошего", "fear": "ждут плохого"}
        for r in self.upcoming(days)[:limit]:
            d = datetime.strptime(r["date"], "%Y-%m-%d")
            who = "РЫНОК" if r["base"] == MARKET else (r["base"][7:] if r["base"].startswith("SECTOR:") else r["base"])
            tail = (f" в {r['hhmm']}" if r.get("hhmm") else "") + (f" — {exp_ru[r['expect']]}" if r.get("expect") in exp_ru else "")
            out.append(f"{d.strftime('%d.%m')} {who}: {KIND_RU.get(r['kind'], r['kind'])}{tail}")
        return out

    def context_text(self):
        t = self.text(14, 20)
        return ("Календарь событий по компаниям (ближайшие две недели; перед событием бумагу часто покупают заранее, "
                "а на факте, совпавшем с ожиданиями, продают):\n" + "\n".join(t)) if t else ""

    # ---------- сделки «на ожидании» в тенях ----------
    def _daily(self, sym, n=40):
        d = self.ex.klines(sym, "D", n)
        return d if d is not None and len(d) >= 22 else None

    @staticmethod
    def _prev_trading_day(d):
        p = d - timedelta(days=1)
        while p.weekday() >= 5:
            p -= timedelta(days=1)
        return p

    def tick(self, divcal=None, index="IMOEXF", cal=None):
        """Раз в полчаса: отсечки из календаря брокера, новые тени «на ожидании», закрытие созревших."""
        if time.time() - self._tick_ts < 1800:
            return
        self._tick_ts = time.time()
        c = self.cfg()
        if divcal is not None:
            try:
                self.sync_cutoffs(divcal)
            except Exception as e:  # noqa: BLE001
                log.debug("календарь: отсечки: %s", e)
        try:
            self.sync_schedule(cal)
        except Exception as e:  # noqa: BLE001
            log.debug("календарь: расписание: %s", e)
        if not c.get("pre_event_shadow", True):
            return
        today = datetime.now(MSK).date()
        try:
            idx_px = float(self.ex.last_price(index) or 0)
        except Exception:  # noqa: BLE001
            idx_px = 0.0
        # 1) новые тени
        lo, hi = int(c.get("pre_event_min_days", 3)), int(c.get("pre_event_max_days", 20))
        have = {r["event_id"] for r in self.j._run("SELECT event_id FROM pre_event", (), "all")}
        opened_bases = {r["base"] for r in self.j._run("SELECT base FROM pre_event WHERE status='open'", (), "all")}
        # только бумаги, которыми бот реально торгует: не из стоп-листа и с оборотом не ниже порога списка
        # (иначе статистика теней наберётся на неликвиде вроде «Базиса», куда бот всё равно не войдёт)
        ucfg = self.s.get("universe") or {}
        black = set(ucfg.get("blacklist") or [])
        min_t = float(ucfg.get("min_turnover_24h_usd") or 0)
        turn = getattr(self.ex, "_turn_cache", None) or {}
        for ev in self.upcoming(hi):
            days_left = (datetime.strptime(ev["date"], "%Y-%m-%d").date() - today).days
            if ev["id"] in have or ev["kind"] not in TRADE_KINDS or days_left < lo or ev["base"] in opened_bases or ev["base"] not in self.bases():
                continue
            if ev["base"] in black or (min_t and float(turn.get(ev["base"]) or 0) < min_t):
                continue      # оборот неизвестен (срез ещё не посчитан) или мал — не берём
            if ev["base"] in (self.s.get("risk.base_alias") or {}):
                continue      # привилегированная акция — та же компания: тень ведём по обыкновенной
            try:
                d = self._daily(ev["base"])
                di = self._daily(index)
                if d is None or di is None:
                    continue
                cl, ci = d["close"].astype(float), di["close"].astype(float)
                px = float(self.ex.last_price(ev["base"]) or cl.iloc[-1])
                strong = (cl.iloc[-1] / cl.iloc[-6] - 1) - (ci.iloc[-1] / ci.iloc[-6] - 1) >= 0 and px > cl.iloc[-21:-1].mean()
                if not strong:
                    continue      # бумага не сильнее рынка: проверим в следующий раз, пока до события не меньше lo дней
                tr = (d["high"].astype(float) - d["low"].astype(float)).iloc[-14:].mean()
                stop = px - 2.0 * float(tr)
                self.j._run("INSERT OR IGNORE INTO pre_event(event_id,base,event_date,kind,entry_ts,entry,stop,idx_entry,status) VALUES(?,?,?,?,?,?,?,?, 'open')",
                            (ev["id"], ev["base"], ev["date"], ev["kind"], int(time.time()), px, stop, idx_px))
                opened_bases.add(ev["base"])
                log.info("тень «на ожидании»: %s вход %g, стоп %g, событие %s (%s) через %d дн.", ev["base"], px, stop, ev["date"], KIND_RU[ev["kind"]], days_left)
            except Exception as e:  # noqa: BLE001
                log.debug("тень «на ожидании» %s: %s", ev["base"], e)
        # 2) закрытие: стоп или вечер торгового дня перед событием
        now_msk = datetime.now(MSK)
        for r in self.j._run("SELECT * FROM pre_event WHERE status='open'", (), "all"):
            try:
                px = float(self.ex.last_price(r["base"]) or 0)
                if px <= 0:
                    continue
                ev_date = datetime.strptime(r["event_date"], "%Y-%m-%d").date()
                exit_day = self._prev_trading_day(ev_date)
                why = None
                if px <= r["stop"]:
                    px, why = r["stop"], "стоп"
                elif today > exit_day or (today == exit_day and now_msk.hour >= 18):
                    why = "выход за день до события"
                if not why:
                    continue
                pct = (px / r["entry"] - 1) * 100
                exc = pct - ((idx_px / r["idx_entry"] - 1) * 100 if idx_px and r["idx_entry"] else 0.0)
                self.j._run("UPDATE pre_event SET status='closed', exit_ts=?, exit=?, idx_exit=?, pct=?, exc_pct=?, why=? WHERE id=?",
                            (int(time.time()), px, idx_px, pct, exc, why, r["id"]))
                log.info("тень «на ожидании» закрыта: %s %+.2f%% (сверх рынка %+.2f%%), %s", r["base"], pct, exc, why)
            except Exception as e:  # noqa: BLE001
                log.debug("закрытие тени «на ожидании» %s: %s", r["base"], e)

    def shadow_text(self):
        rows = self.j._run("SELECT * FROM pre_event ORDER BY entry_ts DESC", (), "all")
        if not rows:
            return ["пока нет: ждём событий, назначенных за 3–20 дней"]
        closed = [r for r in rows if r["status"] == "closed"]
        out = []
        if closed:
            p = [r["pct"] for r in closed]
            x = [r["exc_pct"] for r in closed]
            out.append(f"закрыто {len(closed)}: в среднем {sum(p) / len(p):+.2f}%, сверх рынка {sum(x) / len(x):+.2f}%, в плюс {sum(1 for v in p if v > 0) / len(p) * 100:.0f}%")
        for r in rows[:8]:
            d = datetime.strptime(r["event_date"], "%Y-%m-%d").strftime("%d.%m")
            if r["status"] == "open":
                out.append(f"• {r['base']} до {d} ({KIND_RU.get(r['kind'], r['kind'])}): вход {r['entry']:g}, стоп {r['stop']:g}")
            else:
                out.append(f"• {r['base']} до {d}: {r['pct']:+.2f}% (сверх рынка {r['exc_pct']:+.2f}%), {r['why']}")
        return out
