"""Мост на стороне головы: локальный HTTP-сервер POST /fetch (его использует bot/net.py как RELAY_URL)
и один WebSocket к брокеру (Cloudflare Worker). Запрос уходит брокеру, ответ ретранслятора возвращается
по id. Окружение: RELAY_WS=wss://bot-relay.<account>.workers.dev/ws, RELAY_KEY=...
Запуск: python3 bridge.py [--port 8082]"""
import argparse
import base64
import json
import logging
import os
import threading
import time
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from websockets.sync.client import connect

log = logging.getLogger("bridge")
waiting = {}          # id -> {"event": Event, "result": dict}
waiting_lock = threading.Lock()
ws_holder = {"ws": None}
send_lock = threading.Lock()


def ws_loop(url, key):
    backoff = 2
    while True:
        try:
            with connect(f"{url}?role=head&key={key}", max_size=8 * 1024 * 1024, open_timeout=20,
                         ping_interval=20, ping_timeout=20) as ws:
                ws_holder["ws"] = ws
                log.info("connected to broker")
                backoff = 2
                for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    with waiting_lock:
                        slot = waiting.get(msg.get("id"))
                    if slot is not None:
                        slot["result"] = msg
                        slot["event"].set()
        except Exception as e:  # noqa: BLE001
            log.warning("broker connection: %s; reconnect in %ds", str(e)[:150], backoff)
        ws_holder["ws"] = None
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)


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
        self._json(200, {"ok": True, "connected": ws_holder["ws"] is not None})

    def do_POST(self):  # noqa: N802
        if self.path != "/fetch":
            return self._json(404, {"error": "not found"})
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._json(400, {"error": "bad json"})
        ws = ws_holder["ws"]
        if ws is None:
            return self._json(503, {"error": "bridge not connected to broker"})
        rid = uuid.uuid4().hex
        req["id"] = rid
        slot = {"event": threading.Event(), "result": None}
        with waiting_lock:
            waiting[rid] = slot
        try:
            with send_lock:
                ws.send(json.dumps(req))
            if not slot["event"].wait(float(req.get("timeout") or 30) + 15):
                return self._json(504, {"error": "relay timeout"})
            res = slot["result"]
            if res.get("error"):
                return self._json(502, {"error": res["error"]})
            if res.get("gz"):
                res["body_b64"] = base64.b64encode(zlib.decompress(base64.b64decode(res["body_b64"]))).decode()
                res.pop("gz", None)
            self._json(200, res)
        except Exception as e:  # noqa: BLE001
            self._json(502, {"error": f"{type(e).__name__}: {str(e)[:200]}"})
        finally:
            with waiting_lock:
                waiting.pop(rid, None)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8082)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    threading.Thread(target=ws_loop, args=(os.environ["RELAY_WS"].rstrip("/"), os.environ["RELAY_KEY"]), daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    srv.daemon_threads = True
    log.info("bridge on 127.0.0.1:%d", a.port)
    srv.serve_forever()
