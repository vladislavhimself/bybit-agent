"""Сторож на сервере с головой: раз в минуту проверяет здоровье и пишет в Telegram о технических проблемах,
о которых сам бот сообщить не может (он упал, завис, кончились деньги на модель, сервер атакуют, диск полон),
и шлёт «пульс» брокеру на Cloudflare — тот поднимет тревогу, если сервер замолчит целиком (выключили, не оплачен).
Запуск по systemd-таймеру: python scripts/watchdog.py  (переменные из /opt/bybit-agent/.env)"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bot.config import DATA, LOGS, active_market  # noqa: E402  (подгружает .env)

STATE = DATA / "watchdog.json"
TOKEN, CHAT = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
REPEAT_SEC = 6 * 3600          # одна и та же проблема напоминается не чаще раза в 6 часов
LOG_STALE_SEC = 15 * 60        # бот не писал в лог 15 минут = завис


def tg(text, html=False):
    if not TOKEN or not CHAT:
        return
    body = {"chat_id": CHAT, "text": text}
    if html:
        body["parse_mode"] = "HTML"
    try:
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json=body, timeout=15)
        if html and r.status_code != 200:
            requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": CHAT, "text": re.sub(r"</?b>", "", text)}, timeout=15)
    except requests.RequestException:
        pass


def active(unit):
    return subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True).stdout.strip() == "active"


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout


def nrestarts(unit):
    out = subprocess.run(["systemctl", "show", "-p", "NRestarts", "--value", unit], capture_output=True, text=True).stdout.strip()
    return int(out) if out.isdigit() else 0


def last_traceback():
    """Последняя строка ошибки из журнала службы — чтобы владелец видел причину, а не только факт падения."""
    out = sh("journalctl -u bybit-agent --since '-20min' --no-pager -o cat 2>/dev/null | grep -E '^[A-Za-z]+Error|^[A-Za-z]+Exception|Error:' | tail -1")
    return out.strip()[:200]


def checks(st):
    problems = {}   # key -> text
    market = active_market()
    # 08–09.11: бот падал на старте и systemd поднимал его каждые 34 с; сторож заставал его то живым, то мёртвым и всю ночь
    # писал «проблема»/«исправлено» (60 сообщений). Теперь рост счётчика перезапусков = отдельная проблема «падает по кругу»
    nr = nrestarts("bybit-agent")
    prev_nr = int(st.get("_nrestarts", nr))
    hist = [x for x in st.get("_nr_hist", []) if time.time() - x[0] < 3600] + [(time.time(), nr)]
    st["_nr_hist"], st["_nrestarts"] = hist, nr
    per_hour = nr - hist[0][1] if len(hist) > 1 else 0
    if nr > prev_nr or per_hour >= 3:
        tb = last_traceback()
        problems["bot_crashloop"] = (f"Бот падает и перезапускается: {per_hour} перезапусков за час (всего {nr}). Сделок нет."
                                     + (f"\nОшибка: {tb}" if tb else ""))
    elif not active("bybit-agent"):
        problems["bot_down"] = "Служба бота не запущена (bybit-agent). Сделок не будет, позиции под стопами брокера."
    log = LOGS / "bot.log"
    if log.exists() and time.time() - log.stat().st_mtime > LOG_STALE_SEC and "bot_crashloop" not in problems:
        problems["bot_stale"] = f"Бот не пишет в лог уже {int((time.time() - log.stat().st_mtime) / 60)} мин: завис или без сети."
    if market == "moex":
        if not active("relay-bridge"):
            problems["bridge_down"] = "Мост к ретранслятору (relay-bridge) не запущен: нет связи с Мосбиржей."
        else:
            try:
                st = requests.get("http://127.0.0.1:8082/", timeout=5).json()
                if not st.get("connected"):
                    problems["broker_off"] = "Голова не подключена к брокеру Cloudflare: нет связи с Мосбиржей."
            except Exception:  # noqa: BLE001
                problems["bridge_dead"] = "Мост к ретранслятору не отвечает."
        key, ws = os.getenv("RELAY_KEY", ""), os.getenv("RELAY_WS", "")
        if key and ws:
            try:
                r = requests.get(ws.replace("wss://", "https://").replace("/ws", "/status"), headers={"X-Relay-Key": key}, timeout=15).json()
                if not r.get("relays"):
                    problems["relay_off"] = "Московский ретранслятор не подключён к брокеру: котировок и ордеров Мосбиржи нет."
            except Exception:  # noqa: BLE001
                problems["cf_off"] = "Брокер Cloudflare недоступен с сервера."
    # деньги на модель / ключи: по свежим строкам лога
    if log.exists():
        tail = sh(f"tail -n 400 '{log}'")
        # только настоящие ошибки оплаты API, а не любое «402» (ловилось «задержка 402с» в строке про новость)
        if re.search(r"credit balance is too low|insufficient_quota|billing_error|Your credit balance|HTTP/?\S* 402\b.*anthropic|anthropic.*\b402\b", tail, re.I):
            problems["llm_money"] = "Anthropic отвечает ошибкой оплаты: закончились деньги на API. Новости не анализируются."
        if re.search(r"authentication_error|invalid x-api-key|401 .*anthropic", tail, re.I):
            problems["llm_key"] = "Ключ Anthropic не принимается (401). Проверь ключ."
        if re.search(r"tbank\.ru.*(401|403|UNAUTHENTICATED|PERMISSION_DENIED)", tail, re.I):
            problems["tinvest_key"] = "T-Invest отвергает токен (401/403): токен истёк или отозван."
        if re.search(r"Bybit.*(10003|10004|33004)", tail):
            problems["bybit_key"] = "Bybit отвергает API-ключ (истёк или неверный)."
    # ресурсы
    du = shutil.disk_usage("/")
    if du.free / du.total < 0.08:
        problems["disk"] = f"Диск почти полон: свободно {du.free / 2**30:.1f} ГБ."
    mem = sh("free -m | awk 'NR==2{print $7}'").strip()
    if mem.isdigit() and int(mem) < 120:
        problems["mem"] = f"Мало свободной памяти: {mem} МБ. Бот может быть убит системой."
    # безопасность: подбор пароля по SSH и чужие входы
    fails = sh("journalctl -u ssh --since '-10min' --no-pager -o cat 2>/dev/null | grep -c -E 'Failed password|Invalid user'").strip()
    if fails.isdigit() and int(fails) > 50:
        problems["ssh_bruteforce"] = f"За 10 минут {fails} неудачных попыток входа по SSH: подбирают пароль (вход по паролю выключен, ключ в безопасности)."
    logins = sh("journalctl -u ssh --since '-10min' --no-pager -o cat 2>/dev/null | grep -E 'Accepted (password|publickey)' | grep -v -c 'bybit-agent-claude'").strip()
    accepted = sh("journalctl -u ssh --since '-10min' --no-pager -o cat 2>/dev/null | grep -E 'Accepted ' | tail -3")
    if accepted and "Accepted password" in accepted:
        problems["ssh_password_login"] = f"Кто-то вошёл по SSH ПАРОЛЕМ (не ключом):\n{accepted.strip()[:300]}"
    return problems


# сколько проверок подряд (раз в минуту) проблема должна держаться, прежде чем писать: перезапуск бота при обновлении
# занимает 3 секунды, переподключение к брокеру 1–3 секунды — это не проблемы
CONFIRM = {"bot_down": 3, "bot_stale": 2, "bridge_down": 2, "broker_off": 3, "bridge_dead": 2, "relay_off": 3, "cf_off": 3, "bot_crashloop": 2}
CLEAR_CHECKS = 5   # «исправлено» — только когда проблема не видна 5 проверок подряд (5 минут), иначе качели при перезапусках по кругу
TITLES = {"bot_down": "бот не запущен", "bot_stale": "бот завис", "bot_crashloop": "бот падает и перезапускается по кругу",
          "bridge_down": "мост к ретранслятору", "broker_off": "связь с брокером Cloudflare",
          "bridge_dead": "мост не отвечает", "relay_off": "московский ретранслятор", "cf_off": "брокер Cloudflare", "llm_money": "деньги на модели",
          "llm_key": "ключ Anthropic", "tinvest_key": "токен T-Invest", "bybit_key": "ключ Bybit", "disk": "диск", "mem": "память",
          "ssh_bruteforce": "подбор пароля SSH", "ssh_password_login": "вход по паролю SSH"}
ADVICE = {"bot_down": "Зайти на сервер и запустить: systemctl restart bybit-agent. Позиции под стопами брокера.",
          "bot_crashloop": "Перезапуск не поможет: нужно смотреть ошибку в журнале (journalctl -u bybit-agent) и чинить код или данные. Позиции под стопами брокера.",
          "bot_stale": "Перезапустить службу: systemctl restart bybit-agent.", "llm_money": "Пополнить баланс Anthropic.",
          "llm_key": "Проверить ANTHROPIC_API_KEY в .env.", "tinvest_key": "Выпустить новый токен T-Invest и записать в .env.",
          "relay_off": "Проверить московский сервер: systemctl status relay-agent.", "disk": "Почистить logs/ и старые архивы."}


def main():
    st = json.loads(STATE.read_text()) if STATE.exists() else {}
    cnt = st.get("_cnt", {})          # сколько проверок подряд проблема видна
    clear = st.get("_clear", {})      # сколько проверок подряд проблема НЕ видна (после того как о ней написали)
    alerted = st.get("_alert", {})    # key -> время последнего сообщения о проблеме
    opened = st.get("_open", {})      # key -> когда проблема впервые подтвердилась
    problems = checks(st)
    now = time.time()
    for key in list(cnt):
        if key not in problems:
            cnt.pop(key)
    for key, text in problems.items():
        cnt[key] = cnt.get(key, 0) + 1
        clear.pop(key, None)
        if cnt[key] < CONFIRM.get(key, 1):
            continue
        opened.setdefault(key, now)
        # одно сообщение на проблему, напоминание не чаще раза в 6 часов — и после «исправлено» тоже (качели не рассылаем)
        if now - alerted.get(key, 0) > REPEAT_SEC:
            parts = ["🔧 <b>ТЕХНИЧЕСКАЯ ПРОБЛЕМА</b>: " + TITLES.get(key, key),
                     "<b>Что случилось</b>\n" + text.replace("<", "&lt;"),
                     "<b>Проверено</b>\n" + f"{cnt[key]} раз подряд с интервалом в минуту"]
            if ADVICE.get(key):
                parts.append("<b>Что делать</b>\n" + ADVICE[key])
            tg("\n\n".join(parts), html=True)
            alerted[key] = now
    for key in list(opened):
        if key in problems:
            continue
        clear[key] = clear.get(key, 0) + 1
        if clear[key] >= CLEAR_CHECKS:
            mins = int((now - opened[key]) / 60)
            tg("✅ <b>ИСПРАВЛЕНО</b>: " + TITLES.get(key, key) + f"\n\n<b>Что было</b>\n{TITLES.get(key, key)}, длилось {mins} мин; "
               f"сейчас {CLEAR_CHECKS} проверок подряд в норме", html=True)
            opened.pop(key)
            clear.pop(key, None)
    for key in list(alerted):   # через сутки без проблемы — забываем, чтобы новое событие сообщалось сразу
        if key not in problems and now - alerted[key] > 24 * 3600:
            alerted.pop(key)
    st["_cnt"], st["_clear"], st["_alert"], st["_open"] = cnt, clear, alerted, opened
    for key in list(st):   # записи старого формата (key -> ts) больше не нужны
        if not key.startswith("_"):
            st.pop(key)
    STATE.write_text(json.dumps(st))
    # пульс брокеру: если сервер замолчит целиком, брокер сам напишет в Telegram
    key, ws = os.getenv("RELAY_KEY", ""), os.getenv("RELAY_WS", "")
    if key and ws:
        try:
            requests.post(ws.replace("wss://", "https://").replace("/ws", "/heartbeat"), headers={"X-Relay-Key": key},
                          json={"problems": list(problems), "market": active_market()}, timeout=15)
        except requests.RequestException:
            pass


if __name__ == "__main__":
    main()
