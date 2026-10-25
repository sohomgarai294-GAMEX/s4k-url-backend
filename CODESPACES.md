# Run the backend in Codespaces (no new account, free)

GitHub gives every account **60 free Codespaces hours per month**. That is enough to
run this API while you work. No credit card, no new signup — your GitHub account is all.

## Steps

### 1. Open Codespaces
On the repo page: green **Code** button → **Codespaces** tab → **Create codespace on main**.

First boot takes ~2 minutes. It automatically installs `ffmpeg` and the Python packages.

### 2. Start the API
In the terminal at the bottom, paste these two lines:

```bash
export COOKIE_SECRET="$(head -c 32 /dev/urandom | base64)"
export ADMIN_KEY="myadmin"
uvicorn app:app --host 0.0.0.0 --port 8080
```

You should see `Uvicorn running on http://0.0.0.0:8080`.

> Keep this terminal tab open. If you close the codespace the server stops,
> and the URL dies with it.

### 3. Make the port public
Bottom panel → **Ports** tab → find `8080` → right-click (or long-press) →
**Port Visibility** → **Public**.

That last click is what lets your phone reach it.

### 4. Copy the URL
Same row, under **Forwarded Address**, you get something like:

```
https://abc123-8080.app.github.dev
```

Paste that into the web app:
**Import URL → BACKEND CONNECTION → Backend URL → Save backend → Test connection**
You should see a green **connected**.

### 5. Connect your own YouTube account
In the same dialog, **Your YouTube account** → **Choose cookies.txt** → **Connect my account**.
See `YOUTUBE-COOKIES-SETUP.md` in the S.4K package for how to export that file.

## Honest limits

| Thing | Reality |
|---|---|
| Free hours | 60 h/month. Idle codespaces auto-stop after 30 min. |
| URL stability | The URL changes when the codespace is rebuilt. Re-paste after that. |
| Always-on | Not possible. This is for when *you* are downloading. |
| Other users | They would need their own codespace. For a public service you need real hosting. |
| Cookies | Stored in the codespace's SQLite. Rebuilding the codespace wipes them — reconnect. |

## Alternative: your own phone (Termux)

```bash
pkg install python ffmpeg
pip install -r requirements.txt
export COOKIE_SECRET="$(head -c 32 /dev/urandom | base64)"
export ADMIN_KEY="myadmin"
uvicorn app:app --host 0.0.0.0 --port 8080
```

Then the app must point at your phone's LAN IP (`http://192.168.x.x:8080`) — only
reachable from the same Wi-Fi. No GitHub account needed beyond the repo itself.
