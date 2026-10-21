"""Маршрутизация запросов к российским хостам (T-Invest, ISS Мосбиржи, российские издания).

Голова бота живёт на зарубежном сервере, который российские сети не пропускают ни в одну сторону, поэтому
два режима:
  * RU_PROXY=http://host:port  — обычный HTTP-прокси (ретранслятор в WireGuard-туннеле);
  * RELAY_KEY + RELAY_URL_FILE — HTTP-ретранслятор relay/relay_http.py на российской машине, доступный
    через Cloudflare Quick Tunnel; его текущий адрес (меняется при перезапуске) ретранслятор публикует
    в приватный git-репозиторий, а голова читает из файла, который обновляет `git pull` по таймеру.
Если ничего не задано — всё напрямую (так работает запуск с машины в России или дома)."""
import base64
import logging
import os
import time
from urllib.parse import urlsplit

import requests
from requests.adapters import BaseAdapter, HTTPAdapter

log = logging.getLogger("net")
RU_DOMAINS = (".ru", ".su", ".рф", "moex.com", ".moex.com", "tbank.ru", "tinkoff.ru")
_relay_cache = {"url": "", "ts": 0.0}


def ru_proxy():
    return os.getenv("RU_PROXY", "").strip()


def relay_key():
    return os.getenv("RELAY_KEY", "").strip()


def relay_url():
    """Адрес ретранслятора из RELAY_URL (постоянный) или из файла RELAY_URL_FILE (quick tunnel). Кэш 30 с."""
    fixed = os.getenv("RELAY_URL", "").strip()
    if fixed:
        return fixed.rstrip("/")
    path = os.getenv("RELAY_URL_FILE", "").strip()
    if not path:
        return ""
    now = time.time()
    if now - _relay_cache["ts"] > 30:
        try:
            url = open(path, encoding="utf-8").read().strip()
            _relay_cache["url"] = url if url.startswith("http") else ""
        except OSError:
            _relay_cache["url"] = ""
        _relay_cache["ts"] = now
    return _relay_cache["url"].rstrip("/")


def relay_enabled():
    return bool(relay_key() and (os.getenv("RELAY_URL") or os.getenv("RELAY_URL_FILE")))


def is_ru_host(host):
    host = (host or "").lower()
    return any(host == d.lstrip(".") or host.endswith(d) for d in RU_DOMAINS)


def proxies_for(url_or_host):
    """Словарь proxies для requests в режиме RU_PROXY; пустой для нероссийских хостов и в режиме relay."""
    p = ru_proxy()
    if not p:
        return {}
    host = urlsplit(url_or_host).hostname if "://" in url_or_host else url_or_host
    return {"http": p, "https": p} if is_ru_host(host) else {}


class RelayAdapter(BaseAdapter):
    """Адаптер requests: упаковывает запрос в JSON, отправляет ретранслятору, собирает Response из ответа."""

    def __init__(self):
        super().__init__()
        self.http = requests.Session()
        self.http.trust_env = False

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):  # noqa: D102
        base = relay_url()
        if not base:
            raise requests.ConnectionError("адрес ретранслятора ещё не известен (relay url пуст)")
        read_to = timeout[1] if isinstance(timeout, tuple) else (timeout or 30)
        body = request.body
        if isinstance(body, str):
            body = body.encode()
        payload = {"method": request.method, "url": request.url, "headers": dict(request.headers),
                   "body_b64": base64.b64encode(body or b"").decode(), "timeout": read_to}
        for attempt in range(3):
            try:
                r = self.http.post(base + "/fetch", json=payload, headers={"X-Relay-Key": relay_key()},
                                   timeout=(10, read_to + 15))
                break
            except requests.RequestException as e:
                if attempt == 2:
                    raise requests.ConnectionError(f"ретранслятор недоступен: {e}") from e
                time.sleep(1 + attempt)
        if r.status_code != 200:
            raise requests.ConnectionError(f"ретранслятор ответил {r.status_code}: {r.text[:200]}")
        data = r.json()
        resp = requests.Response()
        resp.status_code = int(data["status"])
        resp.reason = data.get("reason", "")
        resp.headers.update(data.get("headers") or {})
        resp._content = base64.b64decode(data.get("body_b64") or "")
        resp.url = request.url
        resp.request = request
        resp.encoding = requests.utils.get_encoding_from_headers(resp.headers)
        return resp

    def close(self):  # noqa: D102
        self.http.close()


class RoutedSession(requests.Session):
    """Сессия, которая сама решает, как ходить к хосту: через ретранслятор, через RU_PROXY или напрямую."""

    def __init__(self):
        super().__init__()
        self.trust_env = False
        self._relay = RelayAdapter() if relay_enabled() else None
        self._direct = HTTPAdapter(pool_maxsize=20)

    def get_adapter(self, url):
        host = urlsplit(url).hostname
        if self._relay is not None and is_ru_host(host) and not self._prefer_direct(host):
            return self._relay
        return self._direct

    # хосты, до которых из России хуже, чем напрямую (RBC, Forbes, Смарт-лаб...): запоминаем на час
    _direct_until = {}

    def _prefer_direct(self, host):
        return self._direct_until.get(host, 0) > time.time()

    def request(self, method, url, **kw):
        if not kw.get("proxies") and self._relay is None:
            p = proxies_for(url)
            if p:
                kw["proxies"] = p
        host = urlsplit(url).hostname
        try:
            return super().request(method, url, **kw)
        except requests.ConnectionError as e:
            # ретранслятор не справился: один раз пробуем напрямую и, если вышло, час ходим напрямую
            if self._relay is None or not is_ru_host(host) or self._prefer_direct(host):
                raise
            log.info("%s через ретранслятор не отвечает (%s), пробую напрямую", host, str(e)[:80])
            self._direct_until[host] = time.time() + 3600
            try:
                return super().request(method, url, **kw)
            except requests.RequestException:
                self._direct_until.pop(host, None)
                raise e


def session():
    return RoutedSession()
