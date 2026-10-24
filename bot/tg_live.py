"""Новости из Telegram в реальном времени: подписка на каналы через MTProto (Telethon) от имени аккаунта
пользователя. Сообщение приходит за доли секунды после публикации — вместо опроса веб-превью раз в 20 секунд.
Нужны TELEGRAM_API_ID, TELEGRAM_API_HASH (my.telegram.org) и TELEGRAM_SESSION (scripts/telegram_login.py).
Элементы кладутся в ту же очередь, что и остальные источники (bot.raw_q), источник "tg:<канал>"."""
import asyncio
import hashlib
import logging
import os
import re
import threading
import time

log = logging.getLogger("tg_live")


class TelegramLive:
    def __init__(self, channels, raw_q, wake, settings=None):
        self.channels = [c.lstrip("@") for c in channels]
        self.raw_q, self.wake = raw_q, wake
        self.api_id = os.getenv("TELEGRAM_API_ID", "").strip()
        self.api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
        self.session = os.getenv("TELEGRAM_SESSION", "").strip()
        self.enabled = bool(self.api_id and self.api_hash and self.session)
        self.connected = False
        self.received = 0

    def start(self):
        if not self.enabled:
            log.info("Telegram live выключен: нет TELEGRAM_API_ID/HASH/SESSION, остаётся опрос веб-превью")
            return False
        threading.Thread(target=self._run, daemon=True, name="tg-live").start()
        return True

    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        backoff = 5
        while True:
            try:
                loop.run_until_complete(self._main())
            except Exception as e:  # noqa: BLE001
                log.warning("Telegram live: %s; переподключение через %ds", str(e)[:150], backoff)
            self.connected = False
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)

    async def _main(self):
        from telethon import TelegramClient, events
        from telethon.sessions import StringSession
        client = TelegramClient(StringSession(self.session), int(self.api_id), self.api_hash)
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("сессия Telegram не авторизована: перезапустите scripts/telegram_login.py")
        entities = []
        for ch in self.channels:
            try:
                entities.append(await client.get_entity(ch))
            except Exception as e:  # noqa: BLE001
                log.warning("канал %s: %s", ch, str(e)[:80])
        names = {e.id: ch for e, ch in zip(entities, [c for c in self.channels if True][:len(entities)])}

        @client.on(events.NewMessage(chats=entities))
        async def handler(event):
            text = (event.message.message or "").strip()
            if not text:
                return
            chat = await event.get_chat()
            ch = getattr(chat, "username", None) or names.get(event.chat_id, str(event.chat_id))
            title = re.sub(r"\s+", " ", text.split("\n")[0])[:200]
            item = {"id": hashlib.sha1(f"{ch}/{event.message.id}".encode()).hexdigest()[:16], "source": f"tg:{ch}",
                    "title": title, "summary": text[:2000], "url": f"https://t.me/{ch}/{event.message.id}",
                    "published_ts": int(event.message.date.timestamp())}
            self.raw_q.put(item)
            self.received += 1
            self.wake.set()   # разбудить поток новостей сразу, не ждать таймера опроса

        self.connected = True
        log.info("Telegram live: подключён, каналов %d", len(entities))
        await client.run_until_disconnected()
