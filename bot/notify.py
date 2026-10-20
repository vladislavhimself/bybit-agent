"""Telegram: отчёты о балансе, сделки, команды управления (/status /stop /panic /resume /approve)."""
import logging
import re
import threading
import time

import requests

log = logging.getLogger("notify")


class Telegram:
    def __init__(self, token, chat_id):
        self.token, self.chat_id = token, str(chat_id)
        self.enabled = bool(token and chat_id)
        self.base = f"https://api.telegram.org/bot{token}"
        self.offset = None
        self.commands = []          # очередь входящих команд (text)
        self.lock = threading.Lock()
        if self.enabled:
            threading.Thread(target=self._poll_loop, daemon=True).start()

    def send(self, text, html=False):
        """html=True: текст с тегами <b>; если Telegram не принял разметку, уходит как обычный текст без тегов."""
        if not self.enabled:
            log.info("[TG] %s", text.replace("\n", " | "))
            return
        try:
            for chunk in (text[i:i + 3900] for i in range(0, len(text), 3900)):
                body = {"chat_id": self.chat_id, "text": chunk}
                if html:
                    body["parse_mode"] = "HTML"
                r = requests.post(f"{self.base}/sendMessage", json=body, timeout=15)
                if html and r.status_code != 200:
                    plain = re.sub(r"</?b>", "", chunk).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
                    requests.post(f"{self.base}/sendMessage", json={"chat_id": self.chat_id, "text": plain}, timeout=15)
        except requests.RequestException as e:
            log.warning("Telegram send: %s", e)

    def _poll_loop(self):
        while True:
            try:
                params = {"timeout": 30}
                if self.offset:
                    params["offset"] = self.offset
                r = requests.get(f"{self.base}/getUpdates", params=params, timeout=40).json()
                for u in r.get("result", []):
                    self.offset = u["update_id"] + 1
                    msg = u.get("message") or {}
                    if str(msg.get("chat", {}).get("id")) != self.chat_id:
                        continue  # команды принимаем только от владельца
                    text = (msg.get("text") or "").strip()
                    if text.startswith("/"):
                        with self.lock:
                            self.commands.append(text)
            except Exception as e:  # noqa: BLE001
                log.debug("Telegram poll: %s", e)
                time.sleep(5)

    def pop_commands(self):
        with self.lock:
            cmds, self.commands = self.commands, []
        return cmds
