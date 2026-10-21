#!/usr/bin/env bash
# Cloudflare Quick Tunnel до локального ретранслятора + публикация его адреса в приватный репозиторий
# relay-address (голова читает оттуда). Адрес trycloudflare.com меняется при каждом перезапуске,
# поэтому скрипт следит за выводом cloudflared и при каждом новом адресе делает git push.
set -u
PORT="${1:-8081}"
REPO_DIR="${REPO_DIR:-/opt/relay-address}"
export GIT_SSH_COMMAND="ssh -i /root/.ssh/relay_deploy -o StrictHostKeyChecking=accept-new"

publish() {
  local url="$1"
  cd "$REPO_DIR" || return
  git pull -q --rebase origin main 2>/dev/null || git pull -q --rebase origin master 2>/dev/null
  if [ "$(cat url.txt 2>/dev/null)" != "$url" ]; then
    echo "$url" > url.txt
    git -c user.email=relay@moscow -c user.name=relay commit -q -am "relay url $(date -u +%FT%TZ)" && git push -q origin HEAD \
      && echo "published $url" || echo "publish failed"
  fi
}

while true; do
  cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:${PORT}" 2>&1 | while IFS= read -r line; do
    echo "$line"
    if [[ "$line" =~ (https://[a-z0-9-]+\.trycloudflare\.com) ]]; then
      publish "${BASH_REMATCH[1]}"
    fi
  done
  echo "cloudflared exited, restart in 5s"
  sleep 5
done
