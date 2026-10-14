#!/usr/bin/env bash
# Выкладка проекта на сервер и перезапуск службы.
# Использование: bash scripts/deploy.sh [путь_к_ssh_ключу]
# Секреты (.env), база и логи на сервере не трогаются.
set -e
HOST="root@177.3.217.105"
KEY="${1:-$HOME/.ssh/bybit_agent_ed25519}"
O="-o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -o LogLevel=ERROR"
cd "$(dirname "$0")/.."
tar --exclude='.git' --exclude='data/history' --exclude='data/*.db' --exclude='data/*.db-journal' --exclude='logs' \
    --exclude='__pycache__' --exclude='data/backtest_trades.csv' --exclude='.env' --exclude='.venv' \
    -czf /tmp/bybit-agent.tgz .
scp -i "$KEY" $O /tmp/bybit-agent.tgz "$HOST:/opt/bybit-agent/"
ssh -i "$KEY" $O "$HOST" 'cd /opt/bybit-agent && tar -xzf bybit-agent.tgz && rm bybit-agent.tgz \
  && .venv/bin/pip install -q -r requirements.txt && systemctl restart bybit-agent && sleep 3 \
  && systemctl --no-pager status bybit-agent | head -5'
echo "Выложено. Логи: ssh -i $KEY $HOST journalctl -u bybit-agent -f"
