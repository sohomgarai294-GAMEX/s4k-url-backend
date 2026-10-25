#!/usr/bin/env bash
# Starts the API + a public tunnel, then publishes the public URL (and a boot log)
# into the repo via the GitHub API so the web app and the operator can find it.
set -u
cd "$(dirname "$0")" || exit 1
REPO="${S4K_REPO:-sohomgarai294-GAMEX/s4k-url-backend}"
TOKEN="${S4K_TOKEN:-}"
BOOT=/tmp/s4k-boot.log
TLOG=/tmp/s4k-tunnel.log
: > "$TLOG"

log() { echo "$(date -u +%T) $*" | tee -a "$BOOT"; }

publish() {   # publish <path> <local-file>
  local path="$1" file="$2" sha
  [ -z "$TOKEN" ] && { log "no token, cannot publish $path"; return; }
  sha=$(curl -s -H "Authorization: Bearer $TOKEN" \
        "https://api.github.com/repos/$REPO/contents/$path" \
        | python3 -c 'import sys,json
try:
  print(json.load(sys.stdin).get("sha",""))
except Exception:
  print("")' 2>/dev/null)
  python3 - "$path" "$file" "$sha" "$REPO" "$TOKEN" <<'PY'
import base64, json, sys, urllib.request
path, fname, sha, repo, token = sys.argv[1:6]
body = {"message": f"update {path}", "content": base64.b64encode(open(fname,'rb').read()).decode(), "branch": "main"}
if sha: body["sha"] = sha
req = urllib.request.Request(
    f"https://api.github.com/repos/{repo}/contents/{path}",
    data=json.dumps(body).encode(), method="PUT",
    headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
             "Content-Type": "application/json", "User-Agent": "s4k"})
try:
    urllib.request.urlopen(req, timeout=30)
    print(f"published {path}")
except Exception as e:
    print(f"publish {path} failed: {e}")
PY
}

log "boot: repo=$REPO python=$(python3 -V 2>&1) ssh=$(command -v ssh || echo none)"

# ---- secrets ----
if [ ! -f .codespace.env ]; then
  { echo "COOKIE_SECRET=$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 43)"
    echo "ADMIN_KEY=$(head -c 18 /dev/urandom | base64 | tr -d '/+=' | head -c 18)"
    echo "REQUIRE_LOGIN=1"; } > .codespace.env
  chmod 600 .codespace.env
fi
set -a; . ./.codespace.env; set +a
log "secrets ready"

# ---- api ----
if ! curl -sf -o /dev/null http://127.0.0.1:8080/health; then
  nohup python3 -m uvicorn app:app --host 0.0.0.0 --port 8080 >> /tmp/s4k-api.log 2>&1 &
  for i in $(seq 1 45); do curl -sf -o /dev/null http://127.0.0.1:8080/health && break; sleep 2; done
fi
curl -sf -o /dev/null http://127.0.0.1:8080/health && log "api up" || log "api DOWN"

# ---- tunnel ----
while true; do
  log "tunnel starting"
  ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -o BatchMode=yes \
      -R 80:localhost:8080 nokey@localhost.run >> "$TLOG" 2>&1 &
  SSH_PID=$!
  URL=""
  for i in $(seq 1 30); do
    URL=$(grep -oE 'https://[a-z0-9]+\.lhr\.life' "$TLOG" | tail -1)
    [ -n "$URL" ] && break
    sleep 2
  done
  if [ -n "$URL" ]; then
    log "tunnel url: $URL"
    echo "$URL" > /tmp/tunnel_url.txt
    publish TUNNEL_URL.txt /tmp/tunnel_url.txt
  else
    log "no url yet (first 20 log lines follow)"
    head -20 "$TLOG" >> "$BOOT"
  fi
  tail -40 "$TLOG" > /tmp/tunlog.txt; publish TUNNEL_LOG.txt /tmp/tunlog.txt
  wait $SSH_PID
  log "tunnel dropped, retry in 5s"
  sleep 5
  : > "$TLOG"
done
