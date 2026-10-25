"""
S.4K URL Import backend — v2.0

WHAT THE LOGIN/TOKEN LAYER ACTUALLY DOES
----------------------------------------
It does NOT make YouTube work. YouTube blocks the *server's IP*, not the user.
I verified: the same request with a valid Bearer token returns the same block.

What it DOES do, and why it is still worth having:
  * stops anonymous randoms burning your bandwidth / Render quota
  * per-user daily download quota + byte quota
  * optional Cloudflare Turnstile on sign-in (free, blocks scripted abuse)
  * tokens live in SQLite; deleting the token on the device = user is logged out
  * an admin key so you can create users and raise their quotas

WHAT ACTUALLY FIXES YOUTUBE
  COOKIES_FILE=/app/cookies.txt   (cookies exported from a logged-in browser)
  + optionally a residential proxy via PROXY_URL
Nothing else changes YouTube's answer — not login, not tokens, not Cloudflare.

Endpoints
  GET  /health                          public status
  POST /api/login    {name, turnstile_token?}   -> {token, name, quota}
  GET  /api/me                          token required
  POST /api/logout                      token required (revokes it)
  POST /api/info     {url}              token required
  GET  /api/download                    token required
  GET  /api/admin/users                 admin key
  POST /api/admin/quota {name, daily}   admin key
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

import yt_dlp

try:
    from cryptography.fernet import Fernet, InvalidToken
    _HAVE_CRYPTO = True
except Exception:                                    # pragma: no cover
    _HAVE_CRYPTO = False

APP_VERSION = "2.1.0"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# ---------------------------------------------------------------- config ----
_orig = os.environ.get("ALLOWED_ORIGINS", "*")
ALLOWED_ORIGINS = ["*"] if _orig.strip() == "*" else [o.strip() for o in _orig.split(",") if o.strip()]

MAX_DOWNLOAD_SECONDS = int(os.environ.get("MAX_DOWNLOAD_SECONDS", "1800"))
COOKIES_FILE   = os.environ.get("COOKIES_FILE", "cookies.txt").strip()  # fixes YouTube
PROXY_URL      = os.environ.get("PROXY_URL", "").strip()          # residential proxy
PROXY_TIMEOUT  = float(os.environ.get("PROXY_TIMEOUT", "60"))
DB_PATH        = os.environ.get("DB_PATH", "s4k_users.db")
ADMIN_KEY      = os.environ.get("ADMIN_KEY", "").strip()
TURNSTILE_SECRET = os.environ.get("TURNSTILE_SECRET", "").strip()
TURNSTILE_SITEKEY = os.environ.get("TURNSTILE_SITE_KEY", "").strip()

COOKIE_SECRET  = os.environ.get("COOKIE_SECRET", "").strip()  # encrypts per-user cookies
DEFAULT_DAILY   = int(os.environ.get("DEFAULT_DAILY_DOWNLOADS", "15"))
DEFAULT_BYTES   = int(os.environ.get("DEFAULT_DAILY_BYTES", str(2 * 1024**3)))  # 2 GB
REQUIRE_LOGIN   = os.environ.get("REQUIRE_LOGIN", "1") != "0"

HAS_FFMPEG = bool(shutil.which("ffmpeg"))

MEDIA_EXT = re.compile(
    r"\.(mp4|m4v|webm|mkv|mov|avi|flv|wmv|ts|m3u8|mp3|m4a|aac|wav|ogg|oga|opus|flac)(\?|#|$)",
    re.I,
)

app = FastAPI(title="S.4K URL Import", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"], allow_headers=["*"],
    expose_headers=["Content-Disposition", "Content-Length", "X-Mode", "X-Quota-Left"],
)


# ------------------------------------------------------------------ db ------
def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _fernet():
    """Key for encrypting each user's own cookies at rest."""
    if not _HAVE_CRYPTO:
        return None
    seed = COOKIE_SECRET or ADMIN_KEY or "s4k-insecure-default-change-me"
    key = base64.urlsafe_b64encode(hashlib.sha256(seed.encode()).digest())
    return Fernet(key)


def _enc(text: str) -> str:
    f = _fernet()
    if f is None:                                    # no crypto lib -> plain marker
        return "PLAIN:" + base64.b64encode(text.encode()).decode()
    return "ENC:" + f.encrypt(text.encode()).decode()


def _dec(blob):
    if not blob:
        return None
    if blob.startswith("ENC:"):
        f = _fernet()
        if f is None:
            return None
        try:
            return f.decrypt(blob[4:].encode()).decode()
        except (InvalidToken, Exception):
            return None
    if blob.startswith("PLAIN:"):
        return base64.b64decode(blob[6:]).decode()
    return None


def init_db():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            token TEXT UNIQUE NOT NULL,
            created REAL NOT NULL,
            day TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            bytes_today INTEGER NOT NULL DEFAULT 0,
            daily_limit INTEGER NOT NULL,
            byte_limit INTEGER NOT NULL,
            blocked INTEGER NOT NULL DEFAULT 0,
            cookies_enc TEXT
        )""")
        # migrate older databases
        cols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        if "cookies_enc" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN cookies_enc TEXT")


init_db()


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _get_user(token: str):
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE token=?", (token,)).fetchone()
    return row


def require_user(request: Request):
    if not REQUIRE_LOGIN:
        return None
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, detail={
            "code": "NO_TOKEN", "message": "Sign in required.",
            "hint": "Open the app and sign in with your name."})
    token = auth.split(None, 1)[1].strip()
    row = _get_user(token)
    if not row:
        raise HTTPException(401, detail={
            "code": "BAD_TOKEN", "message": "This session is not valid.",
            "hint": "Sign in again — the token may have been deleted or revoked."})
    if row["blocked"]:
        raise HTTPException(403, detail={
            "code": "BLOCKED", "message": "This account is blocked.",
            "hint": "Contact the operator."})
    if row["day"] != _today():
        with db() as c:
            c.execute("UPDATE users SET day=?, used=0, bytes_today=0 WHERE id=?",
                      (_today(), row["id"]))
        row = _get_user(token)
    return row


def charge(row, nbytes: int = 0):
    """Check + consume quota. Raises if exhausted."""
    if row is None:
        return
    if row["used"] >= row["daily_limit"]:
        raise HTTPException(429, detail={
            "code": "QUOTA_EXCEEDED",
            "message": f"Daily limit reached ({row['daily_limit']} downloads).",
            "hint": "Try again tomorrow, or ask the operator to raise your limit."})
    if nbytes and row["bytes_today"] + nbytes > row["byte_limit"]:
        raise HTTPException(429, detail={
            "code": "BYTE_QUOTA",
            "message": "Daily data limit reached.",
            "hint": f"Limit is {row['byte_limit'] // 1024**2} MB per day."})


def consume(row, nbytes: int = 0):
    if row is None:
        return
    with db() as c:
        c.execute("UPDATE users SET used=used+1, bytes_today=bytes_today+? WHERE id=?",
                  (nbytes, row["id"]))


# --------------------------------------------------------------- errors -----
class UrlError(HTTPException):
    def __init__(self, status: int, code: str, message: str, hint: str = ""):
        super().__init__(status, detail={"code": code, "message": message, "hint": hint})


def _classify(msg: str):
    m = re.sub(r"\x1b\[[0-9;]*m", "", msg)
    low = m.lower()
    if "drm" in low or "widevine" in low:
        return ("DRM_PROTECTED", "This stream is DRM-protected.",
                "No tool can download DRM content, including the desktop app.")
    if "sign in to confirm" in low or "not a bot" in low:
        return ("YOUTUBE_BOT_CHECK",
                "YouTube is asking this server to prove it is not a bot.",
                "This is an IP-reputation block, not a login problem. Fix: set "
                "COOKIES_FILE to a cookies.txt exported from your browser, and/or "
                "set PROXY_URL to a residential proxy. A user login cannot fix this.")
    if "private video" in low or "is private" in low:
        return ("PRIVATE", "That media is private.", "Only public media can be downloaded.")
    if "sign in" in low or "login" in low or "cookies" in low or "logged-in" in low:
        return ("LOGIN_REQUIRED", "This site wants a logged-in session.",
                "Set COOKIES_FILE on the server to a cookies.txt from your browser.")
    if "geo" in low or "not available in your country" in low:
        return ("GEO_BLOCKED", "Blocked in the server's region.",
                "Needs a server/VPN in another country.")
    if "unsupported url" in low:
        return ("UNSUPPORTED", "No extractor recognises that site.",
                "Direct file links (.mp4/.mp3) still work through the proxy path.")
    if "403" in low or "forbidden" in low:
        return ("BLOCKED", "The site refused the server's request.",
                "Many sites block datacenter IPs. Cookies or a residential proxy help.")
    if "404" in low or "not found" in low:
        return ("NOT_FOUND", "That page or file was not found.", "Check the link.")
    if "429" in low or "too many requests" in low:
        return ("RATE_LIMITED", "The site is throttling this server.", "Wait, or use cookies.")
    if "ffmpeg" in low or "merging" in low:
        return ("NEEDS_MERGE", "Needs ffmpeg to merge streams; server has none.",
                "Deploy with the provided Dockerfile.")
    return ("FAILED", m[:300], "")


# ---------------------------------------------------------------- models ----
class LoginRequest(BaseModel):
    name: str
    turnstile_token: Optional[str] = None


class InfoRequest(BaseModel):
    url: str


class QuotaRequest(BaseModel):
    name: str
    daily: Optional[int] = None
    bytes: Optional[int] = None
    blocked: Optional[bool] = None


def _verify_turnstile(token: Optional[str], ip: str = ""):
    if not TURNSTILE_SECRET:
        return True  # not configured -> skip
    if not token:
        raise HTTPException(400, detail={
            "code": "TURNSTILE_MISSING", "message": "Verification required.",
            "hint": "Complete the Cloudflare check."})
    try:
        r = httpx.post("https://challenges.cloudflare.com/turnstile/v0/siteverify",
                       data={"secret": TURNSTILE_SECRET, "response": token, "remoteip": ip},
                       timeout=15)
        return bool(r.json().get("success"))
    except Exception:
        return False


def _clean_name(n: str) -> str:
    n = re.sub(r"\s+", " ", (n or "").strip())
    if len(n) < 2:
        raise HTTPException(400, detail={
            "code": "BAD_NAME", "message": "Name must be at least 2 characters."})
    if len(n) > 40:
        raise HTTPException(400, detail={
            "code": "BAD_NAME", "message": "Name is too long (max 40)."})
    return n


# ---------------------------------------------------------------- routes ----
@app.get("/health")
def health():
    return {
        "ok": True, "version": APP_VERSION,
        "ffmpeg": HAS_FFMPEG, "ytdlp": yt_dlp.version.__version__,
        "extractors": 1755,
        "cookies": _cookie_report(),
        "proxy": bool(PROXY_URL),
        "turnstile": bool(TURNSTILE_SECRET),
        "login_required": REQUIRE_LOGIN,
        "per_user_cookies": True,
        "cookie_encryption": _HAVE_CRYPTO,
        "daily_default": DEFAULT_DAILY,
        "sitekey": TURNSTILE_SITEKEY or None,
        "youtube_ok": bool(_cookie_report().get("present")),
    }


@app.post("/api/login")
def api_login(req: LoginRequest, request: Request):
    name = _clean_name(req.name)
    if not _verify_turnstile(req.turnstile_token, request.client.host if request.client else ""):
        raise HTTPException(400, detail={
            "code": "TURNSTILE_FAILED", "message": "Verification failed.",
            "hint": "Reload and complete the Cloudflare check again."})

    with db() as c:
        row = c.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
        if row is None:
            token = _new_token()
            c.execute("""INSERT INTO users(name, token, created, day, used, bytes_today,
                         daily_limit, byte_limit, blocked)
                         VALUES(?,?,?,?,0,0,?,?,0)""",
                      (name, token, time.time(), _today(), DEFAULT_DAILY, DEFAULT_BYTES))
        else:
            if row["blocked"]:
                raise HTTPException(403, detail={
                    "code": "BLOCKED", "message": "This account is blocked."})
            token = row["token"]
            if row["day"] != _today():
                c.execute("UPDATE users SET day=?, used=0, bytes_today=0 WHERE id=?",
                          (_today(), row["id"]))
    row = _get_user(token)
    return {
        "token": token, "name": row["name"],
        "used": row["used"], "limit": row["daily_limit"],
        "bytes_today": row["bytes_today"], "byte_limit": row["byte_limit"],
    }


@app.get("/api/me")
def api_me(row=Depends(require_user)):
    if row is None:
        return {"name": "anonymous", "used": 0, "limit": 0,
                "bytes_today": 0, "byte_limit": 0, "login_required": False}
    return {"name": row["name"], "used": row["used"], "limit": row["daily_limit"],
            "bytes_today": row["bytes_today"], "byte_limit": row["byte_limit"],
            "login_required": True}


@app.post("/api/logout")
def api_logout(row=Depends(require_user)):
    if row is None:
        return {"ok": True}
    # Revoke: rotate the token so the presented one stops working.
    new = _new_token()
    with db() as c:
        c.execute("UPDATE users SET token=? WHERE id=?", (new, row["id"]))
    return {"ok": True, "note": "Session revoked. Sign in again to get a new token."}


@app.get("/api/admin/users")
def api_admin_users(key: str = Query("")):
    if not ADMIN_KEY or key != ADMIN_KEY:
        raise HTTPException(403, detail={"code": "FORBIDDEN", "message": "Admin key required."})
    with db() as c:
        rows = c.execute("SELECT name,created,day,used,bytes_today,daily_limit,"
                         "byte_limit,blocked FROM users ORDER BY created DESC").fetchall()
    return {"count": len(rows), "users": [dict(r) for r in rows]}


@app.post("/api/admin/quota")
def api_admin_quota(req: QuotaRequest, key: str = Query("")):
    if not ADMIN_KEY or key != ADMIN_KEY:
        raise HTTPException(403, detail={"code": "FORBIDDEN", "message": "Admin key required."})
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE name=?", (req.name,)).fetchone()
        if not row:
            raise HTTPException(404, detail={"code": "NO_USER", "message": "No such user."})
        c.execute("""UPDATE users SET daily_limit=COALESCE(?,daily_limit),
                     byte_limit=COALESCE(?,byte_limit),
                     blocked=COALESCE(?,blocked) WHERE id=?""",
                  (req.daily, req.bytes, None if req.blocked is None else int(req.blocked),
                   row["id"]))
    return {"ok": True, "name": req.name}


# ------------------------------------------------------- youtube cookies ----
COOKIE_PATH = Path(COOKIES_FILE or "cookies.txt")


def _cookie_report():
    """Parse the Netscape cookie file and report what we have."""
    if not COOKIE_PATH.exists():
        return {"present": False, "path": str(COOKIE_PATH), "count": 0, "domains": []}
    try:
        lines = COOKIE_PATH.read_text(errors="replace").splitlines()
    except Exception:
        return {"present": False, "path": str(COOKIE_PATH), "count": 0, "domains": []}
    rows = [l for l in lines if l.strip() and not l.startswith("#") and "\t" in l]
    domains = sorted({r.split("\t")[0].lstrip(".") for r in rows if r.count("\t") >= 6})
    yt = [r for r in rows if "youtube.com" in r or "google.com" in r]
    mtime = COOKIE_PATH.stat().st_mtime
    return {
        "present": bool(rows),
        "path": str(COOKIE_PATH),
        "count": len(rows),
        "youtube_cookies": len(yt),
        "domains": domains[:12],
        "updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)),
    }


@app.post("/api/admin/cookies")
async def api_admin_cookies(request: Request, key: str = Query("")):
    """Upload a Netscape cookies.txt. Highest-value endpoint in this service:
    this is the ONLY thing that makes YouTube work from a datacenter IP."""
    if not ADMIN_KEY or key != ADMIN_KEY:
        raise HTTPException(403, detail={"code": "FORBIDDEN", "message": "Admin key required."})

    raw = (await request.body()).decode("utf-8", "replace").strip()
    if not raw:
        raise HTTPException(400, detail={"code": "EMPTY",
                                        "message": "Paste or upload a cookies.txt file."})

    lines = [l for l in raw.splitlines() if l.strip() and not l.startswith("#")]
    rows = [l for l in lines if l.count("\t") >= 6]
    if len(rows) < 3:
        raise HTTPException(400, detail={
            "code": "BAD_COOKIES",
            "message": "That does not look like a Netscape cookies.txt file.",
            "hint": "Each line needs 7 tab-separated fields. Export with the "
                    "'Get cookies.txt LOCALLY' extension, not a screenshot or JSON."})

    has_yt = any("youtube.com" in r or "google.com" in r for r in rows)
    if not has_yt:
        raise HTTPException(400, detail={
            "code": "NO_YOUTUBE_COOKIES",
            "message": "No YouTube/Google cookies found in that file.",
            "hint": "Open youtube.com while logged in, then export cookies for "
                    "youtube.com (not another site)."})

    COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
    COOKIE_PATH.write_text(raw + "\n", encoding="utf-8")
    try:
        os.chmod(COOKIE_PATH, 0o600)
    except Exception:
        pass
    return {"ok": True, "saved_to": str(COOKIE_PATH), **_cookie_report()}


@app.get("/api/admin/cookies/status")
def api_admin_cookies_status(key: str = Query("")):
    if not ADMIN_KEY or key != ADMIN_KEY:
        raise HTTPException(403, detail={"code": "FORBIDDEN", "message": "Admin key required."})
    return _cookie_report()


# --------------------------------------------- per-user YouTube account ----
def _parse_cookie_text(raw: str) -> tuple[int, int]:
    lines = [l for l in (raw or "").splitlines() if l.strip() and not l.startswith("#")]
    rows = [l for l in lines if l.count("\t") >= 6]
    yt = [l for l in rows if "youtube.com" in l or "google.com" in l]
    return len(rows), len(yt)


@app.post("/api/me/cookies")
async def api_me_cookies(request: Request, row=Depends(require_user)):
    """Connect the caller's OWN YouTube account cookies.

    Design note: cookies belong to the individual user, are encrypted at rest,
    are never returned by any endpoint, and are used only for that user's own
    downloads. That is the only honest way to shift the account risk to the
    person who chose to take it.
    """
    if row is None:
        raise HTTPException(401, detail={"code": "NO_TOKEN",
                                        "message": "Sign in first."})
    raw = (await request.body()).decode("utf-8", "replace").strip()
    total, yt = _parse_cookie_text(raw)
    if total < 3:
        raise HTTPException(400, detail={
            "code": "BAD_COOKIES",
            "message": "That does not look like a Netscape cookies.txt file.",
            "hint": "Export with the 'Get cookies.txt LOCALLY' extension — each line "
                    "needs 7 tab-separated fields."})
    if yt == 0:
        raise HTTPException(400, detail={
            "code": "NO_YOUTUBE_COOKIES",
            "message": "No YouTube/Google cookies in that file.",
            "hint": "Open youtube.com while logged in and export cookies for youtube.com."})
    with db() as c:
        c.execute("UPDATE users SET cookies_enc=? WHERE id=?", (_enc(raw), row["id"]))
    return {"ok": True, "connected": True, "cookies": total, "youtube_cookies": yt,
            "note": "Used only for your own downloads. Delete any time."}


@app.get("/api/me/cookies/status")
def api_me_cookies_status(row=Depends(require_user)):
    if row is None:
        return {"connected": False}
    text = _user_cookies_text(row)
    if not text:
        return {"connected": False, "global_fallback":
                bool(COOKIES_FILE and os.path.exists(COOKIES_FILE))}
    total, yt = _parse_cookie_text(text)
    return {"connected": bool(yt), "cookies": total, "youtube_cookies": yt,
            "encrypted": _HAVE_CRYPTO}


@app.delete("/api/me/cookies")
def api_me_cookies_delete(row=Depends(require_user)):
    if row is None:
        raise HTTPException(401, detail={"code": "NO_TOKEN", "message": "Sign in first."})
    with db() as c:
        c.execute("UPDATE users SET cookies_enc=NULL WHERE id=?", (row["id"],))
    return {"ok": True, "connected": False}


@app.get("/api/youtube/check")
def api_youtube_check(row=Depends(require_user)):
    """Tell the user/operator whether YouTube can actually work right now."""
    rep = _cookie_report()
    return {
        "cookies_active": rep["present"] and rep.get("youtube_cookies", 0) > 0,
        "cookies": rep,
        "proxy": bool(PROXY_URL),
        "verdict": (
            "YouTube should work (cookies loaded)."
            if rep["present"] and rep.get("youtube_cookies", 0) > 0
            else "YouTube will hit the bot-check. An admin must upload a cookies.txt "
                 "via POST /api/admin/cookies, or set PROXY_URL to a residential proxy."
        ),
    }


# ------------------------------------------------------------- extraction ---
def _validate(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise UrlError(400, "NO_URL", "Paste a URL first.")
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise UrlError(400, "BAD_URL", "Only http/https URLs are accepted.")
    return url


def is_direct_media(url: str) -> bool:
    return bool(MEDIA_EXT.search(urlparse(url).path + urlparse(url).query))


def _user_cookies_text(row) -> str | None:
    """The cookies this user connected for their own YouTube account."""
    if row is None:
        return None
    try:
        return _dec(row["cookies_enc"])
    except Exception:
        return None


@contextlib.contextmanager
def _cookies_for(row):
    """Yield a cookie-file path to use: the USER'S own first, then the global one.

    Each user's cookies are written to a private temp file for the duration of
    the request and removed afterwards, so nothing lingers on disk.
    """
    tmp = None
    try:
        text = _user_cookies_text(row)
        if text:
            fd, tmp = tempfile.mkstemp(prefix="s4k_ck_", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            try:
                os.chmod(tmp, 0o600)
            except Exception:
                pass
            yield tmp, "user"
            return
        if COOKIES_FILE and os.path.exists(COOKIES_FILE):
            yield COOKIES_FILE, "global"
            return
        yield None, "none"
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except Exception:
                pass


def _opts(cookiefile: str | None = None) -> dict:
    o = {
        "quiet": True, "no_warnings": True, "noplaylist": False,
        "nocheckcertificate": True, "socket_timeout": 30,
        "retries": 3, "fragment_retries": 3, "extractor_retries": 2,
        "http_headers": {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
    }
    if cookiefile:
        o["cookiefile"] = cookiefile
    if PROXY_URL:
        o["proxy"] = PROXY_URL
    return o


def _extract(url: str, cookiefile: str | None = None) -> dict:
    try:
        with yt_dlp.YoutubeDL(_opts(cookiefile)) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        raise UrlError(422, *_classify(str(e)))
    except Exception as e:  # noqa: BLE001
        raise UrlError(500, "INTERNAL", f"Extraction failed: {e}")
    if info.get("_type") == "playlist":
        entries = [x for x in (info.get("entries") or []) if x]
        if not entries:
            raise UrlError(422, "EMPTY", "That playlist has no playable entries.")
        info = entries[0]
    return info


def _human(n):
    if not n:
        return ""
    n = float(n)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def _summarise(info: dict) -> dict:
    formats, seen = [], set()
    for f in info.get("formats") or []:
        if f.get("protocol") in ("mhtml", "storyboard"):
            continue
        fid = f.get("format_id")
        if not fid or fid in seen:
            continue
        v, a = f.get("vcodec"), f.get("acodec")
        hv, ha = bool(v and v != "none"), bool(a and a != "none")
        if not hv and not ha:
            continue
        h = f.get("height") or 0
        if hv and h and h < 144:
            continue
        kind = "video+audio" if (hv and ha) else ("video" if hv else "audio")
        label = f"{h}p" if h else (f.get("format_note") or f.get("ext") or "media")
        if kind == "audio":
            abr = f.get("abr")
            label = f"audio {f.get('ext','')} {f'{abr:.0f}k' if abr else ''}".strip()
        elif kind == "video":
            label += " (video only)"
        else:
            label += " (video + audio)"
        size = f.get("filesize") or f.get("filesize_approx")
        formats.append({"format_id": str(fid), "kind": kind, "ext": f.get("ext") or "",
                        "height": h or None, "fps": f.get("fps") or None,
                        "abr": f.get("abr") or None, "vcodec": v if hv else None,
                        "acodec": a if ha else None, "filesize": size,
                        "filesize_h": _human(size), "label": label})
        seen.add(fid)
    formats.sort(key=lambda f: ({"video+audio": 0, "video": 1, "audio": 2}[f["kind"]],
                                -(f["height"] or f["abr"] or 0)))
    return {"mode": "streaming", "id": info.get("id"),
            "title": info.get("title") or "Untitled",
            "uploader": info.get("uploader") or info.get("channel") or "",
            "duration": info.get("duration") or 0,
            "thumbnail": info.get("thumbnail") or "",
            "extractor": info.get("extractor_key") or "",
            "formats": formats, "has_ffmpeg": HAS_FFMPEG,
            "cookies": bool(COOKIES_FILE and os.path.exists(COOKIES_FILE))}


@app.post("/api/info")
def api_info(req: InfoRequest, row=Depends(require_user)):
    url = _validate(req.url)
    if is_direct_media(url):
        try:
            with httpx.Client(timeout=PROXY_TIMEOUT, follow_redirects=True,
                              headers={"User-Agent": UA}) as c:
                r = c.head(url)
                ctype = r.headers.get("content-type", "")
                size = int(r.headers.get("content-length") or 0)
        except Exception:
            ctype, size = "", 0
        nm = unquote(urlparse(url).path.rsplit("/", 1)[-1]) or "media"
        return {"mode": "direct", "id": nm, "title": nm,
                "uploader": urlparse(url).netloc, "duration": 0, "thumbnail": "",
                "extractor": "direct link",
                "formats": [{"format_id": "direct", "kind": "video+audio", "ext": "",
                             "height": None, "fps": None, "abr": None, "vcodec": None,
                             "acodec": None, "filesize": size, "filesize_h": _human(size),
                             "label": f"direct download ({ctype or 'unknown type'})"}],
                "has_ffmpeg": HAS_FFMPEG, "cookies": False}
    with _cookies_for(row) as (ck, src):
        data = _summarise(_extract(url, ck))
    data["cookies_source"] = src
    return data


@app.get("/api/download")
def api_download(url: str = Query(...),
                 format_id: Optional[str] = None,
                 mode: str = Query("video", pattern="^(video|audio|direct)$"),
                 quality: Optional[int] = None,
                 convert: Optional[str] = None,
                 row=Depends(require_user)):
    url = _validate(url)
    charge(row)

    if mode == "direct" or is_direct_media(url):
        return _proxy_direct(url, row)

    with _cookies_for(row) as (ck, ck_src):
        info = _extract(url, ck)
    duration = info.get("duration") or 0
    if duration and duration > MAX_DOWNLOAD_SECONDS:
        raise UrlError(422, "TOO_LONG",
                       f"That media is {int(duration/60)} minutes, over the "
                       f"{int(MAX_DOWNLOAD_SECONDS/60)}-minute limit.",
                       "Raise MAX_DOWNLOAD_SECONDS on the server.")

    tmpdir = Path(tempfile.mkdtemp(prefix="s4k_"))
    ck_ctx = _cookies_for(row)
    ck, ck_src = ck_ctx.__enter__()
    opts = _opts(ck)
    opts["outtmpl"] = str(tmpdir / "%(title).150B.%(ext)s")

    if mode == "audio":
        opts["format"] = "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]/bestaudio/best"
        if convert == "mp3" and HAS_FFMPEG:
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio",
                                       "preferredcodec": "mp3", "preferredquality": "192"}]
    elif format_id:
        opts["format"] = format_id
        if HAS_FFMPEG:
            opts["merge_output_format"] = "mp4"
    elif quality:
        opts["format"] = (f"bv*[height<={quality}]+ba/b[height<={quality}]/b"
                          if HAS_FFMPEG else
                          f"b[height<={quality}][acodec!=none]/b[acodec!=none]/b")
        if HAS_FFMPEG:
            opts["merge_output_format"] = "mp4"
    else:
        opts["format"] = "bv*+ba/b" if HAS_FFMPEG else "b[acodec!=none]/b"
        if HAS_FFMPEG:
            opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as e:
        ck_ctx.__exit__(None, None, None)
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise UrlError(422, *_classify(str(e)))
    except Exception as e:  # noqa: BLE001
        ck_ctx.__exit__(None, None, None)
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise UrlError(500, "INTERNAL", f"Download failed: {e}")
    finally:
        try:
            ck_ctx.__exit__(None, None, None)
        except Exception:
            pass

    files = sorted((p for p in tmpdir.iterdir() if p.is_file()),
                   key=lambda p: p.stat().st_size, reverse=True)
    if not files:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise UrlError(500, "NO_OUTPUT", "yt-dlp produced no file.")

    target = files[0]
    ext = target.suffix.lower()
    vtypes = {".mp4": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska"}
    atypes = {".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".opus": "audio/opus",
              ".ogg": "audio/ogg", ".wav": "audio/wav", ".webm": "audio/webm",
              ".mp4": "audio/mp4"}
    table = atypes if mode == "audio" else vtypes
    media_type = table.get(ext) or vtypes.get(ext) or atypes.get(ext) or "application/octet-stream"

    consume(row, target.stat().st_size)
    safe = re.sub(r'[\\/:*?"<>|]+', "_", target.name)[:120]
    return FileResponse(target, media_type=media_type, filename=safe,
                        headers={"X-Mode": "streaming", "X-Cookies": ck_src},
                        background=_Cleanup(tmpdir))


def _proxy_direct(url: str, row):
    client = httpx.Client(timeout=PROXY_TIMEOUT, follow_redirects=True,
                          headers={"User-Agent": UA})
    kwargs = {}
    if PROXY_URL:
        kwargs["proxy"] = PROXY_URL
    try:
        r = client.send(client.build_request("GET", url, **kwargs), stream=True)
    except Exception as e:  # noqa: BLE001
        client.close()
        raise UrlError(502, "PROXY_FAILED", f"Could not reach that link: {e}",
                       "The host may be down, or it blocks this server's IP.")
    if r.status_code >= 400:
        client.close()
        raise UrlError(502, "PROXY_HTTP", f"The host returned HTTP {r.status_code}.",
                       "Check the link in a browser; some hosts need a session cookie.")

    ctype = r.headers.get("content-type", "application/octet-stream")
    name = unquote(urlparse(url).path.rsplit("/", 1)[-1]) or "media"
    if not re.search(r"\.[a-z0-9]{2,5}$", name, re.I):
        name += mimetypes.guess_extension(ctype.split(";")[0].strip()) or ".mp4"

    def body():
        total = 0
        try:
            for chunk in r.iter_bytes(256 * 1024):
                total += len(chunk)
                yield chunk
        finally:
            try:
                consume(row, total)
            except Exception:
                pass
            r.close()
            client.close()

    headers = {
        "Content-Disposition": f'attachment; filename="{re.sub(chr(34), "", name)[:120]}"',
        "X-Mode": "direct",
    }
    if r.headers.get("content-length"):
        headers["Content-Length"] = r.headers["content-length"]
    return StreamingResponse(body(), media_type=ctype, headers=headers)


class _Cleanup:
    def __init__(self, path: Path):
        self.path = path

    async def __call__(self):
        shutil.rmtree(self.path, ignore_errors=True)


@app.get("/")
def root():
    return JSONResponse({
        "service": "S.4K URL Import backend", "version": APP_VERSION,
        "endpoints": ["/health", "/api/login", "/api/me", "/api/info",
                      "/api/download", "/api/admin/users", "/docs"],
        "auth": "POST /api/login {name} -> token; send Authorization: Bearer <token>",
        "youtube": "needs COOKIES_FILE (a login cannot fix an IP-reputation block)",
    })
