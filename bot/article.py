"""Полный текст новости по ссылке: заголовок RSS часто вводит в заблуждение, направление ясно только из тела.
Текст вытаскивается эвристикой readability: блок с наибольшей массой текста и малой долей ссылок, из вложенных
берётся самый глубокий, который держит основную часть текста (чтобы не захватить всю страницу с меню).
Российские сайты скачиваются через ретранслятор (bot.net), остальные напрямую. Сайты, отдающие пустую страницу
без JavaScript или с антиботом, остаются на анонсе из RSS."""
import logging
import re
import time

from bs4 import BeautifulSoup

from . import net

log = logging.getLogger("article")
MAX_CHARS = 7000
_cache = {}   # url -> (ts, text)
_session = None
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
      "Accept-Language": "ru,en;q=0.8"}
JUNK = ("nav", "header", "footer", "aside", "script", "style", "noscript", "form", "iframe", "svg", "figure", "button")
JUNK_CLASS = re.compile(r"(comment|share|social|subscribe|related|promo|banner|advert|cookie|menu|sidebar|teaser|tags|recommend)", re.I)
WS = re.compile("[ \t ]+")


def _sess():
    global _session
    if _session is None:
        _session = net.session()
        _session.headers.update(UA)
    return _session


def _text(el):
    return WS.sub(" ", el.get_text("\n", strip=True))


def extract(html):
    soup = BeautifulSoup(html, "lxml")
    for t in soup.find_all(JUNK):
        t.decompose()
    for t in soup.find_all(attrs={"class": JUNK_CLASS}):
        if len(_text(t)) < 400:   # мусорный блок; большой блок с таким классом может быть самой статьёй
            t.decompose()
    cands = []
    for el in soup.find_all(["article", "main", "section", "div"]):
        txt = _text(el)
        n = len(txt)
        if n < 300:
            continue
        link_len = sum(len(a.get_text(strip=True)) for a in el.find_all("a"))
        density = link_len / n
        if density > 0.35:
            continue
        cands.append((n * (1 - density), n, txt))
    if not cands:
        ps = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        return "\n".join(p for p in ps if len(p) > 40)[:MAX_CHARS]
    best_score = max(c[0] for c in cands)
    inner = min((c for c in cands if c[0] >= 0.7 * best_score), key=lambda c: c[1])
    lines = [l.strip() for l in inner[2].split("\n")]
    lines = [l for l in lines if len(l) > 30]
    text = "\n".join(lines)
    return re.sub(r"\n{2,}", "\n", text).strip()[:MAX_CHARS]


def fetch_text(url, timeout=20):
    """Текст статьи или "" (тогда используем анонс из RSS). Кэш 6 часов."""
    if not url or not url.startswith("http"):
        return ""
    c = _cache.get(url)
    if c and time.time() - c[0] < 6 * 3600:
        return c[1]
    text = ""
    try:
        r = _sess().get(url, timeout=timeout, allow_redirects=True)
        ctype = r.headers.get("content-type", "")
        if r.status_code == 200 and ("html" in ctype or "xml" in ctype or not ctype):
            text = extract(r.text)
            if len(text) < 300:   # пустышка, пейволл или антибот: лучше анонс из RSS
                text = ""
        else:
            log.debug("article %s: %s %s", url, r.status_code, ctype[:30])
    except Exception as e:  # noqa: BLE001
        log.debug("article %s: %s", url, e)
    if len(_cache) > 2000:
        _cache.clear()
    _cache[url] = (time.time(), text)
    return text
