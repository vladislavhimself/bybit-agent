"""Одноразовый вход в Telegram для живого чтения каналов (Telethon). Запускается на компьютере владельца:
    pip install telethon
    python scripts/telegram_login.py
Спросит api_id и api_hash (получить на https://my.telegram.org -> API development tools), номер телефона и код
из Telegram. Напечатает строку TELEGRAM_SESSION — её нужно положить в .env на сервере вместе с
TELEGRAM_API_ID и TELEGRAM_API_HASH. Пароль и код никуда не отправляются, кроме серверов Telegram."""
import sys

from telethon.sessions import StringSession
from telethon.sync import TelegramClient

api_id = int(input("api_id: ").strip())
api_hash = input("api_hash: ").strip()
with TelegramClient(StringSession(), api_id, api_hash) as client:
    me = client.get_me()
    print(f"\nВход выполнен: {me.first_name} (@{me.username})")
    print("\nДобавьте в .env на сервере три строки:\n")
    print(f"TELEGRAM_API_ID={api_id}")
    print(f"TELEGRAM_API_HASH={api_hash}")
    print(f"TELEGRAM_SESSION={client.session.save()}")
    sys.stdout.flush()
