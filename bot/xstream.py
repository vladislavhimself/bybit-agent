"""X Activity API: поток событий post.create по выбранным аккаунтам в реальном времени.
Оплата по факту: ~0.005$ за доставленное событие, ~0.01$ за разовое получение id пользователя.
Поток живёт в отдельной нити, новые посты отдаются в callback и попадают в общий конвейер новостей."""
import json
import logging
import os
import threading
import time
from datetime import datetime

import requests

log = logging.getLogger("xstream")
API = "https://api.x.com/2"


class XActivityStream:
    def __init__(self, settings, journal, on_items, accounts):
        self.s, self.j, self.on_items = settings, journal, on_items
        self.accounts = [a.lstrip("@") for a in accounts]
        self.token = os.getenv("TWITTER_BEARER_TOKEN", "").strip()
        self.enabled = bool(self.token and self.accounts)
        self.disabled_reason = ""
        self.stop = threading.Event()
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {self.token}", "User-Agent": "bybit-agent/0.2"})
        self.ids = {}         # username -> user_id
        self.events_seen = 0
        self.connected_at = 0.0

    # ---------- подготовка ----------
    def _resolve_ids(self):
        cache = self.j.kv_get("x_user_ids", {})
        for name in self.accounts:
            if name in cache:
                self.ids[name] = cache[name]
                continue
            try:
                r = self.session.get(f"{API}/users/by/username/{name}", timeout=15)
                if r.status_code == 200:
                    uid = r.json()["data"]["id"]
                    self.ids[name] = cache[name] = uid
                    log.info("X: @%s -> id %s", name, uid)
                elif r.status_code in (401, 402, 403):
                    self._disable(f"нет доступа к X API (HTTP {r.status_code}): {r.text[:120]}")
                    return False
                else:
                    log.warning("X: @%s не найден (HTTP %s)", name, r.status_code)
            except requests.RequestException as e:
                log.warning("X: resolve @%s: %s", name, e)
        self.j.kv_set("x_user_ids", cache)
        return bool(self.ids)

    def _existing_subscriptions(self):
        try:
            r = self.session.get(f"{API}/activity/subscriptions", timeout=15)
            if r.status_code != 200:
                return set()
            data = r.json().get("data", [])
            if isinstance(data, dict):
                data = data.get("subscriptions", [])
            out = set()
            for sub in data:
                if sub.get("event_type") == "post.create":
                    out.add(str((sub.get("filter") or {}).get("user_id", "")))
            return out
        except (requests.RequestException, ValueError) as e:
            log.debug("X: list subscriptions: %s", e)
            return set()

    def _ensure_subscriptions(self):
        have = self._existing_subscriptions()
        for name, uid in self.ids.items():
            if str(uid) in have:
                continue
            body = {"event_type": "post.create", "filter": {"user_id": str(uid)}, "tag": f"bot-{name}"}
            try:
                r = self.session.post(f"{API}/activity/subscriptions", json=body, timeout=15)
                if r.status_code in (200, 201):
                    log.info("X: подписка на посты @%s создана", name)
                elif r.status_code in (401, 402, 403):
                    self._disable(f"подписка запрещена (HTTP {r.status_code}): {r.text[:160]}")
                    return False
                else:
                    log.warning("X: подписка @%s: HTTP %s %s", name, r.status_code, r.text[:160])
            except requests.RequestException as e:
                log.warning("X: subscribe @%s: %s", name, e)
        return True

    def _disable(self, reason):
        self.enabled = False
        self.disabled_reason = reason
        log.error("X Activity API отключён: %s", reason)

    # ---------- поток ----------
    def _parse_event(self, obj):
        d = obj.get("data") or obj
        if d.get("event_type") != "post.create":
            return None
        p = d.get("payload") or {}
        text = p.get("text") or ""
        if len(text) < 5:
            return None
        users = {u.get("id"): u.get("username") for u in (d.get("includes") or {}).get("users", [])}
        who = users.get(p.get("author_id")) or next(
            (n for n, i in self.ids.items() if str(i) == str((d.get("filter") or {}).get("user_id"))), "x")
        try:
            ts = int(datetime.fromisoformat((p.get("created_at") or d.get("created_at")).replace("Z", "+00:00")).timestamp())
        except (AttributeError, ValueError):
            ts = int(time.time())
        pid = str(p.get("id") or d.get("event_uuid") or ts)
        return {"id": "xs_" + pid[-14:], "source": f"x:{who}", "title": text[:160], "summary": text,
                "url": f"https://x.com/{who}/status/{pid}", "published_ts": ts}

    def _stream_once(self, backfill):
        params = {"backfill_minutes": backfill} if backfill else {}
        with self.session.get(f"{API}/activity/stream", params=params, stream=True, timeout=(15, 90)) as r:
            if r.status_code in (401, 402, 403):
                self._disable(f"поток запрещён (HTTP {r.status_code}): {r.text[:160]}")
                return
            if r.status_code == 429:
                log.warning("X: лимит подключений к потоку, пауза 60 с")
                time.sleep(60)
                return
            if r.status_code != 200:
                log.warning("X: поток HTTP %s: %s", r.status_code, r.text[:160])
                time.sleep(15)
                return
            self.connected_at = time.time()
            log.info("X: поток подключён (%d аккаунтов)", len(self.ids))
            for line in r.iter_lines():
                if self.stop.is_set():
                    return
                if not line:
                    continue   # keep-alive
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                item = self._parse_event(obj)
                if item:
                    self.events_seen += 1
                    lag = time.time() - item["published_ts"]
                    log.info("X: @%s (задержка %.0fс): %s", item["source"][2:], lag, item["title"][:80])
                    self.on_items([item])

    def run(self):
        if not self.enabled:
            return
        if not self._resolve_ids() or not self._ensure_subscriptions():
            return
        backoff, first = 5, True
        while not self.stop.is_set() and self.enabled:
            try:
                self._stream_once(backfill=0 if first else 5)
                first = False
                backoff = 5
            except requests.RequestException as e:
                log.warning("X: поток оборвался: %s, переподключение через %d с", e, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)
            except Exception as e:  # noqa: BLE001
                log.error("X: ошибка потока: %s", e)
                time.sleep(backoff)

    def start(self):
        if self.enabled:
            threading.Thread(target=self.run, daemon=True, name="xstream").start()
        return self.enabled

    def status(self):
        if not self.token:
            return "выкл (нет TWITTER_BEARER_TOKEN)"
        if not self.enabled:
            return f"выкл: {self.disabled_reason[:80]}"
        up = "подключён" if self.connected_at and time.time() - self.connected_at < 7 * 24 * 3600 else "нет связи"
        return f"{up}, {len(self.ids)} аккаунтов, событий {self.events_seen}"
