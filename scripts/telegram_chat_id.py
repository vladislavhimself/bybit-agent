"""Узнать chat_id владельца: напишите боту /start, затем запустите этот скрипт. Он впишет TELEGRAM_CHAT_ID в .env."""
import os
import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bot.config import Settings  # noqa: E402

s = Settings()
tok = s.telegram_token
if not tok:
    raise SystemExit("TELEGRAM_BOT_TOKEN не задан в .env")
r = requests.get(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=15).json()
chats = {}
for u in r.get("result", []):
    m = u.get("message") or u.get("edited_message") or {}
    c = m.get("chat") or {}
    if c.get("id"):
        chats[c["id"]] = f"{c.get('first_name', '')} {c.get('last_name', '')} @{c.get('username', '')}".strip()
if not chats:
    raise SystemExit("Сообщений боту нет. Откройте бота в Telegram, нажмите Start (или напишите /start) и запустите снова.")
for cid, name in chats.items():
    print(f"chat_id={cid}  {name}")
cid = str(list(chats)[0]) if len(chats) == 1 else input("Введите chat_id из списка: ").strip()
env = ROOT / ".env"
text = env.read_text(encoding="utf-8") if env.exists() else ""
if re.search(r"^TELEGRAM_CHAT_ID=.*$", text, re.M):
    text = re.sub(r"^TELEGRAM_CHAT_ID=.*$", f"TELEGRAM_CHAT_ID={cid}", text, flags=re.M)
else:
    text += f"\nTELEGRAM_CHAT_ID={cid}\n"
env.write_text(text, encoding="utf-8")
requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={"chat_id": cid, "text": "✅ Бот привязан к этому чату"},
              timeout=15)
print(f"Записано TELEGRAM_CHAT_ID={cid} в .env, тестовое сообщение отправлено")
