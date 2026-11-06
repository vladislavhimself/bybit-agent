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
           "cutoff": "дивидендная отсечка", "other": "событие"}
TRADE_KINDS = ("dividend", "report", "meeting", "board")     # перед отсечкой ход изучен отдельно (research/moex_dividend_events.py)


def _future_date(d, m, y, today, max_days=90):
    try:
        cand = date(int(y), m, d) if y else date(today.year, m, d)
        if not y and cand < today:
            cand = date(today.year + 1, m, d)
    except ValueError:
        return None
    return cand if 1 <= (cand - today).days <= max_days else None


def _kind(ctx, whole):
    if K_CUTOFF.search(ctx):
        return "cutoff"
    if K_DIV.search(ctx) or (K_DIV.search(whole) and (K_MEET.search(ctx) or re.search(r"совет|СД|набсовет", ctx, re.I))):
        return "dividend"
    if K_REPORT.search(ctx):
        return "report"
    if K_MEET.search(ctx):
        return "meeting"
    return "board"


def extract(text, bases, today=None, base=None):
    """Будущие события из текста по правилам: [(тикер, дата, вид)]. Только даты в будущем (от завтра до 90 дней) и только
    рядом со словами про собрание, совет директоров, дивиденды, отчёт или отсечку. Тикер — из хэштега или латинского
    тикера в тексте; если тикеров больше двух (подборка, дайджест) — ничего не берём. base — тикер известен заранее (раскрытие)."""
    text = text or ""
    today = today or datetime.now(MSK).date()
    if base:
        syms = [base]
    else:
        tags = [t for t in re.findall(r"#([A-Z][A-Z0-9]{0,5})\b", text) if t in bases]
        syms = list(dict.fromkeys(tags)) or list(dict.fromkeys(t for t in re.findall(r"\b([A-Z][A-Z0-9]{1,5})\b", text) if t in bases))
    if not syms or len(syms) > 2:
        return []
    found = []
    for m in list(D_TEXT.finditer(text)) + list(D_NUM.finditer(text)):
        if m.re is D_TEXT:
            dt = _future_date(int(m.group(1)), MONTHS[m.group(2).lower()], m.group(3), today)
        else:
            dt = _future_date(int(m.group(1)), int(m.group(2)), m.group(3), today) if 1 <= int(m.group(2)) <= 12 else None
        if not dt:
            continue
        ctx = text[max(0, m.start() - 110):m.end() + 110]
        if not KEY.search(ctx):
            continue
        k = _kind(ctx, text[:600])
        for s in syms:
            if (s, dt, k) not in found:
                found.append((s, dt, k))
    return found


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
    def add(self, base, dt, kind, title="", source=""):
        """True, если событие новое. dt — date или строка ГГГГ-ММ-ДД."""
        ds = dt.isoformat() if isinstance(dt, date) else str(dt)[:10]
        try:
            d = datetime.strptime(ds, "%Y-%m-%d").date()
        except ValueError:
            return False
        today = datetime.now(MSK).date()
        if base not in self.bases() or not (0 <= (d - today).days <= 120):
            return False
        kind = kind if kind in KIND_RU else "other"
        if self.j._run("SELECT 1 FROM corp_events WHERE base=? AND date=? AND kind=?", (base, ds, kind), "one"):
            return False
        self.j._run("INSERT OR IGNORE INTO corp_events(base,date,kind,title,source,created_ts) VALUES(?,?,?,?,?,?)",
                    (base, ds, kind, (title or "")[:200], source, int(time.time())))
        log.info("календарь: %s %s — %s (%s)", base, ds, KIND_RU[kind], source)
        return True

    def ingest(self, item):
        """Новость или раскрытие из потока: даты по правилам, до фильтров и без модели. Возвращает число новых событий."""
        text = (item.get("title") or "") + " " + (item.get("summary") or "")[:1500]
        n = 0
        for base, dt, kind in extract(text, self.bases(), base=item.get("base")):
            n += self.add(base, dt, kind, item.get("title") or "", item.get("source") or "")
        return n

    def ingest_model(self, ev, title, source):
        """Дата будущего события из ответа модели (поля future_event_date / future_event_kind)."""
        ds, kind = (ev.get("future_event_date") or "").strip(), (ev.get("future_event_kind") or "").strip().lower()
        if not re.fullmatch(r"20\d\d-\d\d-\d\d", ds):
            return 0
        kind = {"dividends": "dividend", "earnings": "report", "agm": "meeting", "egm": "meeting"}.get(kind, kind)
        syms = [x for x in (ev.get("affected_symbols") or []) if x in self.bases()][:2]
        return sum(self.add(b, ds, kind, title, f"модель/{source}") for b in syms)

    def sync_cutoffs(self, divcal):
        for base, rec in (getattr(divcal, "next", None) or {}).items():
            if rec.get("last_buy"):
                self.add(base, rec["last_buy"], "cutoff", f"последний день покупки под дивиденд {rec.get('div', 0):g} руб. ({rec.get('yield', 0)}%)", "брокер")

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
        for r in self.upcoming(days)[:limit]:
            d = datetime.strptime(r["date"], "%Y-%m-%d")
            out.append(f"{d.strftime('%d.%m')} {r['base']}: {KIND_RU.get(r['kind'], r['kind'])}")
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

    def tick(self, divcal=None, index="IMOEXF"):
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
        for ev in self.upcoming(hi):
            days_left = (datetime.strptime(ev["date"], "%Y-%m-%d").date() - today).days
            if ev["id"] in have or ev["kind"] not in TRADE_KINDS or days_left < lo or ev["base"] in opened_bases:
                continue
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
