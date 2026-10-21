"""Ретранслятор на российской машине: держит WebSocket к брокеру (Cloudflare Worker), получает запросы головы,
выполняет их с российского адреса и отправляет ответ обратно. Пересылаются только российские хосты.
Окружение: RELAY_WS=wss://bot-relay.<account>.workers.dev/ws, RELAY_KEY=..., RELAY_CA=/opt/relay/ca_bundle.pem
Запуск: python3 agent.py"""
import base64
import json
import logging
import os
import socket
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import requests
from websockets.sync.client import connect

log = logging.getLogger("agent")
RU_SUFFIXES = (".ru", ".su", "moex.com", "tbank.ru", "tinkoff.ru")
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "content-length", "content-encoding", "host"}
session = requests.Session()
# T-Bank иногда молча рвёт простаивающие соединения: один повтор на обрыв/таймаут чтения вместо ошибки голове
from requests.adapters import HTTPAdapter  # noqa: E402
from urllib3.util.retry import Retry  # noqa: E402
session.mount("https://", HTTPAdapter(pool_maxsize=32, max_retries=Retry(total=1, connect=1, read=1, backoff_factor=0.5,
                                                                           allowed_methods=None, status_forcelist=())))
VERIFY = os.environ.get("RELAY_CA") or True   # связка с российским корневым сертификатом для *.tbank.ru
pool = ThreadPoolExecutor(max_workers=6)
send_lock = threading.Lock()
# бережный ритм к T-Bank, чтобы защита от DDoS не приняла бота за сканер: не больше RATE_PER_MIN запросов
# в минуту и не больше 4 одновременных соединений к хостам tbank
RATE_PER_MIN = int(os.environ.get("RELAY_RATE_PER_MIN", "150"))
_rate_lock = threading.Lock()
_rate_times = []
_tbank_sem = threading.Semaphore(4)


def _throttle(host):
    if "tbank" not in host and "tinkoff" not in host:
        return
    while True:
        with _rate_lock:
            now = time.time()
            while _rate_times and now - _rate_times[0] > 60:
                _rate_times.pop(0)
            if len(_rate_times) < RATE_PER_MIN:
                _rate_times.append(now)
                return
            wait = 60 - (now - _rate_times[0]) + 0.05
        time.sleep(min(max(wait, 0.1), 5))


def allowed_host(host):
    host = (host or "").lower()
    return any(host == s.lstrip(".") or host.endswith(s) for s in RU_SUFFIXES)


def execute(req):
    try:
        url = req["url"]
        host = urlsplit(url).hostname
        if not allowed_host(host):
            return {"id": req.get("id"), "error": f"host not allowed: {host}"}
        body = base64.b64decode(req.get("body_b64") or "") or None
        headers = {k: v for k, v in (req.get("headers") or {}).items() if k.lower() not in HOP}
        _throttle(host)
        sem = _tbank_sem if ("tbank" in host or "tinkoff" in host) else None
        if sem:
            sem.acquire()
        try:
            r = session.request(req.get("method", "GET"), url, headers=headers, data=body,
                                timeout=(10, float(req.get("timeout") or 30)), allow_redirects=False, verify=VERIFY)
        finally:
            if sem:
                sem.release()
        return {"id": req.get("id"), "status": r.status_code, "reason": r.reason,
                "headers": {k: v for k, v in r.headers.items() if k.lower() not in HOP},
                "gz": True, "body_b64": base64.b64encode(zlib.compress(r.content, 6)).decode()}
    except requests.RequestException as e:
        return {"id": req.get("id"), "error": f"{type(e).__name__}: {str(e)[:200]}"}
    except Exception as e:  # noqa: BLE001
        log.exception("execute")
        return {"id": req.get("id"), "error": f"{type(e).__name__}: {str(e)[:200]}"}


def handle(ws, raw):
    try:
        req = json.loads(raw)
    except ValueError:
        return
    res = execute(req)
    with send_lock:
        try:
            ws.send(json.dumps(res))
        except Exception as e:  # noqa: BLE001
            log.warning("send: %s", e)


def open_socket(host, port=443):
    """Российские фильтры режут часть адресов Cloudflare: перебираем все IPv4 хоста, берём первый живой."""
    ips = sorted({ai[4][0] for ai in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)})
    if LAST_GOOD["ip"] in ips:
        ips.remove(LAST_GOOD["ip"])
        ips.insert(0, LAST_GOOD["ip"])
    last = None
    for ip in ips:
        try:
            sock = socket.create_connection((ip, port), timeout=6)
            sock.settimeout(10)
            LAST_GOOD["ip"] = ip
            return sock
        except OSError as e:
            last = e
            log.debug("connect %s: %s", ip, e)
    raise OSError(f"no reachable address for {host}: {last}")


LAST_GOOD = {"ip": ""}


def keepalive():
    """T-Bank принимает не каждое новое соединение с адресов дата-центров: держим пул соединений тёплым."""
    while True:
        for h in ("https://sandbox-invest-public-api.tbank.ru/", "https://invest-public-api.tbank.ru/"):
            try:
                session.get(h, timeout=(6, 10), verify=VERIFY)
            except requests.RequestException:
                pass
        time.sleep(15)


def main():
    if os.environ.get("RELAY_KEEPALIVE") == "1":   # выключено по умолчанию: частые соединения могут выглядеть как атака
        threading.Thread(target=keepalive, daemon=True, name="keepalive").start()
    url = os.environ["RELAY_WS"].rstrip("/")
    key = os.environ["RELAY_KEY"]
    host = urlsplit(url).hostname
    backoff = 2
    while True:
        try:
            sock = open_socket(host)
            with connect(f"{url}?role=relay&key={key}", sock=sock, server_hostname=host, max_size=8 * 1024 * 1024,
                         open_timeout=20, ping_interval=20, ping_timeout=20) as ws:
                log.info("connected to broker")
                backoff = 2
                for raw in ws:
                    pool.submit(handle, ws, raw)
        except Exception as e:  # noqa: BLE001
            log.warning("broker connection: %s; reconnect in %ds", str(e)[:150], backoff)
            LAST_GOOD["ip"] = ""
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
