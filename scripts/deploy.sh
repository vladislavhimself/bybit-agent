#!/usr/bin/env bash
# Выкладка проекта на сервер и перезапуск службы.
# Использование: bash scripts/deploy.sh [путь_к_ssh_ключу]
# Секреты (.env), база и логи на сервере не трогаются.
set -e
HOST="root@177.3.217.105"
KEY="${1:-$HOME/.ssh/bybit_agent_ed25519}"
O="-o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -o LogLevel=ERROR"
cd "$(dirname "$0")/.."
# Перед выкладкой — сценарные тесты на эмуляторе брокера (без сети и без вызовов модели). Не прошли — не выкладываем.
for t in tests/test_*.py; do
  python "$t" > /tmp/bot-test.log 2>&1 || { echo "ТЕСТ НЕ ПРОШЁЛ: $t"; tail -15 /tmp/bot-test.log; exit 1; }
done
echo "Тесты пройдены: $(ls tests/test_*.py | wc -l) файлов"
tar --exclude='.git' --exclude='data/history' --exclude='data/*.db' --exclude='data/*.db-journal' --exclude='logs' \
    --exclude='__pycache__' --exclude='data/backtest_trades.csv' --exclude='.env' --exclude='.venv' \
    -czf /tmp/bybit-agent.tgz .
scp -i "$KEY" $O /tmp/bybit-agent.tgz "$HOST:/opt/bybit-agent/"
# Сообщение коммита режем по символам через python: `cut -c` считает байты и 08.11 разрезал кириллицу пополам — файл версии
# стал невалидным UTF-8, бот падал на старте 20 часов подряд
VER="$(git rev-parse --short HEAD 2>/dev/null) $(git log -1 --pretty=%s 2>/dev/null | python -c 'import sys; print(sys.stdin.read().strip()[:160])')"
ssh -i "$KEY" $O "$HOST" "mkdir -p /opt/bybit-agent/data && printf '%s' \"$VER\" > /opt/bybit-agent/data/version.txt && printf 'обновление кода' > /opt/bybit-agent/data/restart_reason.txt"
ssh -i "$KEY" $O "$HOST" 'cd /opt/bybit-agent && tar -xzf bybit-agent.tgz && rm bybit-agent.tgz \
  && .venv/bin/pip install -q -r requirements.txt && systemctl reset-failed bybit-agent; systemctl restart bybit-agent && sleep 3 \
  && systemctl --no-pager status bybit-agent | head -5'
# Проверка живучести: через 30 секунд процесс должен быть тем же и без Traceback (3 секунды «active» ничего не доказывают — 08.11 бот
# падал на 2-й секунде и перезапускался systemd каждые 34 секунды)
sleep 30
ssh -i "$KEY" $O "$HOST" 'if systemctl is-active --quiet bybit-agent && [ "$(journalctl -u bybit-agent --since "45 sec ago" --no-pager | grep -c Traceback)" = "0" ] \
   && [ "$(systemctl show -p NRestarts --value bybit-agent)" = "0" ]; then echo "ПРОВЕРКА: бот жив, перезапусков 0, ошибок старта нет"; \
   else echo "ПРОВЕРКА НЕ ПРОЙДЕНА: смотри журнал"; journalctl -u bybit-agent --since "60 sec ago" --no-pager | grep -v "news: tg" | tail -20; exit 2; fi'
echo "Выложено. Логи: ssh -i $KEY $HOST journalctl -u bybit-agent -f"
