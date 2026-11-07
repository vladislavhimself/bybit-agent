"""Главная тема рынка и серии новостей.

Тема. 06.11.2026 бот шортил банк и индекс за два дня до мирных переговоров: по графику бумаги тренд был вниз, а рынок
жил ожиданием переговоров и рос. Технический сигнал видит одну бумагу; тема — то, что двигает весь рынок. Раз в час модель
по потоку новостей и календарю называет главную тему и сторону, в которую рынок на неё идёт. Технические сделки против
темы не открываются.

Серии. Вечером 06.11 за 23 минуты вышло два десятка сообщений «переговоры пройдут 10 ноября». Каждое модель оценила
на 5–6, и бот ничего не сделал, хотя вместе это главное событие дня. Пять и больше общерыночных новостей об одном
за полчаса превращаются в одно событие важности 8: срабатывает правило для важных общерыночных новостей (стоп по
встречной позиции подтягивается), а тема рынка пересчитывается сразу, не дожидаясь часа."""
import hashlib
import json
import logging
import re
import threading
import time
from typing import Literal

from pydantic import BaseModel

log = logging.getLogger("theme")

SYSTEM = """Ты определяешь главную тему, которая СЕЙЧАС двигает российский рынок акций (Мосбиржа), и сторону, в которую рынок на неё идёт.
Тебе дают новости за последние часы с оценками, ближайшие события календаря и ход индекса.
- theme: коротко, 2–6 слов (например: «ожидание мирных переговоров», «ужесточение риторики ЦБ», «обвал нефти», «новые санкции»).
- direction: bullish — рынок растёт на ожиданиях или фактах этой темы и встречные продажи выкупают; bearish — падает; none — явной
  темы нет, сигналы смешанные, или тема не двигает индекс. Смотри на то, как рынок РЕАГИРУЕТ (ход индекса), а не только на смысл новостей.
- confidence 0..1: насколько тема доминирует. Ниже 0.6 бот её не учитывает, поэтому не завышай.
- horizon_days: на сколько торговых дней тема ещё актуальна (до ожидаемого события или пока поток новостей не сменится), 1..5.
Не выдумывай: опирайся только на переданное. Отвечай строго по схеме."""

STOP = {"россии", "россия", "росси", "сообщи", "заявил", "заявил", "котор", "после", "может", "будет", "также", "около", "против", "между",
        "более", "новост", "главны", "главно", "сегодн", "рынок", "рынка", "рынке", "говори", "сказал", "назвал", "этого", "время", "тольк"}


class ThemeView(BaseModel):
    theme: str
    direction: Literal["bullish", "bearish", "none"]
    confidence: float
    horizon_days: int
    reasoning: str


def stems(title):
    """Основы слов заголовка (первые 6 букв слов от 5 букв) без общих слов — для сравнения «об одном ли новости»."""
    return {w[:6] for w in re.findall(r"[а-яёa-z]{5,}", (title or "").lower())} - STOP


def similar(a, b):
    # два общих значимых слова («путин» + «перего», «санкци» + «пакет»): заголовки об одном пишут по-разному, и требование
    # трёх общих слов пропускало половину серии 06.11
    return len(a & b) >= 2


# Слова-повороты: новость с ними — не пересказ, а новый поворот темы («переговоры сорваны» после «переговоры подтверждены»)
TURN = {"сорван", "сорвал", "срыв", "отмен", "отказ", "опрове", "провал", "перене", "отлож", "приост", "прерва"}


def dup_tokens(title):
    """Основы значимых слов плюс числа заголовка: одни и те же цифры — сильный признак той же новости."""
    return stems(title) | {"#" + m.replace(",", ".") for m in re.findall(r"\d+(?:[.,]\d+)?", title or "")}


def duplicate_of(recent, title, now, hours=3.0):
    """Повтор ли заголовок одной из недавно разобранных новостей: три и больше общих значимых слов, не меньше половины слов
    короткого заголовка, числа (если есть в обоих) совпадают, и в новом заголовке нет слова-поворота, которого не было в первом.
    recent — [(ts первой публикации, dup_tokens, оценка модели, id новости)], свежие в конце.
    07.11.2026: из 3 583 новостей за сутки 1 296 (36%) — пересказы того, что пришло за два часа до (ТАСС, РИА, РБК, Прайм)."""
    st = dup_tokens(title)
    words = {x for x in st if not x.startswith("#")}
    if len(words) < 3:
        return None
    nums = {x for x in st if x.startswith("#")}
    for ts, st0, ev, nid in reversed(recent):
        if now - ts > hours * 3600:
            continue
        words0 = {x for x in st0 if not x.startswith("#")}
        nums0 = {x for x in st0 if x.startswith("#")}
        common = len(words & words0)
        if common < 3 or common < 0.5 * min(len(words), len(words0)):
            continue
        if nums and nums0 and not (nums <= nums0 or nums0 <= nums):
            continue            # другие цифры — другая новость (или новые данные); пересказ может опустить часть чисел
        if any(any(w.startswith(t) for t in TURN) for w in words - words0):
            continue            # новый поворот темы
        return ts, st0, ev, nid
    return None


class SentNews:
    """Что уже отправлено владельцу в Telegram: одна и та же новость приходит от десятка каналов (06.11.2026 покупка ЦИАН —
    семь сообщений «ВАЖНАЯ НОВОСТЬ» подряд). Повтор — та же сторона и: общая бумага с тем же видом события, либо три общих
    значимых слова, либо два, если бумаг нет (новость про рынок). Память в базе: перезапуск бота её не стирает.
    На торговлю не влияет: событие разбирается и учитывается как раньше, не дублируется только сообщение."""
    KEY = "tg_news_sent"

    def __init__(self, settings, journal):
        self.s, self.j, self.lock = settings, journal, threading.Lock()

    def repeat(self, tickers, etype, direction, title, now=None):
        """True — о том же уже писали за последние notify.news_repeat_hours часов (сообщение не нужно). Иначе запоминает и даёт False."""
        hours = float(self.s.get("notify.news_repeat_hours", 12) or 0)
        if hours <= 0:
            return False
        now = now or time.time()
        tk, st = set(tickers or []), stems(title)
        with self.lock:
            sent = [x for x in (self.j.kv_get(self.KEY, []) or []) if now - x[0] <= hours * 3600]
            for _, tk0, et0, d0, st0 in sent:
                if d0 != direction:
                    continue        # та же тема с другим знаком (сделка сорвалась) — новость, а не повтор
                common = len(st & set(st0))
                if (tk & set(tk0) and et0 == etype) or common >= 3 or (not tk and not tk0 and common >= 2):
                    return True
            sent.append([int(now), sorted(tk), etype, direction, sorted(st)])
            self.j.kv_set(self.KEY, sent[-200:])
        return False


class ClusterWatch:
    """Серии общерыночных новостей об одном: min_items за window_min минут -> одно событие важности 8."""
    def __init__(self, settings, index_move=None):
        self.s, self.index_move = settings, index_move
        self.recent = []          # (ts, stems, direction, title)
        self.fired = []           # (ts, stems) уже поднятых серий
        self.lock = threading.Lock()

    def cfg(self):
        return self.s.get("clusters") or {}

    def add(self, rec, now=None):
        """rec — событие из разбора новости. Возвращает синтетическое событие серии или None."""
        c = self.cfg()
        if not c.get("enabled") or not (rec.get("market_wide") or len(rec.get("affected") or []) >= 5):
            return None
        now = now or time.time()
        st = stems(rec.get("title"))
        if len(st) < 3:
            return None
        window, need = float(c.get("window_min", 30)) * 60, int(c.get("min_items", 5))
        with self.lock:
            self.recent = [x for x in self.recent if now - x[0] <= window]
            self.fired = [x for x in self.fired if now - x[0] <= float(c.get("quiet_min", 120)) * 60]
            group = [x for x in self.recent if similar(st, x[1])]
            self.recent.append((now, st, rec.get("direction"), rec.get("title") or ""))
            if len(group) + 1 < need or any(similar(st, x[1]) for x in self.fired):
                return None
            self.fired.append((now, st))
        dirs = [x[2] for x in group] + [rec.get("direction")]
        bull, bear = dirs.count("bullish"), dirs.count("bearish")
        direction = "bullish" if bull > bear else ("bearish" if bear > bull else "neutral")
        if direction == "neutral" and self.index_move:
            # модель ставила «нейтрально»: сторону показывает сам рынок за время серии
            try:
                mv = float(self.index_move(int(window / 60)) or 0)
                direction = "bullish" if mv >= 0.15 else ("bearish" if mv <= -0.15 else "neutral")
            except Exception:  # noqa: BLE001
                pass
        title = min((x[3] for x in group), key=len, default=rec.get("title") or "")[:140] or (rec.get("title") or "")[:140]
        key = hashlib.md5(("|".join(sorted(st)) + str(int(now // 3600))).encode()).hexdigest()[:12]
        log.warning("серия новостей: %d сообщений об одном за %d мин (%s): %s", len(group) + 1, int(window / 60), direction, title[:100])
        return {"news_id": "cluster:" + key, "ts": int(now), "event_type": rec.get("event_type") or "other", "importance": 8, "direction": direction,
                "confidence": 0.8, "affected": [], "market_wide": True, "horizon_hours": 24, "n": len(group) + 1,
                "reasoning": f"серия из {len(group) + 1} новостей об одном за {int(window / 60)} минут", "title": "Серия новостей: " + title}


class MarketTheme:
    def __init__(self, settings, journal, analyst, corpcal=None, index_move=None):
        self.s, self.j, self.analyst, self.corpcal, self.index_move = settings, journal, analyst, corpcal, index_move
        self.state = json.loads(journal.kv_get("market_theme", "{}") or "{}")
        self.last_ts = float(self.state.get("ts") or 0)
        self.lock = threading.Lock()

    def cfg(self):
        return self.s.get("theme") or {}

    def due(self, now=None):
        c = self.cfg()
        if not c.get("enabled"):
            return False
        from .analyst import night_mode
        every = c.get("every_min_night", 180) if night_mode(self.s) else c.get("every_min", 60)
        return (now or time.time()) - self.last_ts >= float(every) * 60

    def refresh(self, now=None):
        """Один вызов модели: тема, сторона, уверенность. При ошибке прежняя тема остаётся до истечения срока."""
        now = now or time.time()
        if not self.analyst or not self.lock.acquire(blocking=False):
            return
        try:
            self.last_ts = now
            rows = [e for e in self.j.recent_events(12) if e.get("market_wide") or e["importance"] >= 6][:70]
            if len(rows) < 3:
                return
            lines = [f"- {time.strftime('%d.%m %H:%M', time.gmtime(e['ts'] + 10800))} важность {e['importance']}, {e['direction']}"
                     f"{', рынок' if e.get('market_wide') else ''}: {(e.get('title') or '')[:140]}" for e in rows]
            cal = "\n".join(self.corpcal.text(7, 15)) if self.corpcal else ""
            mv = ""
            if self.index_move:
                try:
                    mv = f"Индекс: за сутки {self.index_move(24 * 60):+.2f}%, за трое суток {self.index_move(72 * 60):+.2f}%."
                except Exception:  # noqa: BLE001
                    mv = ""
            user = (f"{mv}\n\nКалендарь на неделю:\n{cal or 'нет'}\n\nНовости за 12 часов (сначала свежие):\n" + "\n".join(lines))
            tier = self.analyst.spend_tier() if hasattr(self.analyst, "spend_tier") else "normal"
            # Один вызов в час: тему считает сильная модель и в экономном режиме. 06.11 вечером дешёвая дала «сторона не ясна, 0.55»,
            # сильная ночью — «ожидание переговоров, рост, 0.70–0.75»; из-за этого тема вечером не работала
            model = self.analyst.cheap if tier == "minimal" else self.analyst.model
            res = self.analyst._parse(model, SYSTEM, user, ThemeView, 2500, "low")
            if res is None:
                return
            st = {"theme": res.theme[:80], "direction": res.direction, "confidence": max(0.0, min(1.0, float(res.confidence))),
                  "horizon_days": max(1, min(5, int(res.horizon_days))), "reasoning": res.reasoning[:300], "ts": now}
            self.state = st
            self.j.kv_set("market_theme", json.dumps(st, ensure_ascii=False))
            log.info("тема рынка: %s (%s, уверенность %.2f): %s", st["theme"], st["direction"], st["confidence"], st["reasoning"][:120])
        except Exception as e:  # noqa: BLE001
            log.warning("тема рынка: %s", str(e)[:160])
        finally:
            self.lock.release()

    def active(self, now=None):
        """Действующая тема или None: есть сторона, уверенность не ниже порога, определена не слишком давно."""
        st, c = self.state, self.cfg()
        if not c.get("enabled") or not st or st.get("direction") not in ("bullish", "bearish"):
            return None
        if float(st.get("confidence") or 0) < float(c.get("min_confidence", 0.6)):
            return None
        if (now or time.time()) - float(st.get("ts") or 0) > float(c.get("max_age_min", 180)) * 60:
            return None
        return st

    def blocks(self, direction, news_driven=False):
        """Причина запрета технической сделки против темы или пусто. Сделки по свежей сильной новости тема не блокирует."""
        st = self.active()
        if not st or news_driven:
            return ""
        if (st["direction"] == "bullish" and direction == "short") or (st["direction"] == "bearish" and direction == "long"):
            return f"против темы рынка: {st['theme']} ({'рынок растёт' if st['direction'] == 'bullish' else 'рынок падает'}, уверенность {st['confidence']:.2f})"
        return ""

    def text(self):
        st = self.active()
        if not st:
            return ""
        side = "технические шорты закрыты" if st["direction"] == "bullish" else "технические лонги закрыты"
        return f"Тема рынка: {st['theme']} — {'позитив' if st['direction'] == 'bullish' else 'негатив'}, уверенность {st['confidence']:.2f}; {side}"
