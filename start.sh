#!/usr/bin/env bash
# S.4K URL Import backend — one-command start (Codespaces / Linux / Termux)
set -u

cd "$(dirname "$0")" || exit 1
ENVFILE=".codespace.env"

say() { printf '\n\033[1;32m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }

# 1. ffmpeg (needed so 1080p+ video+audio can be merged)
if ! command -v ffmpeg >/dev/null 2>&1; then
  warn "ffmpeg missing — installing…"
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -qq && sudo apt-get install -y -qq ffmpeg
  elif command -v pkg >/dev/null 2>&1; then
    pkg install -y ffmpeg
  fi
fi

# 2. python deps
if ! python3 -c "import fastapi, uvicorn, yt_dlp" >/dev/null 2>&1; then
  warn "Python packages missing — installing (1–2 min)…"
  python3 -m pip install -q --upgrade pip
  python3 -m pip install -q -r requirements.txt
fi

# 3. secrets — created once, reused on every restart
if [ ! -f "$ENVFILE" ]; then
  {
    echo "COOKIE_SECRET=$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 43)"
    echo "ADMIN_KEY=$(head -c 18 /dev/urandom | base64 | tr -d '/+=' | head -c 18)"
    echo "REQUIRE_LOGIN=1"
  } > "$ENVFILE"
  chmod 600 "$ENVFILE"
fi
set -a; . "./$ENVFILE"; set +a

say "S.4K URL Import backend starting…"
echo "    yt-dlp  : $(python3 -m yt_dlp --version 2>/dev/null || echo '?')"
echo "    ffmpeg  : $(command -v ffmpeg || echo 'not found (merging disabled)')"
echo "    ADMIN_KEY: $ADMIN_KEY   <-- keep this private"
echo
say "Now make port 8080 PUBLIC (Ports tab -> right-click 8080 -> Port Visibility -> Public)"
echo "Then copy the https://…-8080.app.github.dev address and paste it in the app."
say "Press Ctrl+C to stop."
echo

exec python3 -m uvicorn app:app --host 0.0.0.0 --port 8080
