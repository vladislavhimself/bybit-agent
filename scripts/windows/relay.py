"""Ретранслятор для российской машины: HTTP-прокси (метод CONNECT и обычные GET), слушает только на адресе
WireGuard-туннеля, чтобы снаружи им нельзя было воспользоваться. Голова бота в Германии ходит через него
к T-Invest, ISS Мосбиржи и российским новостным лентам. Без внешних зависимостей: только стандартная библиотека.
Запуск: python scripts/windows/relay.py [--bind 10.8.0.2] [--port 8080]"""
import argparse
import logging
import select
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

log = logging.getLogger("relay")
ALLOWED_PEERS = ("10.8.0.",)   # обращаться к ретранслятору может только голова через туннель


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 60

    def log_message(self, fmt, *args):  # noqa: D401
        log.debug(fmt, *args)

    def _allowed(self):
        if not self.client_address[0].startswith(ALLOWED_PEERS):
            self.send_error(403)
            return False
        return True

    def do_CONNECT(self):  # noqa: N802
        if not self._allowed():
            return
        host, _, port = self.path.rpartition(":")
        try:
            remote = socket.create_connection((host, int(port or 443)), timeout=15)
        except OSError as e:
            self.send_error(502, f"connect failed: {e}")
            return
        self.send_response(200, "Connection established")
        self.end_headers()
        self._pipe(self.connection, remote)

    def _pipe(self, a, b):
        socks = [a, b]
        try:
            while True:
                r, _, x = select.select(socks, [], socks, 300)
                if x or not r:
                    break
                for s in r:
                    data = s.recv(65536)
                    if not data:
                        return
                    (b if s is a else a).sendall(data)
        except OSError:
            pass
        finally:
            for s in socks:
                try:
                    s.close()
                except OSError:
                    pass

    def do_GET(self):  # noqa: N802
        self._forward()

    def do_POST(self):  # noqa: N802
        self._forward()

    def _forward(self):
        """Обычный (не CONNECT) запрос по абсолютному URL: пересылаем как есть."""
        if not self._allowed():
            return
        u = urlsplit(self.path)
        if not u.hostname:
            self.send_error(400)
            return
        port = u.port or (443 if u.scheme == "https" else 80)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        head = f"{self.command} {path} HTTP/1.1\r\n".encode()
        for k, v in self.headers.items():
            if k.lower() in ("proxy-connection", "proxy-authorization"):
                continue
            head += f"{k}: {v}\r\n".encode()
        head += b"Connection: close\r\n\r\n"
        try:
            remote = socket.create_connection((u.hostname, port), timeout=15)
            if u.scheme == "https":
                import ssl
                remote = ssl.create_default_context().wrap_socket(remote, server_hostname=u.hostname)
            remote.sendall(head + body)
            while True:
                chunk = remote.recv(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
            remote.close()
        except OSError as e:
            self.send_error(502, f"forward failed: {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="10.8.0.2")
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    srv = ThreadingHTTPServer((a.bind, a.port), Handler)
    srv.daemon_threads = True
    log.info("relay listening on %s:%d", a.bind, a.port)
    srv.serve_forever()
