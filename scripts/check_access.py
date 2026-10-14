"""Проверка доступа с этой машины: Bybit (mainnet/demo/testnet), Anthropic API, Telegram. Запуск: python scripts/check_access.py"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import requests  # noqa: E402

from bot.config import Settings  # noqa: E402

s = Settings()
proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy")
print(f"Режим: {s.mode}; прокси: {proxy or 'нет'}")


def ping(name, url):
    t = time.time()
    try:
        r = requests.get(url, timeout=10)
        print(f"[{'OK' if r.status_code < 500 else 'FAIL'}] {name}: HTTP {r.status_code}, {time.time() - t:.2f}s")
        return r.status_code < 500
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {name}: {e}")
        return False


ping("Bybit mainnet (рыночные данные)", "https://api.bybit.com/v5/market/time")
ping("Bybit demo (торговля на виртуальном балансе)", "https://api-demo.bybit.com/v5/market/time")
ping("Bybit testnet", "https://api-testnet.bybit.com/v5/market/time")
ping("Anthropic API", "https://api.anthropic.com/v1/models")
ping("Telegram API", "https://api.telegram.org")
ping("RSS CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/")

if s.bybit_key:
    try:
        from bot.exchange import Exchange
        ex = Exchange(s)
        eq, avail = ex.equity()
        print(f"[OK] Bybit ключ работает: equity {eq:.2f} USDT, доступно {avail:.2f}")
        print(f"     открытых позиций: {len(ex.positions())}")
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] Bybit ключ: {e}")
else:
    print("[--] BYBIT_API_KEY не задан")

if s.has_anthropic:
    try:
        import anthropic
        c = anthropic.Anthropic(max_retries=0, timeout=30)
        r = c.messages.create(model=s.get("llm.cheap_model"), max_tokens=20,
                              messages=[{"role": "user", "content": "Ответь одним словом: работает?"}])
        print(f"[OK] Anthropic ключ работает: {r.content[0].text.strip()[:40]}")
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] Anthropic ключ: {e}")
else:
    print("[--] ANTHROPIC_API_KEY не задан")

if s.telegram_token:
    try:
        r = requests.get(f"https://api.telegram.org/bot{s.telegram_token}/getMe", timeout=10).json()
        print(f"[OK] Telegram бот: @{r['result']['username']}" if r.get("ok") else f"[FAIL] Telegram: {r}")
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] Telegram: {e}")
else:
    print("[--] TELEGRAM_BOT_TOKEN не задан")
