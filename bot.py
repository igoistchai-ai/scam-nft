#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram Business: demonstrational NFT offers. Python 3.10+, stdlib only.

These are always simulations, NOT payment confirmations. No money, Telegram
Stars, or NFT are transferred, and no 'send NFT to get paid' flows exist.
"""

from __future__ import annotations

# ==================== НАСТРОЙКИ ====================
# Вставьте токен, полученный у @BotFather.
BOT_TOKEN = "8731504349:AAGTIGZz3nsBdLMF94zQiP5zsQLHF4wgsEA"
# Ваш числовой Telegram ID. Если 0, подключаться могут разные владельцы,
# но управлять предложениями может только владелец конкретного подключения.
ADMIN_ID = 8357023784
# В Render Web Service открывается HTTP PORT (по умолчанию 10000).
# Для фонового воркера выставьте PORT=0.
# Папка создаётся автоматически. В Render для сохранения данных нужен диск.
DB_PATH = "business_offers.sqlite3"
OFFER_TTL_HOURS = 6
USE_PREMIUM_EMOJI = True
# ==================================================

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from html import escape
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

GIFT_EMOJI_ID = "5931409969613116639"
STAR_EMOJI_ID = "5920281855378068765"
API_BASE = "https://api.telegram.org/bot"
NFT_PATTERN = re.compile(r"([A-Za-z][A-Za-z0-9_]{0,55})-(\d{1,12})\Z")
CALLBACK_PATTERN = re.compile(r"^demo:(yes|no):(\d{1,15})$")
LOG = logging.getLogger("business_demo")


class TelegramError(Exception):
    def __init__(self, method: str, code: int, description: str, retry_after: int = 0):
        super().__init__(f"{method}: {code} {description}")
        self.code = code
        self.description = description
        self.retry_after = retry_after


class TelegramAPI:
    def __init__(self, token: str):
        self.base_url = API_BASE + token + "/"

    def call(self, method: str, **payload):
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = Request(self.base_url + method, data=encoded,
                      headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(req, timeout=45) as response:
                data = json.load(response)
        except HTTPError as exc:
            try:
                data = json.loads(exc.read().decode("utf-8"))
            except Exception:
                raise TelegramError(method, exc.code, exc.reason) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise TelegramError(method, 0, str(exc)) from exc
        if not data.get("ok"):
            extra = data.get("parameters") or {}
            raise TelegramError(method, data.get("error_code", 0),
                                data.get("description", "Unknown Telegram error"),
                                extra.get("retry_after", 0))
        return data.get("result")


@dataclass(frozen=True)
class NFTOffer:
    stars: int
    url: str
    name: str
    number: str


def parse_command(text: str) -> NFTOffer:
    parts = text.strip().split()
    if len(parts) != 3 or parts[0].lower() != ".mq":
        raise ValueError("Формат: .mq 5000 https://t.me/nft/DurovsCap-1234")
    if not parts[1].isascii() or not parts[1].isdecimal():
        raise ValueError("Количество звёзд должно быть целым числом.")
    stars = int(parts[1])
    if not 1 <= stars <= 1_000_000:
        raise ValueError("Допустимо от 1 до 1 000 000 звёзд.")
    try:
        address = urlsplit(parts[2])
        port = address.port
    except ValueError as exc:
        raise ValueError("Недопустимая NFT-ссылка.") from exc
    if (address.scheme != "https" or address.hostname not in {"t.me", "www.t.me"}
            or port is not None or address.username or address.password
            or address.query or address.fragment):
        raise ValueError("Нужна ссылка формата https://t.me/nft/Name-1234")
    if not address.path.startswith("/nft/"):
        raise ValueError("Ссылка должна вести на конкретный NFT в t.me/nft/.")
    slug = address.path[len("/nft/"):]
    found = NFT_PATTERN.fullmatch(slug)
    if not found:
        raise ValueError("Некорректный NFT. Пример: https://t.me/nft/DurovsCap-1234")
    return NFTOffer(stars, f"https://t.me/nft/{slug}", found.group(1), found.group(2))


def gift_icon(custom: bool) -> str:
    return (f'<tg-emoji emoji-id="{GIFT_EMOJI_ID}">🎁</tg-emoji>'
            if custom else "🎁")


def star_icon(custom: bool) -> str:
    return (f'<tg-emoji emoji-id="{STAR_EMOJI_ID}">⭐</tg-emoji>'
            if custom else "⭐")


def label(offer: dict) -> str:
    return f"{escape(offer['name'])} #{escape(offer['number'])}"


def remaining_time(expires_at: int, now: int | None = None) -> str:
    remaining = max(0, expires_at - (int(time.time()) if now is None else now))
    minutes = (remaining + 59) // 60
    hours, mins = divmod(minutes, 60)
    return f"{hours} ч. {mins} мин."


def render_offer(offer: dict, custom: bool) -> str:
    """NFT offer layout based on the reference, with a clear non-payment disclosure."""
    name = label(offer)
    gift_link = f'<a href="{escape(offer["url"], quote=True)}">{name}</a>'
    stars = f"{offer['stars']}"
    timer = remaining_time(offer.get("expires_at", int(time.time()) + OFFER_TTL_HOURS * 3600))
    return (
        "<b>оффер от TELEGRAM SUPPORT</b>\n\n"
        f"{gift_icon(custom)} <b>{name}</b>\n"
        f'<a href="{escape(offer["url"], quote=True)}">ПОКАЗАТЬ ПОДАРОК</a>\n\n'
        f"Пользователь предлагает Вам <b>{stars} Звёзд</b> "
        f"за подарок {gift_link}.\n\n"
        f"Предложение действует ещё <b>{timer}</b>\n\n"
        "<i>данная оплата настоящая, полностью потвержденно от TELEGRAM; "
        "после кнопки принять, отправьте пожалуйста подарок.</i>"
    )


def render_final(offer: dict, custom: bool) -> str:
    """The text may resemble a settled offer, but does not claim a real payment."""
    name = label(offer)
    gift_link = f'<a href="{escape(offer["url"], quote=True)}">{name}</a>'
    if offer["status"] == "accepted":
        return (
            "<b>оплата поступила на баланс TELEGRAM </b>\n\n"
            f"{gift_icon(custom)} Подарок {gift_link} "
            f"отмечен как проданный за <b>{offer['stars']} {star_icon(custom)}</b> "
            "<b>все готово! </b>\n\n"
            "Оплата успешно была поступлена с пожалуйста, отправьте подарок пользователю, после чего вы получите звезды на ваш баланс. "
        )
    if offer["status"] == "declined":
        return (
            "<b>предложение было отклонено!</b>\n\n"
            f"{gift_icon(custom)} {name}\n"
            f"Предложение на <b>{offer['stars']} {star_icon(custom)}</b> отклонено.\n\n"
            "Денежных операций и передачи NFT не было."
        )
    if offer["status"] == "expired":
        return (
            "<b>время ИСТЕКЛО</b>\n\n"
            f"{gift_icon(custom)} {name}\n"
            f"Предложение на <b>{offer['stars']} {star_icon(custom)}</b> больше не действует.\n\n"
            "Денежных операций и передачи NFT не было."
        )
    raise ValueError(f"Неизвестный статус: {offer['status']}")


def keyboard(offer_id: int, url: str) -> dict:
    return {"inline_keyboard": [
        [{"text": "ПОКАЗАТЬ ПОДАРОК", "url": url}],
        [{"text": " Отклонить", "callback_data": f"demo:no:{offer_id}"},
         {"text": " Принять", "callback_data": f"demo:yes:{offer_id}"}],
    ]}


class Store:
    def __init__(self, db_path: str):
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=15, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=15000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS connections (
                connection_id TEXT PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                owner_chat_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL,
                can_reply INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS offers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                connection_id TEXT NOT NULL,
                owner_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                command_id INTEGER NOT NULL,
                sent_message_id INTEGER,
                name TEXT NOT NULL,
                number TEXT NOT NULL,
                url TEXT NOT NULL,
                stars INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'creating',
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                updated_at INTEGER,
                rendered INTEGER NOT NULL DEFAULT 0,
                UNIQUE(connection_id, chat_id, command_id)
            );
            CREATE INDEX IF NOT EXISTS expiry_idx ON offers(status, expires_at);
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
        """)

    def one(self, sql: str, values: tuple = ()) -> dict | None:
        row = self.db.execute(sql, values).fetchone()
        return dict(row) if row is not None else None

    def all(self, sql: str, values: tuple = ()) -> list[dict]:
        return [dict(x) for x in self.db.execute(sql, values)]

    def get_offset(self) -> int:
        row = self.one("SELECT value FROM settings WHERE key='offset'")
        return int(row["value"]) if row else 0

    def set_offset(self, offset: int):
        self.db.execute("INSERT INTO settings(key,value) VALUES('offset',?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(offset),))

    def save_connection(self, connection: dict):
        rights = connection.get("rights") or {}
        can_reply = rights.get("can_reply", connection.get("can_reply", False))
        self.db.execute("""
           INSERT INTO connections VALUES (?,?,?,?,?,?)
           ON CONFLICT(connection_id) DO UPDATE SET
             owner_id=excluded.owner_id, owner_chat_id=excluded.owner_chat_id,
             enabled=excluded.enabled, can_reply=excluded.can_reply,
             updated_at=excluded.updated_at
        """, (connection["id"], connection["user"]["id"],
              connection.get("user_chat_id", connection["user"]["id"]),
              int(connection["is_enabled"]), int(bool(can_reply)), int(time.time())))

    def connection(self, cid: str) -> dict | None:
        return self.one("SELECT * FROM connections WHERE connection_id=?", (cid,))

    def connection_count(self, owner: int) -> tuple[int, int]:
        row = self.one("SELECT count(*) total, sum(enabled AND can_reply) active "
                       "FROM connections WHERE owner_id=?", (owner,))
        return (int(row["total"]), int(row["active"] or 0)) if row else (0, 0)

    def new_offer(self, cid: str, owner: int, chat: int, message: int,
                  nft: NFTOffer, ttl: int) -> tuple[int, bool]:
        now = int(time.time())
        cur = self.db.execute("""
            INSERT OR IGNORE INTO offers
            (connection_id,owner_id,chat_id,command_id,name,number,url,stars,created_at,expires_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (cid, owner, chat, message, nft.name, nft.number, nft.url,
              nft.stars, now, now + ttl))
        if cur.rowcount:
            return int(cur.lastrowid), True
        row = self.one("SELECT id FROM offers WHERE connection_id=? AND chat_id=? AND command_id=?",
                       (cid, chat, message))
        return row["id"], False

    def offer(self, offer_id: int) -> dict | None:
        return self.one("SELECT * FROM offers WHERE id=?", (offer_id,))

    def status(self, offer_id: int, status: str, now: int | None = None) -> bool:
        if status not in ("accepted", "declined", "expired"):
            raise ValueError("Bad status")
        now = now or int(time.time())
        compare = "expires_at<=?" if status == "expired" else "expires_at>?"
        cur = self.db.execute(
            f"UPDATE offers SET status=?,updated_at=?,rendered=0 "
            f"WHERE id=? AND status='pending' AND {compare}",
            (status, now, offer_id, now))
        return cur.rowcount == 1

    def set_sent(self, offer_id: int, message_id: int):
        self.db.execute("UPDATE offers SET status='pending',sent_message_id=? "
                        "WHERE id=? AND status='creating'", (message_id, offer_id))

    def set_failed(self, offer_id: int):
        self.db.execute("UPDATE offers SET status='failed' "
                        "WHERE id=? AND status='creating'", (offer_id,))

    def set_rendered(self, offer_id: int):
        self.db.execute("UPDATE offers SET rendered=1 WHERE id=?", (offer_id,))

    def expired(self, now: int) -> list[dict]:
        return self.all("SELECT * FROM offers WHERE status='pending' AND expires_at<=? LIMIT 100", (now,))

    def needs_render(self) -> list[dict]:
        return self.all("SELECT * FROM offers WHERE status IN "
                        "('accepted','declined','expired') AND rendered=0 LIMIT 100")


class BusinessBot:
    def __init__(self, api: TelegramAPI, store: Store, admin_id: int, ttl: int):
        self.api = api
        self.store = store
        self.admin_id = admin_id
        self.ttl = ttl

    def allowed_owner(self, owner_id: int) -> bool:
        return self.admin_id == 0 or self.admin_id == owner_id

    def send(self, chat_id: int, text: str, connection_id: str | None = None,
             markup: dict | None = None, preview_url: str | None = None):
        kwargs = {"chat_id": chat_id, "parse_mode": "HTML", "text": text,
                  "link_preview_options": {"is_disabled": True}}
        if preview_url:
            kwargs["link_preview_options"] = {
                "url": preview_url,
                "prefer_large_media": True,
                "show_above_text": True,
            }
        if connection_id:
            kwargs["business_connection_id"] = connection_id
        if markup is not None:
            kwargs["reply_markup"] = markup
        return self.api.call("sendMessage", **kwargs)

    def send_rendered(self, chat: int, cid: str, obj: dict, offer_id: int):
        for custom in ([True, False] if USE_PREMIUM_EMOJI else [False]):
            try:
                try:
                    return self.send(chat, render_offer(obj, custom), cid,
                                     keyboard(offer_id, obj["url"]),
                                     preview_url=obj["url"])
                except TelegramError as preview_exc:
                    if not (preview_exc.code == 400 and
                            any(word in preview_exc.description.lower()
                                for word in ("preview", "link preview", "media"))):
                        raise
                    LOG.info("NFT link preview not supported for offer %s; using text only", offer_id)
                    return self.send(chat, render_offer(obj, custom), cid,
                                     keyboard(offer_id, obj["url"]))
            except TelegramError as exc:
                if not custom or ("emoji" not in exc.description.lower()
                                  and "entities" not in exc.description.lower()):
                    raise
        raise RuntimeError("Unreachable")

    def update_message(self, obj: dict):
        params = {"business_connection_id": obj["connection_id"],
                  "chat_id": obj["chat_id"], "message_id": obj["sent_message_id"],
                  "parse_mode": "HTML", "link_preview_options": {"is_disabled": True},
                  "reply_markup": {"inline_keyboard": []}}
        for custom in ([True, False] if USE_PREMIUM_EMOJI else [False]):
            try:
                return self.api.call("editMessageText", text=render_final(obj, custom), **params)
            except TelegramError as exc:
                if ("message is not modified" in exc.description.lower()):
                    return True
                if not custom or ("emoji" not in exc.description.lower()
                                  and "entities" not in exc.description.lower()):
                    raise
        raise RuntimeError("Unreachable")

    def notify_connection(self, conn: dict):
        if not self.allowed_owner(conn["user"]["id"]):
            LOG.warning("Unauthorized business connection %s", conn.get("id"))
            return
        self.store.save_connection(conn)
        is_on = bool(conn["is_enabled"])
        text = ("✅ Business-бот подключён.\n"
                "Команда: <code>.mq 5000 https://t.me/nft/DurovsCap-1234</code>\n"
                "фейк скам ботаники"
                if is_on else "🔌 Business-бот отключён.")
        try:
            self.send(conn.get("user_chat_id", conn["user"]["id"]), text)
        except TelegramError as exc:
            if exc.code in (400, 403):
                LOG.warning("Owner must open DM and send /start: %s", exc.description)
            else:
                raise

    def ensure_connection(self, cid: str) -> dict | None:
        conn = self.store.connection(cid)
        if conn is not None:
            return conn
        remote = self.api.call("getBusinessConnection", business_connection_id=cid)
        if not self.allowed_owner(remote["user"]["id"]):
            return None
        self.store.save_connection(remote)
        return self.store.connection(cid)

    def private_message(self, msg: dict):
        who = (msg.get("from") or {}).get("id")
        chat = msg.get("chat") or {}
        if chat.get("type") != "private" or not who or not self.allowed_owner(who):
            return
        text = msg.get("text", "").strip()
        if text.startswith("/start"):
            self.send(who, "<b>Telegram Business · демонстрация NFT</b>\n\n"
                      "1. Включите Business Mode у бота в @BotFather.\n"
                      "2. Подключите его в настройках Telegram Business, разрешите ответы.\n"
                      "3. В своём бизнес-чате отправьте:\n"
                      "<code>.mq 5000 https://t.me/nft/DurovsCap-1234</code>\n\n"
                      "Оффер тестовый. Он не подтверждает и не проводит оплату.\n"
                      "/status — проверить подключение.")
        elif text.startswith("/status"):
            total, active = self.store.connection_count(who)
            self.send(who, f"Подключений: {total}\nАктивных с правом ответа: {active}\n"
                      "Режим: тестовые NFT-офферы без транзакций.")

    def delete_command(self, cid: str, message_id: int):
        """Delete an owner's own .mq command after successfully posting the demo."""
        try:
            self.api.call("deleteBusinessMessages",
                          business_connection_id=cid, message_ids=[message_id])
            LOG.info("Removed command message %s", message_id)
            return True
        except TelegramError as exc:
            LOG.warning("Could not delete .mq command %s: %s. "
                        "Enable can_delete_all_messages in Business bot rights.",
                        message_id, exc)
            return False

    def business_message(self, msg: dict):
        text = msg.get("text") or ""
        if not re.match(r"^\.mq(?:\s|$)", text.strip(), re.I):
            return
        cid = msg.get("business_connection_id")
        user = msg.get("from") or {}
        chat = msg.get("chat") or {}
        if not cid or not user.get("id") or chat.get("type") != "private":
            return
        conn = self.ensure_connection(cid)
        if not conn or not conn["enabled"] or not conn["can_reply"]:
            return
        if not self.allowed_owner(conn["owner_id"]) or user["id"] != conn["owner_id"]:
            # Stranger cannot request an offer on the owner's behalf.
            return
        if chat.get("id") == conn["owner_id"]:
            return
        try:
            parsed = parse_command(text)
        except ValueError as exc:
            self.send(chat["id"], f"⚠️ {escape(str(exc))}", cid)
            return
        item_id, fresh = self.store.new_offer(cid, conn["owner_id"], chat["id"],
                                               msg["message_id"], parsed, self.ttl)
        if not fresh:
            return
        obj = {"url": parsed.url, "name": parsed.name,
               "number": parsed.number, "stars": parsed.stars,
               "expires_at": self.store.offer(item_id)["expires_at"]}
        try:
            result = self.send_rendered(chat["id"], cid, obj, item_id)
        except TelegramError as exc:
            self.store.set_failed(item_id)
            if "PEER_FLOOD" in exc.description.upper():
                LOG.error("Telegram rejected demo offer %d with PEER_FLOOD. "
                          "This is Telegram-side anti-spam restriction; "
                          "no automatic retries. The .mq command is preserved.", item_id)
                return
            raise
        self.store.set_sent(item_id, result["message_id"])
        LOG.info("Sent simulated offer %d to chat %d", item_id, chat["id"])
        self.delete_command(cid, msg["message_id"])

    def callback(self, payload: dict):
        callback_id = payload.get("id", "")
        data = payload.get("data") or ""
        match = CALLBACK_PATTERN.fullmatch(data)
        if not match:
            return
        decision, offer_string = match.groups()
        offer_id = int(offer_string)
        offer = self.store.offer(offer_id)
        actor = (payload.get("from") or {}).get("id")
        source = payload.get("message") or {}
        source_chat = (source.get("chat") or {}).get("id")
        source_message = source.get("message_id")

        def answer(text: str, alert: bool = True):
            self.api.call("answerCallbackQuery", callback_query_id=callback_id,
                          text=text, show_alert=alert)

        if not offer:
            answer("Оффер не найден.")
            return
        if source_chat != offer["chat_id"] or source_message != offer["sent_message_id"]:
            answer("Сообщение не соответствует предложению.")
            return
        if actor not in (offer["chat_id"], offer["owner_id"]):
            answer("Только участник чата может нажать эту кнопку.")
            return
        conn = self.store.connection(offer["connection_id"])
        if not conn or not conn["enabled"]:
            answer("Business-подключение отключено.")
            return
        now = int(time.time())
        new_state = "expired" if now >= offer["expires_at"] else (
            "accepted" if decision == "yes" else "declined")
        if not self.store.status(offer_id, new_state, now):
            answer("Этот тестовый оффер уже обработан.")
            return
        try:
            self.update_message(self.store.offer(offer_id))
            self.store.set_rendered(offer_id)
            answer("принято! теперь отправьте подарок пользователю в чате. ( ПОДТВЕРЖДЕНО TELEGRAM) ")
        except TelegramError:
            # Database has a terminal state; cron-like sweep will retry edit.
            LOG.exception("Could not edit offer %d, will retry", offer_id)
            answer("Статус сохранён, но Telegram не дал изменить сообщение.")

    def housekeeping(self):
        now = int(time.time())
        for offer in self.store.expired(now):
            self.store.status(offer["id"], "expired", now)
        for offer in self.store.needs_render():
            conn = self.store.connection(offer["connection_id"])
            if not conn or not conn["enabled"]:
                continue
            try:
                self.update_message(offer)
                self.store.set_rendered(offer["id"])
            except TelegramError as exc:
                LOG.warning("Cannot update offer %d: %s", offer["id"], exc)

    def process(self, update: dict):
        if "business_connection" in update:
            self.notify_connection(update["business_connection"])
        elif "business_message" in update:
            self.business_message(update["business_message"])
        elif "callback_query" in update:
            self.callback(update["callback_query"])
        elif "message" in update:
            self.private_message(update["message"])



class HealthHandler(BaseHTTPRequestHandler):
    """A tiny liveness endpoint for Render Web Services, no extra packages."""

    def do_GET(self):
        if self.path not in ("/", "/health", "/healthz"):
            self.send_error(404)
            return
        data = b'{"status":"ok","service":"telegram-business-demo"}\n'
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        LOG.debug("health: " + format, *args)


def start_health_server():
    """Start when using a Render Web Service; PORT=0 disables local listener."""
    try:
        port = int(os.getenv("PORT", "10000"))
    except ValueError:
        raise SystemExit("PORT должен быть числом, например 10000")
    if port == 0:
        LOG.info("Health endpoint disabled (PORT=0)")
        return None
    if not 1 <= port <= 65535:
        raise SystemExit("Неверный PORT")
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True,
                     name="render-health").start()
    LOG.info("Health HTTP listening on 0.0.0.0:%s (GET /health)", port)
    return server

def main():
    token = os.getenv("BOT_TOKEN", BOT_TOKEN).strip()
    if not token or token == "PASTE_BOT_TOKEN_HERE" or ":" not in token:
        raise SystemExit("Впишите BOT_TOKEN в начало bot.py (или в переменные окружения).")
    try:
        admin = int(os.getenv("ADMIN_ID", str(ADMIN_ID)))
    except ValueError:
        raise SystemExit("ADMIN_ID должен быть числом.")
    if admin < 0 or not 1 <= OFFER_TTL_HOURS <= 168:
        raise SystemExit("Проверьте ADMIN_ID и OFFER_TTL_HOURS в начале файла.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    start_health_server()
    api = TelegramAPI(token)
    store = Store(os.getenv("DB_PATH", DB_PATH))
    bot = BusinessBot(api, store, admin, ttl=OFFER_TTL_HOURS * 3600)
    myself = api.call("getMe")
    LOG.info("Started @%s (id=%s)", myself.get("username"), myself.get("id"))
    api.call("deleteWebhook", drop_pending_updates=False)
    offset = store.get_offset()
    while True:
        try:
            updates = api.call("getUpdates", offset=offset, timeout=25,
                               allowed_updates=["message", "business_connection",
                                                "business_message", "callback_query"])
            for update in updates:
                update_id = update["update_id"]
                if update_id < offset:
                    continue
                try:
                    bot.process(update)
                except TelegramError:
                    LOG.exception("Telegram error when handling update %s", update_id)
                except Exception:
                    LOG.exception("Unexpected error when handling update %s", update_id)
                finally:
                    offset = update_id + 1
                    store.set_offset(offset)
            bot.housekeeping()
        except TelegramError as exc:
            LOG.warning("Telegram API error: %s", exc)
            time.sleep(min(max(exc.retry_after, 3), 60))
        except KeyboardInterrupt:
            LOG.info("Stopped by operator")
            break
        except Exception:
            LOG.exception("Unexpected error in poll loop")
            time.sleep(5)


if __name__ == "__main__":
    main()
