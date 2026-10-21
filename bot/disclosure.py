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
        n = _norm(issuer)
        for name, base in self._issuers().items():
            if name in n or n in name:
                return base
        return None

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
            body = self._page(BASE + href)
            items.append({"id": hashlib.sha1(href.encode()).hexdigest()[:16], "source": "disclosure",
                          "title": f"{_norm(issuer).title()} ({base}): {title}", "summary": body or title,
                          "url": BASE + href, "published_ts": int(ts)})
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
            i = txt.find("Сообщение о существенном факте")
            if i < 0:
                i = txt.find("1. Общие сведения")
            if i < 0:
                i = max(0, txt.find("Главная Лента новостей") + 200)
            return txt[i:i + 6000]
        except Exception as e:  # noqa: BLE001
            log.debug("страница раскрытия %s: %s", url, e)
            return ""
