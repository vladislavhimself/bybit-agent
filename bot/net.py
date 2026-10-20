"""Маршрутизация запросов: российские хосты (T-Invest, ISS Мосбиржи, российские издания) с зарубежного сервера
ходят через ретранслятор на российской машине (RU_PROXY в .env, обычно http://10.8.0.2:8080 по WireGuard),
всё остальное — напрямую. Если RU_PROXY не задан, всё идёт напрямую."""
import os
from urllib.parse import urlsplit

RU_DOMAINS = (".ru", ".su", ".рф", "moex.com", ".moex.com", "tbank.ru", "tinkoff.ru")


def ru_proxy():
    return os.getenv("RU_PROXY", "").strip()


def is_ru_host(host):
    host = (host or "").lower()
    return any(host == d.lstrip(".") or host.endswith(d) for d in RU_DOMAINS)


def proxies_for(url_or_host):
    """Словарь proxies для requests: через ретранслятор для российских хостов, пустой для остальных."""
    p = ru_proxy()
    if not p:
        return {}
    host = urlsplit(url_or_host).hostname if "://" in url_or_host else url_or_host
    return {"http": p, "https": p} if is_ru_host(host) else {}
