"""Ретранслятор на российской машине: держит WebSocket к брокеру (Cloudflare Worker), получает запросы головы,
выполняет их с российского адреса и отправляет ответ обратно. Пересылаются только российские хосты.
Окружение: RELAY_WS=wss://bot-relay.<account>.workers.dev/ws, RELAY_KEY=...
Запуск: python3 agent.py"""
import base64
import json
import logging
import os
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
pool = ThreadPoolExecutor(max_workers=16)
send_lock = threading.Lock()


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
        r = session.request(req.get("method", "GET"), url, headers=headers, data=body,
                            timeout=(10, float(req.get("timeout") or 30)), allow_redirects=False)
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


def main():
    url = os.environ["RELAY_WS"].rstrip("/")
    key = os.environ["RELAY_KEY"]
    backoff = 2
    while True:
        try:
            with connect(f"{url}?role=relay&key={key}", max_size=8 * 1024 * 1024, open_timeout=20,
                         ping_interval=20, ping_timeout=20) as ws:
                log.info("connected to broker")
                backoff = 2
                for raw in ws:
                    pool.submit(handle, ws, raw)
        except Exception as e:  # noqa: BLE001
            log.warning("broker connection: %s; reconnect in %ds", str(e)[:150], backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
