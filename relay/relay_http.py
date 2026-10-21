"""HTTP-ретранслятор на российской машине. Голова (Германия) не может открыть российские сайты напрямую,
а Россия не пускает пакеты к немецкому хостеру, поэтому связь идёт через Cloudflare Tunnel: голова шлёт
POST /fetch с описанием запроса, ретранслятор выполняет его с российского адреса и возвращает ответ.

Защита: заголовок X-Relay-Key должен совпадать с RELAY_KEY из окружения; пересылаются только запросы
к российским хостам (список RU_SUFFIXES), чтобы ретранслятор нельзя было использовать как открытый прокси.
Запуск: RELAY_KEY=... python3 relay_http.py [--port 8081]"""
import argparse
import base64
import json
import logging
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import requests

log = logging.getLogger("relay")
RU_SUFFIXES = (".ru", ".su", "moex.com", "tbank.ru", "tinkoff.ru")
KEY = os.environ.get("RELAY_KEY", "")
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "content-length", "content-encoding", "host"}
session = requests.Session()


def allowed_host(host):
    host = (host or "").lower()
    return any(host == s.lstrip(".") or host.endswith(s) for s in RU_SUFFIXES)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # noqa: D401
        log.debug(fmt, *args)

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            return self._json(200, {"ok": True})
        self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path != "/fetch":
            return self._json(404, {"error": "not found"})
        if not KEY or self.headers.get("X-Relay-Key") != KEY:
            return self._json(403, {"error": "forbidden"})
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            url = req["url"]
            host = urlsplit(url).hostname
            if not allowed_host(host):
                return self._json(403, {"error": f"host not allowed: {host}"})
            body = base64.b64decode(req.get("body_b64") or "") or None
            headers = {k: v for k, v in (req.get("headers") or {}).items() if k.lower() not in HOP}
            r = session.request(req.get("method", "GET"), url, headers=headers, data=body,
                                timeout=(10, float(req.get("timeout") or 30)), allow_redirects=False)
            out = {"status": r.status_code, "reason": r.reason,
                   "headers": {k: v for k, v in r.headers.items() if k.lower() not in HOP},
                   "body_b64": base64.b64encode(r.content).decode()}
            self._json(200, out)
        except requests.RequestException as e:
            self._json(502, {"error": f"{type(e).__name__}: {str(e)[:200]}"})
        except Exception as e:  # noqa: BLE001
            log.exception("fetch")
            self._json(500, {"error": f"{type(e).__name__}: {str(e)[:200]}"})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8081)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not KEY:
        print("RELAY_KEY не задан", file=sys.stderr)
        sys.exit(1)
    srv = ThreadingHTTPServer((a.bind, a.port), Handler)
    srv.daemon_threads = True
    log.info("relay on %s:%d", a.bind, a.port)
    srv.serve_forever()
