#!/usr/bin/env python3
"""Standalone glassmorphism login site using Telegram's official Login Widget.

This is a website login only. It never requests Telegram SMS codes,
2FA passwords, or user-account session keys and cannot read private chats.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import subprocess
import sys
import threading
import logging
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

ROOT = Path(__file__).resolve().parent
PORT = int(os.getenv("PORT", "10000"))
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
SESSION_SECRET = os.getenv("SESSION_SECRET", "").strip()
SESSION_KEY = (SESSION_SECRET or BOT_TOKEN or "local-dev-only").encode("utf-8")
COOKIE_NAME = "tg_glass_auth"
COOKIE_LIFETIME = 7 * 24 * 3600


def encode64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def decode64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def sign_session(user: dict) -> str:
    payload = encode64(json.dumps({"user": user, "exp": int(time.time()) + COOKIE_LIFETIME},
                                  separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    sig = encode64(hmac.new(SESSION_KEY, payload.encode("ascii"), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def verify_session(token: str) -> dict | None:
    try:
        payload, signature = token.split(".", 1)
        expected = encode64(hmac.new(SESSION_KEY, payload.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        data = json.loads(decode64(payload))
        if not isinstance(data.get("exp"), int) or data["exp"] < time.time():
            return None
        user = data.get("user")
        return user if isinstance(user, dict) else None
    except (ValueError, TypeError, UnicodeError, KeyError):
        return None


def verify_telegram(data: dict[str, str]) -> dict | None:
    if not BOT_TOKEN:
        return None
    supplied = data.get("hash", "")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", supplied):
        return None
    try:
        auth_time = int(data["auth_date"])
        user_id = int(data["id"])
    except (ValueError, KeyError):
        return None
    if user_id <= 0 or abs(time.time() - auth_time) > 300:
        return None
    allowed = {"auth_date", "first_name", "last_name", "id", "photo_url", "username"}
    check = "\n".join(f"{key}={value}" for key, value in sorted(data.items())
                      if key != "hash" and key in allowed)
    token_hash = hashlib.sha256(BOT_TOKEN.encode("utf-8")).digest()
    expected = hmac.new(token_hash, check.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied.lower(), expected):
        return None
    return {"id": user_id, "first_name": data.get("first_name", ""),
            "last_name": data.get("last_name", ""), "username": data.get("username", ""),
            "photo_url": data.get("photo_url", "")}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Do not log one-time paths or Telegram query signatures.
        logging.info("web request completed")

    def respond(self, code: int, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def current_user(self) -> dict | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            session = cookie.get(COOKIE_NAME)
            return verify_session(session.value) if session else None
        except Exception:
            return None

    def send_cookie(self, value: str, max_age: int) -> None:
        self.send_header("Set-Cookie", f"{COOKIE_NAME}={value}; Path=/; HttpOnly; SameSite=Lax; "
                         f"Max-Age={max_age}" + ("; Secure" if PUBLIC_BASE_URL.startswith("https://") else ""))

    def redirect(self, path: str, cookie: str | None = None, max_age: int = COOKIE_LIFETIME) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", path)
        self.send_header("Cache-Control", "no-store")
        if cookie is not None:
            self.send_cookie(cookie, max_age)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("/health", "/healthz"):
            self.respond(200, b'{"ok":true}', "application/json; charset=utf-8")
            return
        if parsed.path == "/api/me":
            user = self.current_user()
            self.respond(200, json.dumps({"authenticated": bool(user), "user": user},
                                         ensure_ascii=False).encode("utf-8"),
                         "application/json; charset=utf-8")
            return
        if parsed.path == "/auth/telegram":
            params = parse_qs(parsed.query, keep_blank_values=True)
            fields = {k: v[0] for k, v in params.items() if len(v) == 1}
            user = verify_telegram(fields)
            if not user:
                self.redirect("/?error=auth")
                return
            self.redirect("/", sign_session(user))
            return
        if parsed.path.startswith("/auth/"):
            # Personal-account Telethon links belong to a different app.
            # Do not forward Telegram codes or 2FA passwords to this site.
            self.respond(409, ("""<!doctype html><html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Другой способ входа</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;padding:20px;
font:16px system-ui;color:#f1f4ff;background:radial-gradient(circle at 20% 20%,#214b55,#0b1121 65%)}
main{max-width:480px;padding:36px;border-radius:24px;background:#ffffff12;
backdrop-filter:blur(24px);border:1px solid #ffffff30;box-shadow:0 12px 44px #0005}
h1{font-size:24px}p{line-height:1.7;color:#d7ddeb}a{display:inline-block;padding:14px 18px;
background:#88dcca;color:#061d22;border-radius:12px;text-decoration:none;font-weight:700}</style>
</head><body><main><h1>Ссылка от другой системы входа</h1>
<p>Эта ссылка создана менеджером чатов, но данный сайт поддерживает официальный Telegram Login.
Он не запрашивает коды подтверждения или облачные пароли и не предоставляет доступ к личным чатам.</p>
<a href="/">Открыть страницу входа</a></main></body></html>""").encode("utf-8"))
            return
        if parsed.path not in ("/", "/index.html"):
            self.respond(404, b"Not found", "text/plain; charset=utf-8")
            return
        page = (ROOT / "index.html").read_text(encoding="utf-8")
        from html import escape
        widget = ""
        if BOT_USERNAME and BOT_TOKEN and PUBLIC_BASE_URL.startswith("https://"):
            widget = (f'<script async src="https://telegram.org/js/telegram-widget.js?22" '
                      f'data-telegram-login="{escape(BOT_USERNAME, quote=True)}" '
                      f'data-size="large" data-radius="12" '
                      f'data-auth-url="{escape(PUBLIC_BASE_URL, quote=True)}/auth/telegram"></script>')
        page = page.replace("<!-- TG_WIDGET -->", widget)
        page = page.replace("__CONFIG_READY__", "true" if widget else "false")
        self.respond(200, page.encode("utf-8"))

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/logout":
            self.respond(404, b"Not found", "text/plain; charset=utf-8")
            return
        # No sensitive data accepted from POST requests.
        self.redirect("/", "", max_age=0)


def run_bot_alongside_site() -> None:
    """Run an existing bot.py beside the website without competing for PORT.

    The website uses the external Render PORT; the bot has its own internal
    listener so both can run in one Web Service. Neither process is used as
    a credential relay for the other.
    """
    script = ROOT / "bot.py"
    if not script.is_file():
        print("INFO: bot.py not present; starting the site only", flush=True)
        return
    if os.getenv("START_BOT", "1").strip().lower() in ("0", "false", "no"):
        print("INFO: bot.py startup disabled by START_BOT", flush=True)
        return

    def worker() -> None:
        env = os.environ.copy()
        internal_port = int(env.get("BOT_INTERNAL_PORT", "10001"))
        if internal_port == PORT or not (1 <= internal_port <= 65535):
            print("ERROR: BOT_INTERNAL_PORT must differ from PORT", flush=True)
            return
        env["PORT"] = str(internal_port)
        restarts = 0
        while restarts < 3:
            print(f"INFO: starting bot.py (internal port {internal_port})", flush=True)
            try:
                proc = subprocess.Popen([sys.executable, str(script)], cwd=str(ROOT), env=env)
                returncode = proc.wait()
            except OSError as exc:
                print(f"ERROR: could not start bot.py: {exc}", flush=True)
                return
            restarts += 1
            print(f"WARNING: bot.py exited ({returncode}); attempt {restarts}/3", flush=True)
            if restarts < 3:
                time.sleep(min(5 * restarts, 15))
        print("ERROR: bot.py failed repeatedly. Check Render logs.", flush=True)

    threading.Thread(target=worker, name="bot-supervisor", daemon=True).start()


if __name__ == "__main__":
    if not 1 <= PORT <= 65535:
        raise SystemExit("PORT must be 1-65535")
    if not BOT_TOKEN or not BOT_USERNAME or not PUBLIC_BASE_URL:
        print("CONFIG: add BOT_TOKEN, BOT_USERNAME, PUBLIC_BASE_URL for Telegram login")
    print(f"Web server listening on 0.0.0.0:{PORT}", flush=True)
    run_bot_alongside_site()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
