# S.4K URL Import Backend

The Python service that makes **Import from URL** work in the S.4K Studio web app.
The browser cannot extract video from YouTube/Instagram/etc.; this service runs
`yt-dlp` on a real machine and hands the file to the app.

## What it does

- `POST /api/info` — inspect a URL (title, duration, formats)
- `POST /api/download` — fetch the media
- `POST /api/login`, `/api/me`, `/api/logout` — simple per-device auth
- `POST /api/me/cookies` — **each user connects their own YouTube account**
  (encrypted at rest, never returned by the API)
- `GET /health` — liveness + detected yt-dlp version

## Run it locally

```bash
pip install -r requirements.txt
export COOKIE_SECRET="$(head -c 32 /dev/urandom | base64)"
export ADMIN_KEY="your-private-admin-key"
uvicorn app:app --host 0.0.0.0 --port 8080
```

Open <http://localhost:8080/health> — you should see JSON.

## Run it in Codespaces (no extra account)

1. Code button → **Codespaces** tab → **Create codespace on main**
2. Wait for setup (installs ffmpeg + pip packages)
3. In the terminal:

```bash
export COOKIE_SECRET="$(head -c 32 /dev/urandom | base64)"
export ADMIN_KEY="myadmin"
uvicorn app:app --host 0.0.0.0 --port 8080
```

4. Ports tab → right-click port 8080 → **Port Visibility → Public**
5. Copy the forwarded URL, paste it in the web app: **Import URL → Backend connection**

## Environment variables

| Name | Required | What it does |
|---|---|---|
| `COOKIE_SECRET` | yes (prod) | Encrypts per-user YouTube cookies. **Never change it** or stored cookies become unreadable. |
| `ADMIN_KEY` | yes | Protects the shared/global cookie endpoints. |
| `REQUIRE_LOGIN` | no (`1` to enable) | Forces the name+token login. |
| `DEFAULT_DAILY_DOWNLOADS` | no (default `15`) | Per-user daily quota. |
| `DB_PATH` | no | SQLite path. Default `s4k_users.db`. |
| `TURNSTILE_SECRET` / `TURNSTILE_SITE_KEY` | no | Cloudflare Turnstile on login. |
| `PROXY_URL` | no | Outbound proxy for yt-dlp. |

## Honest limits

- DRM-protected services (Netflix, Prime Video, Hotstar, Spotify) **cannot** be
  served. This is not a bug and not fixable.
- YouTube requires the user's own cookies. Without them YouTube returns
  *"Sign in to confirm you're not a bot"*.
- Cookies expire every ~2 weeks to 3 months; each user reconnects their own.
- Never commit `s4k_users.db` or `cookies.txt`.

## Licence / use

Only download media you own or are authorised to use.
