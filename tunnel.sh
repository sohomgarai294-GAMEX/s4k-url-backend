#!/usr/bin/env bash
# Starts the API plus a public tunnel, then publishes the public URL into the repo
# so the web app can discover it automatically. Restarts the tunnel if it drops.
set -u
cd "$(dirname "$0")" || exit 1
LOG=/tmp/s4k-tunnel.log
: > "$LOG"

# ---- secrets (created once) ----
if [ ! -f .codespace.env ]; then
  { echo "COOKIE_SECRET=$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 43)"
    echo "ADMIN_KEY=$(head -c 18 /dev/urandom | base64 | tr -d '/+=' | head -c 18)"
    echo "REQUIRE_LOGIN=1"; } > .codespace.env
  chmod 600 .codespace.env
fi
set -a; . ./.codespace.env; set +a

publish () {
  local url="$1"
  echo "$url" > TUNNEL_URL.txt
  echo "$(date -u +%FT%TZ) $url" >> TUNNEL_HISTORY.txt
  # push with the workspace token so the web app can read it from raw.githubusercontent
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    git config user.email "codespace@users.noreply.github.com" >/dev/null 2>&1
    git config user.name  "S.4K auto" >/dev/null 2>&1
    git add TUNNEL_URL.txt TUNNEL_HISTORY.txt >/dev/null 2>&1
    git commit -q -m "tunnel url: $url" >/dev/null 2>&1
    git push -q origin HEAD:main >/dev/null 2>&1 && echo "[ok] published $url" || echo "[warn] push failed"
  fi
}

# ---- api ----
if ! curl -sf -o /dev/null http://127.0.0.1:8080/health; then
  nohup python3 -m uvicorn app:app --host 0.0.0.0 --port 8080 >> /tmp/s4k-api.log 2>&1 &
  for i in $(seq 1 40); do curl -sf -o /dev/null http://127.0.0.1:8080/health && break; sleep 2; done
fi

# ---- tunnel loop (renews itself if it drops) ----
while true; do
  ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes \
      -R 80:localhost:8080 nokey@localhost.run >> "$LOG" 2>&1 &
  SSH_PID=$!
  URL=""
  for i in $(seq 1 30); do
    URL=$(grep -oE 'https://[a-z0-9]+\.lhr\.life' "$LOG" | tail -1)
    [ -n "$URL" ] && break
    sleep 2
  done
  if [ -n "$URL" ]; then
    publish "$URL"
  else
    echo "[warn] no tunnel url this round"
  fi
  wait $SSH_PID
  echo "[info] tunnel dropped, restarting in 5s"
  sleep 5
  : > "$LOG"
done
