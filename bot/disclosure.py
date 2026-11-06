"""Раскрытие эмитентов с первоисточника: лента существенных фактов сервера раскрытия ПРАЙМ
(disclosure.1prime.ru — один из пяти уполномоченных агентств; e-disclosure Интерфакса закрыт антиботом).
Сообщения по компаниям из нашей вселенной (дивиденды, решения советов директоров, сделки, допэмиссии,
отчётность) попадают в общий новостной конвейер как источник "disclosure" с полным текстом сообщения."""
import hashlib
import logging
import re
import time
from datetime import datetime, timezone, timedelta

from bs4 import BeautifulSoup

from . import net

log = logging.getLogger("disclosure")
BASE = "https://disclosure.1prime.ru"
MSK = timezone(timedelta(hours=3))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
      "Accept-Language": "ru"}
# типы сообщений, которые двигают цену; остальное (созыв собрания, изменение адреса) не тратим на модель
HOT = re.compile(r"дивиденд|решени[яе] совета директоров|существенное влияние|отчетн|отчётн|приобретени|выкуп|"
                 r"дополнительн\w+ (выпуск|акци)|реорганизац|крупн\w+ сделк|рейтинг|дефолт|прекращени|заявк|"
                 r"финансов\w+ результат|прибыл|убыт|купон|оферт", re.I)
# Суть раскрытия стоит не в начале: у решения совета директоров сначала кворум и итоги голосования. 06.11 в сообщении X5
# слово «дивиденд» было на 732-м символе, сумма — на 990-м; фильтр видел первые 300 и отбрасывал сообщение.
DIV_AMT = re.compile(r"дивиденд[^.;]{0,260}?в размере\s+(\d[\d\s\xa0]*(?:[.,]\d+)?)\s*(?:\([^)]{0,80}\))?\s*руб", re.I)
DIV_NONE = re.compile(r"(не выплачивать|не объявлять|не распределять)[^.;]{0,80}дивиденд|дивиденд[^.;]{0,120}(не выплачивать|не объявлять)", re.I)
DIV_PERIOD = re.compile(r"по (?:результатам|итогам)\s+((?:[\w\d]+\s+){0,3}?20\d\d\s*года?)", re.I)
KEYS = re.compile(r"дивиденд|выкуп|оферт|приобретени|дополнительн\w+ выпуск|реорганизац|чист\w+ прибыл|выручк|крупн\w+ сделк", re.I)


def essence(body):
    """(добавка к заголовку, текст с сутью в начале, дивиденд на акцию или None). Без модели, по тексту раскрытия."""
    txt = body or ""
    add, div = "", None
    m = DIV_AMT.search(txt)
    if m:
        try:
            div = float(re.sub(r"[\s\xa0]", "", m.group(1)).replace(",", "."))
        except ValueError:
            div = None
    if div:
        p = DIV_PERIOD.search(txt[max(0, m.start() - 20):m.end()]) or DIV_PERIOD.search(txt)
        add = f"дивиденды {div:g} руб. на акцию" + (f" по итогам {' '.join(p.group(1).split())}" if p else "")
    elif DIV_NONE.search(txt):
        add = "дивиденды не выплачивать"
    k = KEYS.search(txt)
    if k and k.start() > 250:
        i = max(0, k.start() - 150)
        txt = "Суть: " + txt[i:i + 900] + " ... Полный текст: " + txt
    return add, txt, div


STRIP = re.compile(r"\b(ПАО|ОАО|АО|ООО|публичное акционерное общество|акционерное общество|открытое|"
                   r"общество с ограниченной ответственностью)\b|[«»\"']", re.I)


def _norm(name):
    return re.sub(r"\s+", " ", STRIP.sub(" ", name or "")).strip().lower()


class Disclosure:
    def __init__(self, settings, exchange):
        self.s, self.ex = settings, exchange
        self.session = net.session()
        self.session.headers.update(UA)
        self.seen = set()
        self.first = True
        self.names = {}        # нормализованное имя эмитента -> базовый тикер
        self._names_ts = 0.0

    def _issuers(self):
        if time.time() - self._names_ts < 6 * 3600:
            return self.names
        out = {}
        try:
            for sym, i in self.ex.instruments().items():
                if i["kind"] == "share" and i.get("name"):
                    n = _norm(i["name"])
                    if len(n) >= 4:
                        out[n] = i["base"]
        except Exception as e:  # noqa: BLE001
            log.warning("эмитенты: %s", e)
            return self.names
        # ручные синонимы: в раскрытии полные названия, у брокера короткие
        out.update({"сбербанк россии": "SBER", "газпром": "GAZP", "лукойл": "LKOH", "нк роснефть": "ROSN",
                    "новатэк": "NVTK", "гмк норильский никель": "GMKN", "татнефть им. в.д. шашина": "TATN",
                    "банк втб": "VTBR", "магнит": "MGNT", "полюс": "PLZL", "северсталь": "CHMF", "московская биржа": "MOEX",
                    "аэрофлот": "AFLT", "сургутнефтегаз": "SNGS", "фосагро": "PHOR", "мтс": "MTSS", "алроса": "ALRS"})
        self.names, self._names_ts = out, time.time()
        return out

    def _match(self, issuer):
        """Сначала точное совпадение, затем самое длинное название, входящее в название эмитента:
        «Газпром нефть» должен попасть в SIBN, а не в GAZP; «Магнитогорский комбинат» — не в «Магнит»."""
        n = _norm(issuer)
        names = self._issuers()
        if n in names:
            return names[n]
        best = None
        for name, base in names.items():
            if name and name in n and (best is None or len(name) > len(best[0])):
                best = (name, base)
        return best[1] if best else None

    def poll(self, now, max_age=6 * 3600):
        """Новые сообщения по нашим эмитентам: список элементов для новостного конвейера."""
        items = []
        try:
            r = self.session.get(BASE + "/", timeout=30)
            soup = BeautifulSoup(r.text, "lxml")
        except Exception as e:  # noqa: BLE001
            log.warning("ПРАЙМ раскрытие: %s", str(e)[:120])
            return items
        for a in soup.find_all("a", href=re.compile(r"/news/-?\d+/\{")):
            href = a["href"].strip()
            if href in self.seen:
                continue
            self.seen.add(href)
            if self.first:
                continue   # при старте только запоминаем ленту, не торгуем старое
            row = a.find_parent(["tr", "li", "div"])
            text = re.sub(r"\s+", " ", (row.get_text(" ", strip=True) if row else a.get_text(" ", strip=True)))
            m = re.match(r"(\d{2}\.\d{2}\.\d{4}) (\d{2}:\d{2}):?(\d{2})? (.*)", text)
            if not m:
                continue
            ts = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%d.%m.%Y %H:%M").replace(tzinfo=MSK).timestamp()
            if now - ts > max_age:
                continue
            rest = m.group(4)
            # строка: <эмитент> <тип сообщения>; эмитент заканчивается на закрывающую кавычку
            q = max(rest.rfind("»"), rest.rfind('"'))
            issuer, title = (rest[:q + 1], rest[q + 1:].strip()) if q > 0 else (rest, rest)
            base = self._match(issuer)
            if not base or not HOT.search(title):
                continue
            add, body, div = essence(self._page(BASE + href))
            items.append({"id": hashlib.sha1(href.encode()).hexdigest()[:16], "source": "disclosure",
                          "title": f"{_norm(issuer).title()} ({base}): {title}" + (f" — {add}" if add else ""),
                          "summary": body or title, "url": BASE + href, "published_ts": int(ts), "base": base, "div": div})
            log.info("Раскрытие %s: %s", base, title[:80])
        if self.first:
            self.first = False
            log.info("ПРАЙМ раскрытие: лента запомнена (%d сообщений)", len(self.seen))
        if len(self.seen) > 5000:
            self.seen = set(list(self.seen)[-2000:])
        return items

    def _page(self, url):
        try:
            r = self.session.get(url, timeout=30)
            soup = BeautifulSoup(r.text, "lxml")
            for t in soup.find_all(["script", "style", "nav", "header", "footer"]):
                t.decompose()
            txt = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
            i = txt.find("2. Содержание сообщения")   # реквизиты эмитента модели не нужны, суть — во втором разделе
            if i < 0:
                i = txt.find("Сообщение о существенном факте")
            if i < 0:
                i = txt.find("1. Общие сведения")
            if i < 0:
                i = max(0, txt.find("Главная Лента новостей") + 200)
            return txt[i:i + 6000]
        except Exception as e:  # noqa: BLE001
            log.debug("страница раскрытия %s: %s", url, e)
            return ""
