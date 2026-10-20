#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram account manager with private web sign-in. Python 3.10+.

Admin commands: /login +number, /scan, /chat ID, /send TEXT, /status.
One-time HTTPS sign-in pages replace sending Telegram codes to a bot.
Telethon login session is saved locally; Render needs a persistent disk.
"""

from __future__ import annotations

# ======================= SETTINGS =======================
# Rotate any BotFather token that has been shared in screenshots or chats.
BOT_TOKEN = ""  # Set BOT_TOKEN in Render -> Environment
ADMIN_ID = 8357023784
TG_API_ID = 9619937
TG_API_HASH = "e00356ba07ec98314a5c6abcc0ec7899"
DB_PATH = "business_chats.sqlite3"
# Render Web Service requires a bound PORT (usually 10000).
# Set PORT=0 when deploying as a Render Background Worker.
DEFAULT_PORT = 10000
OFFER_TTL_HOURS = 6
USE_PREMIUM_EMOJI = True
GIFT_EMOJI_ID = "5931409969613116639"
STAR_EMOJI_ID = "5920281855378068765"
# ========================================================

import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LOG = logging.getLogger("business_manager")
MAX_TEXT = 4096
REPLY_WINDOW = 24 * 60 * 60
API_ROOT = "https://api.telegram.org/bot"


NFT_PATTERN = re.compile(r"([A-Za-z][A-Za-z0-9_]{0,55})-(\d{1,12})\Z")
OFFER_CALLBACK = re.compile(r"^offer:(yes|no):(\d{1,15})$")


@dataclass(frozen=True)
class NFTOffer:
    stars: int
    url: str
    name: str
    number: str


def parse_offer(text: str) -> NFTOffer:
    parts = text.strip().split()
    if len(parts) != 3 or parts[0].lower() != ".mq":
        raise ValueError("Формат: .mq 5000 https://t.me/nft/DurovsCap-1234")
    if not parts[1].isascii() or not parts[1].isdecimal():
        raise ValueError("Количество звёзд должно быть целым числом.")
    stars = int(parts[1])
    if not 1 <= stars <= 1_000_000:
        raise ValueError("Допустимо от 1 до 1 000 000 звёзд.")
    try:
        url = urlsplit(parts[2])
        if url.port is not None:
            raise ValueError("Invalid URL port")
    except ValueError as exc:
        raise ValueError("Недопустимая NFT-ссылка.") from exc
    if (url.scheme != "https" or url.hostname not in {"t.me", "www.t.me"}
            or url.username or url.password or url.query or url.fragment
            or not url.path.startswith("/nft/")):
        raise ValueError("Нужна ссылка формата https://t.me/nft/Name-1234")
    match = NFT_PATTERN.fullmatch(url.path[len("/nft/"):])
    if not match:
        raise ValueError("Некорректный NFT. Пример: https://t.me/nft/DurovsCap-1234")
    return NFTOffer(stars, f"https://t.me/nft/{match.group(0)}", match.group(1), match.group(2))


def gift_icon(custom: bool) -> str:
    return (f'<tg-emoji emoji-id="{GIFT_EMOJI_ID}">🎁</tg-emoji>'
            if custom else "🎁")


def star_icon(custom: bool) -> str:
    return (f'<tg-emoji emoji-id="{STAR_EMOJI_ID}">⭐</tg-emoji>'
            if custom else "⭐")


def offer_name(offer: dict) -> str:
    return f"{escape(offer['name'])} #{escape(offer['number'])}"


def remaining_time(expires_at: int) -> str:
    minutes = (max(0, expires_at - int(time.time())) + 59) // 60
    hours, mins = divmod(minutes, 60)
    return f"{hours} ч. {mins} мин."


def render_offer(offer: dict, custom: bool = False) -> str:
    """Template preserving the offer layout without claiming a settled transfer."""
    name = offer_name(offer)
    link = f'<a href="{escape(offer["url"], quote=True)}">{name}</a>'
    return (
        "<b>оффер на NFT-подарок</b>\n\n"
        f"{gift_icon(custom)} <b>{name}</b>\n"
        f'<a href="{escape(offer["url"], quote=True)}">ПОКАЗАТЬ ПОДАРОК</a>\n\n'
        f"Пользователь предлагает Вам <b>{offer['stars']} Звёзд</b> "
        f"за подарок {link}.\n\n"
        f"Предложение действует ещё <b>{remaining_time(offer['expires_at'])}</b>\n\n"
        "<i>Это предложение, а не подтверждение оплаты. "
        "Звёзды не переводились.</i>\n\n"
        "чтобы принять напишите <b>принять</b> чтобы отклонить напишите <b>отклонить</b>"
    )


def render_final(offer: dict, custom: bool = False) -> str:
    name = offer_name(offer)
    link = f'<a href="{escape(offer["url"], quote=True)}">{name}</a>'
    if offer["status"] == "accepted":
        return (
            "<b>предложение было принято!</b>\n\n"
            f"{gift_icon(custom)} Подарок {link} · "
            f"<b>{offer['stars']} {star_icon(custom)}</b>\n\n"
            "Подтверждён только интерес к предложению. "
            "Оплата не производилась, NFT не передавался. "
            "Для обмена используйте официальный механизм Telegram."
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
    raise ValueError("Неизвестный статус оффера")


def offer_keyboard(offer_id: int, url: str) -> dict:
    return {"inline_keyboard": [
        [{"text": "Принять", "callback_data": f"offer:yes:{offer_id}"},
         {"text": "Отклонить", "callback_data": f"offer:no:{offer_id}"}],
        [{"text": "ПОКАЗАТЬ ПОДАРОК", "url": url}],
    ]}


class TelegramError(Exception):
    def __init__(self, method: str, code: int, description: str, retry_after: int = 0):
        super().__init__(f"{method}: {code} {description}")
        self.method = method
        self.code = code
        self.description = description
        self.retry_after = retry_after


class BotAPI:
    def __init__(self, token: str):
        self.url = API_ROOT + token + "/"

    def call(self, method: str, **params):
        request = Request(
            self.url + method,
            data=json.dumps(params, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=45) as response:
                result = json.load(response)
        except HTTPError as exc:
            try:
                result = json.loads(exc.read().decode("utf-8"))
            except (ValueError, UnicodeError) as error:
                raise TelegramError(method, exc.code, exc.reason) from error
        except (URLError, OSError, TimeoutError) as exc:
            raise TelegramError(method, 0, str(exc)) from exc
        if not result.get("ok", False):
            params = result.get("parameters") or {}
            raise TelegramError(
                method, result.get("error_code", 0),
                result.get("description", "Telegram API error"),
                int(params.get("retry_after", 0)),
            )
        return result.get("result")


class Store:
    def __init__(self, path: str):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(p), timeout=20, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=20000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS connections (
                id TEXT PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                enabled INTEGER NOT NULL,
                can_reply INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chats (
                connection_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                last_incoming INTEGER NOT NULL DEFAULT 0,
                last_seen INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (connection_id, chat_id)
            );
            CREATE INDEX IF NOT EXISTS chats_last_seen_idx ON chats(last_seen DESC);
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
                rendered INTEGER NOT NULL DEFAULT 0,
                UNIQUE(connection_id, chat_id, command_id)
            );
            CREATE INDEX IF NOT EXISTS expiry_idx ON offers(status, expires_at);
            CREATE TABLE IF NOT EXISTS operator_state (
                admin_id INTEGER PRIMARY KEY,
                selected_connection TEXT,
                selected_chat INTEGER,
                mode TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS settings (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)

    def one(self, query: str, params: tuple = ()) -> dict | None:
        row = self.db.execute(query, params).fetchone()
        return dict(row) if row else None

    def all(self, query: str, params: tuple = ()) -> list[dict]:
        return [dict(row) for row in self.db.execute(query, params)]

    def offset(self) -> int:
        item = self.one("SELECT value FROM settings WHERE name='update_offset'")
        return int(item["value"]) if item else 0

    def set_offset(self, value: int):
        self.db.execute("""INSERT INTO settings(name,value) VALUES('update_offset',?)
            ON CONFLICT(name) DO UPDATE SET value=excluded.value""", (str(value),))

    def upsert_connection(self, conn: dict):
        rights = conn.get("rights") or {}
        can_reply = rights.get("can_reply", conn.get("can_reply", False))
        self.db.execute("""INSERT INTO connections(id,owner_id,enabled,can_reply,updated_at)
            VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            owner_id=excluded.owner_id, enabled=excluded.enabled,
            can_reply=excluded.can_reply, updated_at=excluded.updated_at""",
            (str(conn["id"]), int(conn["user"]["id"]),
             int(bool(conn.get("is_enabled"))), int(bool(can_reply)), int(time.time())))

    def upsert_chat(self, connection_id: str, chat_id: int, title: str,
                    last_incoming: int, last_seen: int):
        self.db.execute("""INSERT INTO chats
            (connection_id, chat_id, title, last_incoming, last_seen)
            VALUES (?,?,?,?,?) ON CONFLICT(connection_id,chat_id) DO UPDATE SET
            title=excluded.title,
            last_incoming=MAX(chats.last_incoming, excluded.last_incoming),
            last_seen=MAX(chats.last_seen, excluded.last_seen)""",
            (connection_id, chat_id, title[:120], last_incoming, last_seen))

    def chats(self, admin_id: int) -> list[dict]:
        return self.all("""SELECT c.*, k.enabled, k.can_reply FROM chats c
            JOIN connections k ON k.id=c.connection_id
            WHERE k.owner_id=? ORDER BY c.last_seen DESC LIMIT 300""", (admin_id,))

    def chat(self, admin_id: int, chat_id: int) -> dict | None:
        return self.one("""SELECT c.*, k.enabled, k.can_reply FROM chats c
            JOIN connections k ON k.id=c.connection_id
            WHERE k.owner_id=? AND c.chat_id=? ORDER BY c.last_seen DESC LIMIT 1""",
            (admin_id, chat_id))

    def create_offer(self, connection_id: str, owner_id: int, chat_id: int,
                     command_id: int, nft: NFTOffer) -> tuple[int, bool]:
        now = int(time.time())
        cur = self.db.execute("""INSERT OR IGNORE INTO offers
            (connection_id, owner_id, chat_id, command_id, name, number, url,
             stars, created_at, expires_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (connection_id, owner_id, chat_id, command_id, nft.name, nft.number,
             nft.url, nft.stars, now, now + OFFER_TTL_HOURS * 3600))
        if cur.rowcount:
            return int(cur.lastrowid), True
        item = self.one("""SELECT id FROM offers
            WHERE connection_id=? AND chat_id=? AND command_id=?""",
            (connection_id, chat_id, command_id))
        return int(item["id"]), False

    def offer(self, offer_id: int) -> dict | None:
        return self.one("SELECT * FROM offers WHERE id=?", (offer_id,))

    def latest_pending(self, connection_id: str, chat_id: int) -> dict | None:
        return self.one("""SELECT * FROM offers
            WHERE connection_id=? AND chat_id=? AND status='pending'
            ORDER BY id DESC LIMIT 1""", (connection_id, chat_id))

    def set_sent(self, offer_id: int, message_id: int):
        self.db.execute("""UPDATE offers SET status='pending', sent_message_id=?
            WHERE id=? AND status='creating'""", (message_id, offer_id))

    def set_failed(self, offer_id: int):
        self.db.execute("UPDATE offers SET status='failed' WHERE id=?", (offer_id,))

    def change_status(self, offer_id: int, status: str) -> bool:
        if status not in ('accepted', 'declined', 'expired'):
            raise ValueError('Invalid offer status')
        now = int(time.time())
        check = "expires_at<=?" if status == "expired" else "expires_at>?"
        cur = self.db.execute(
            f"UPDATE offers SET status=?,rendered=0 WHERE id=? "
            f"AND status='pending' AND {check}", (status, offer_id, now))
        return bool(cur.rowcount)

    def set_rendered(self, offer_id: int):
        self.db.execute("UPDATE offers SET rendered=1 WHERE id=?", (offer_id,))

    def expire_due(self):
        now = int(time.time())
        self.db.execute("""UPDATE offers SET status='expired',rendered=0
            WHERE status='pending' AND expires_at<=?""", (now,))

    def needs_render(self) -> list[dict]:
        return self.all("""SELECT * FROM offers
            WHERE status IN ('accepted', 'declined', 'expired')
            AND rendered=0 LIMIT 100""")

    def state(self, admin_id: int) -> dict:
        return self.one("SELECT * FROM operator_state WHERE admin_id=?", (admin_id,)) or {
            "admin_id": admin_id, "selected_connection": None,
            "selected_chat": None, "mode": "",
        }

    def set_state(self, admin_id: int, *, connection: str | None = None,
                  chat: int | None = None, mode: str = ""):
        self.db.execute("""INSERT INTO operator_state
            (admin_id, selected_connection, selected_chat, mode) VALUES (?,?,?,?)
            ON CONFLICT(admin_id) DO UPDATE SET
            selected_connection=excluded.selected_connection,
            selected_chat=excluded.selected_chat, mode=excluded.mode""",
            (admin_id, connection, chat, mode))


class Manager:
    def __init__(self, api: BotAPI, store: Store, admin_id: int):
        self.api = api
        self.store = store
        self.admin_id = admin_id

    def send_admin(self, text: str, markup: dict | None = None):
        args = {"chat_id": self.admin_id, "text": text,
                "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
        if markup is not None:
            args["reply_markup"] = markup
        return self.api.call("sendMessage", **args)

    @staticmethod
    def menu() -> dict:
        return {"inline_keyboard": [
            [{"text": "Скан чатов", "callback_data": "scan:0"}],
            [{"text": "Ввести ID", "callback_data": "enter_id"},
             {"text": "Отправить сообщение", "callback_data": "compose"}],
            [{"text": "Создать NFT-оффер", "callback_data": "offer_help"}],
            [{"text": "Статус", "callback_data": "status"}],
        ]}

    def refresh_connection(self, cid: str) -> dict | None:
        item = self.store.one("SELECT * FROM connections WHERE id=?", (cid,))
        if item:
            return item
        try:
            remote = self.api.call("getBusinessConnection", business_connection_id=cid)
        except TelegramError as exc:
            LOG.warning("Cannot read business connection: %s", exc.description)
            return None
        if not remote or int(remote["user"]["id"]) != self.admin_id:
            return None
        self.store.upsert_connection(remote)
        return self.store.one("SELECT * FROM connections WHERE id=?", (cid,))

    def business_connection(self, connection: dict):
        if int(connection["user"]["id"]) != self.admin_id:
            return
        self.store.upsert_connection(connection)
        enabled = bool(connection.get("is_enabled"))
        try:
            text = (
                "✅ Business-бот подключён.\n"
                "Команда: <code>.mq 5000 https://t.me/nft/DurovsCap-1234</code>\n"
                "фейк скам ботаники"
                if enabled else "🔌 Business-бот отключён."
            )
            self.send_admin(text, self.menu())
        except TelegramError as exc:
            LOG.warning("Admin must start the bot with /start: %s", exc.description)

    @staticmethod
    def chat_title(chat: dict, sender: dict) -> str:
        title = chat.get("title")
        if title:
            return str(title)
        name = " ".join(p for p in (chat.get("first_name"), chat.get("last_name")) if p)
        name = name or " ".join(p for p in (sender.get("first_name"), sender.get("last_name")) if p)
        username = chat.get("username") or sender.get("username")
        if not name and username:
            name = "@" + str(username)
        return name or "Диалог"

    def business_message(self, msg: dict):
        cid = msg.get("business_connection_id")
        chat = msg.get("chat") or {}
        if not cid or chat.get("type") != "private" or not chat.get("id"):
            return
        connection = self.refresh_connection(str(cid))
        if not connection or not connection["enabled"]:
            return
        sender = msg.get("from") or {}
        owner_id = int(connection["owner_id"])
        incoming = bool(sender.get("id") and int(sender["id"]) != owner_id)
        stamp = int(msg.get("date") or time.time())
        self.store.upsert_chat(
            str(cid), int(chat["id"]), self.chat_title(chat, sender),
            stamp if incoming else 0, stamp,
        )
        text = str(msg.get("text") or "").strip()
        if incoming and text.lower() in ("принять", "отклонить"):
            pending = self.store.latest_pending(str(cid), int(chat["id"]))
            if pending:
                self._settle_offer(pending, "accepted" if text.lower() == "принять"
                                   else "declined")
        elif (not incoming and re.match(r"^\.mq(?:\s|$)", text, re.I)
              and int(sender.get("id") or 0) == owner_id):
            try:
                nft = parse_offer(text)
            except ValueError as exc:
                self.send_admin("Ошибка .mq: " + escape(str(exc)))
                return
            found = self.store.chat(self.admin_id, int(chat["id"]))
            if found:
                self._send_offer(found, nft, int(msg["message_id"]), business_command=True)

    @staticmethod
    def usable(chat: dict) -> bool:
        return (bool(chat["enabled"]) and bool(chat["can_reply"])
                and int(chat["last_incoming"]) > int(time.time()) - REPLY_WINDOW)

    def show_chats(self, page: int = 0):
        items = self.store.chats(self.admin_id)
        if not items:
            self.send_admin(
                "Пока нет доступных диалогов.\n\n"
                "Подключите бота в Telegram Business, разрешите ответы и "
                "дождитесь входящего сообщения в разрешённом чате. "
                "Telegram не предоставляет ботам полный список личных чатов.",
                self.menu(),
            )
            return
        page_size = 8
        total_pages = (len(items) + page_size - 1) // page_size
        page = min(max(page, 0), total_pages - 1)
        excerpt = items[page * page_size:(page + 1) * page_size]
        buttons = []
        lines = [f"<b>Найденные Business-диалоги: {len(items)}</b>",
                 f"Страница {page + 1}/{total_pages}\n"]
        for idx, c in enumerate(excerpt, page * page_size + 1):
            ready = self.usable(c)
            mark = "●" if ready else "○"
            lines.append(f"{mark} {idx}. {escape(c['title'])} — <code>{c['chat_id']}</code>")
            buttons.append([{"text": ("● " if ready else "○ ") +
                            c["title"][:30] + " · " + str(c["chat_id"]),
                            "callback_data": "select:" + str(c["chat_id"])}])
        lines.append("\n● Можно отправить ответ. ○ Нет права ответа или "
                     "не было входящих сообщений за последние 24 часа.")
        paging = []
        if page > 0:
            paging.append({"text": "◀", "callback_data": f"scan:{page - 1}"})
        if page + 1 < total_pages:
            paging.append({"text": "▶", "callback_data": f"scan:{page + 1}"})
        if paging:
            buttons.append(paging)
        buttons.append([{"text": "Обновить", "callback_data": f"scan:{page}"},
                        {"text": "Ввести ID", "callback_data": "enter_id"}])
        self.send_admin("\n".join(lines), {"inline_keyboard": buttons})

    def selected_chat(self) -> dict | None:
        state = self.store.state(self.admin_id)
        chat_id = state.get("selected_chat")
        if chat_id is None:
            return None
        chat = self.store.chat(self.admin_id, int(chat_id))
        if chat and chat["connection_id"] == state.get("selected_connection"):
            return chat
        return None

    def select_chat(self, chat_id: int):
        chat = self.store.chat(self.admin_id, chat_id)
        if chat is None:
            self.send_admin("Этот ID не найден среди доступных Business-диалогов. "
                            "Чат должен появиться после входящего сообщения в разрешённом диалоге.")
            return
        self.store.set_state(self.admin_id, connection=chat["connection_id"],
                             chat=chat_id)
        usable = self.usable(chat)
        self.send_admin(
            f"Выбран чат: <b>{escape(chat['title'])}</b>\n"
            f"ID: <code>{chat_id}</code>\n"
            f"Доступ к ответу: {'есть' if usable else 'нет'}\n\n"
            "Команда: <code>/send Привет! Это сообщение из Business-бота.</code>",
            {"inline_keyboard": [
                [{"text": "Отправить сообщение", "callback_data": "compose"}],
                [{"text": "Создать NFT-оффер", "callback_data": "offer_help"}],
                [{"text": "Скан чатов", "callback_data": "scan:0"}],
            ]},
        )

    def send_to_chat(self, chat: dict, message: str):
        if not message.strip():
            self.send_admin("Текст сообщения пуст.")
            return
        if len(message) > MAX_TEXT:
            self.send_admin("Слишком длинное сообщение, максимум 4096 символов.")
            return
        if not self.usable(chat):
            self.send_admin(
                "Telegram не разрешает ответ в этот чат. Убедитесь, что "
                "Business-подключение активно, есть право can_reply и "
                "собеседник написал вам за последние 24 часа.")
            return
        try:
            self.api.call("sendMessage", business_connection_id=chat["connection_id"],
                          chat_id=int(chat["chat_id"]), text=message,
                          link_preview_options={"is_disabled": True})
        except TelegramError as exc:
            error = exc.description
            if "PEER_ID_INVALID" in error.upper() or "CHAT NOT FOUND" in error.upper():
                self.send_admin(
                    "Telegram отклонил ID чата (PEER_ID_INVALID / chat not found). "
                    "ID должен принадлежать доступному Business-диалогу. "
                    "Повторное введение произвольного ID эту ошибку не исправит.")
            else:
                self.send_admin(f"Не удалось отправить: <code>{escape(error)}</code>")
            return
        self.send_admin(f"Сообщение отправлено в <b>{escape(chat['title'])}</b> "
                        f"(<code>{chat['chat_id']}</code>).", self.menu())

    def show_status(self):
        connections = self.store.all("SELECT * FROM connections WHERE owner_id=?",
                                     (self.admin_id,))
        chats = self.store.chats(self.admin_id)
        available = sum(self.usable(c) for c in chats)
        chosen = self.selected_chat()
        self.send_admin(
            f"Подключений: {len(connections)}\n"
            f"Разрешённых диалогов в истории бота: {len(chats)}\n"
            f"Доступных для ответа сейчас: {available}\n"
            f"Выбранный чат: {escape(chosen['title']) if chosen else 'нет'}",
            self.menu(),
        )

    def _send_offer(self, chat: dict, nft: NFTOffer, command_id: int,
                    business_command: bool = False):
        if not self.usable(chat):
            self.send_admin(
                "Нельзя отправить оффер в этот чат: отсутствует право ответа "
                "или входящее сообщение за последние 24 часа.")
            return
        cid = str(chat["connection_id"])
        chat_id = int(chat["chat_id"])
        item_id, fresh = self.store.create_offer(cid, self.admin_id, chat_id,
                                                 command_id, nft)
        if not fresh:
            self.send_admin("Этот оффер уже обрабатывался.")
            return
        obj = self.store.offer(item_id)
        sent = None
        last_error = None
        for custom in ([True, False] if USE_PREMIUM_EMOJI else [False]):
            for preview in (True, False):
                args = {
                    "business_connection_id": cid, "chat_id": chat_id,
                    "text": render_offer(obj, custom), "parse_mode": "HTML",
                    "reply_markup": offer_keyboard(item_id, nft.url),
                    "link_preview_options": ({"url": nft.url, "prefer_large_media": True,
                                              "show_above_text": True} if preview
                                             else {"is_disabled": True}),
                }
                try:
                    sent = self.api.call("sendMessage", **args)
                    break
                except TelegramError as exc:
                    last_error = exc
                    detail = exc.description.lower()
                    if preview and any(x in detail for x in ("preview", "media")):
                        continue
                    if custom and any(x in detail for x in ("emoji", "entities")):
                        break
                    break
            if sent:
                break
            if last_error and not (custom and any(
                    x in last_error.description.lower() for x in ("emoji", "entities"))):
                break
        if sent:
            self.store.set_sent(item_id, int(sent["message_id"]))
            if business_command:
                try:
                    self.api.call("deleteBusinessMessages", business_connection_id=cid,
                                  message_ids=[command_id])
                except TelegramError as exc:
                    LOG.warning("Cannot delete the command message: %s", exc.description)
            self.send_admin(f"NFT-оффер отправлен в чат <code>{chat_id}</code>.")
        else:
            self.store.set_failed(item_id)
            self.send_admin("Не удалось отправить оффер: <code>"
                            + escape(last_error.description if last_error else "ошибка")
                            + "</code>")

    def _private_offer(self, raw: str, message_id: int):
        parts = raw.split()
        if len(parts) not in (3, 4):
            self.send_admin("Формат: <code>.mq 5000 "
                            "https://t.me/nft/DurovsCap-1234 [CHAT_ID]</code>")
            return
        try:
            nft = parse_offer(" ".join(parts[:3]))
        except ValueError as exc:
            self.send_admin("Ошибка: " + escape(str(exc)))
            return
        if len(parts) == 4:
            if not re.fullmatch(r"-?\d{1,20}", parts[3]):
                self.send_admin("CHAT_ID должен быть числовым.")
                return
            chat = self.store.chat(self.admin_id, int(parts[3]))
        else:
            chat = self.selected_chat()
        if not chat:
            self.send_admin("Чат не найден. Нажмите «Скан чатов» и выберите чат.")
            return
        state = self.store.state(self.admin_id)
        self.store.set_state(self.admin_id, connection=state["selected_connection"],
                             chat=state["selected_chat"])
        self._send_offer(chat, nft, message_id)

    def _update_offer(self, obj: dict):
        args = {
            "business_connection_id": obj["connection_id"],
            "chat_id": obj["chat_id"],
            "message_id": obj["sent_message_id"],
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
            "reply_markup": {"inline_keyboard": []},
        }
        for custom in ([True, False] if USE_PREMIUM_EMOJI else [False]):
            try:
                self.api.call("editMessageText", text=render_final(obj, custom), **args)
                self.store.set_rendered(obj["id"])
                return
            except TelegramError as exc:
                if "message is not modified" in exc.description.lower():
                    self.store.set_rendered(obj["id"])
                    return
                if not custom or not any(x in exc.description.lower()
                                         for x in ("emoji", "entities")):
                    raise

    def _settle_offer(self, obj: dict, status: str):
        if int(time.time()) >= obj["expires_at"]:
            status = "expired"
        if self.store.change_status(obj["id"], status):
            try:
                self._update_offer(self.store.offer(obj["id"]))
            except TelegramError as exc:
                LOG.warning("Could not update offer %s: %s", obj["id"], exc.description)
            return True
        return False

    def housekeeping(self):
        self.store.expire_due()
        for obj in self.store.needs_render():
            try:
                self._update_offer(obj)
            except TelegramError as exc:
                LOG.warning("Retry offer update %s: %s", obj["id"], exc.description)

    def private_message(self, msg: dict):
        sender_id = (msg.get("from") or {}).get("id")
        if sender_id != self.admin_id or (msg.get("chat") or {}).get("type") != "private":
            return
        raw = msg.get("text")
        if not isinstance(raw, str):
            return
        text = raw.strip()
        command, _, rest = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        rest = rest.strip()
        if command in ("/start", "/help", "/menu"):
            self.send_admin(
                "<b>Telegram Business · менеджер чатов</b>\n\n"
                "Бот видит только диалоги, доступные через ваше Business-подключение.\n"
                "1. Подключите бота в Telegram Business и включите право отвечать.\n"
                "2. После входящего сообщения нажмите «Скан чатов».\n"
                "3. Выберите чат и отправьте текст через /send или кнопку.\n\n"
                "Команды: /scan, /chat ID, /send текст, /status.\n"
                "NFT-оффер: <code>.mq 5000 https://t.me/nft/DurovsCap-1234</code> "
                "в выбранный чат или с CHAT_ID в конце.",
                self.menu(),
            )
        elif command == "/scan":
            self.show_chats()
        elif command == ".mq":
            self._private_offer(text, int(msg["message_id"]))
        elif command == "/status":
            self.show_status()
        elif command in ("/login", "/code"):
            self.send_admin(
                "Вход по коду Telegram здесь не используется. "
                "Не отправляйте коды авторизации в боты. "
                "Доступ подтверждается вашим Telegram ID и "
                "подключением в официальных настройках Telegram Business.")
        elif command in ("/chat", "/select"):
            if not re.fullmatch(r"-?\d{1,20}", rest):
                self.send_admin("Формат: <code>/chat 123456789</code>")
            else:
                self.select_chat(int(rest))
        elif command == "/send":
            match = re.match(r"^(-?\d{1,20})\s+([\s\S]+)$", rest)
            if match:
                chat = self.store.chat(self.admin_id, int(match.group(1)))
                if not chat:
                    self.send_admin("ID отсутствует в списке доступных чатов. Запустите /scan.")
                    return
                self.send_to_chat(chat, match.group(2))
            else:
                chat = self.selected_chat()
                if not chat:
                    self.send_admin("Сначала выберите чат через /scan или /chat ID.")
                elif not rest:
                    self.send_admin("Формат: <code>/send Текст сообщения</code>")
                else:
                    self.send_to_chat(chat, rest)
        elif not text.startswith("/"):
            state = self.store.state(self.admin_id)
            mode = state.get("mode")
            if mode == "await_chat_id":
                if re.fullmatch(r"-?\d{1,20}", text):
                    self.select_chat(int(text))
                else:
                    self.send_admin("Введите только числовой ID чата.")
            elif mode == "await_offer":
                self._private_offer(text, int(msg["message_id"]))
            elif mode == "await_message":
                chat = self.selected_chat()
                if not chat:
                    self.send_admin("Чат не выбран. Нажмите «Скан чатов».")
                else:
                    self.store.set_state(self.admin_id, connection=chat["connection_id"],
                                         chat=chat["chat_id"])
                    self.send_to_chat(chat, text)

    def callback(self, cb: dict):
        offer_action = OFFER_CALLBACK.fullmatch(str(cb.get("data") or ""))
        if offer_action:
            decision, offer_id = offer_action.groups()
            offer = self.store.offer(int(offer_id))
            actor = int((cb.get("from") or {}).get("id") or 0)
            source = cb.get("message") or {}
            source_chat_id = int((source.get("chat") or {}).get("id") or 0)
            source_msg_id = int(source.get("message_id") or 0)
            response = "Предложение недоступно."
            if (offer and actor == offer["chat_id"]
                    and source_chat_id == offer["chat_id"]
                    and source_msg_id == offer["sent_message_id"]):
                state = "accepted" if decision == "yes" else "declined"
                response = ("Предложение отмечено как принято, оплата не проведена."
                            if self._settle_offer(offer, state)
                            else "Предложение уже обработано или истекло.")
            try:
                self.api.call("answerCallbackQuery", callback_query_id=cb["id"],
                              text=response, show_alert=True)
            except TelegramError:
                LOG.warning("Could not answer offer callback")
            return
        if (cb.get("from") or {}).get("id") != self.admin_id:
            return
        callback_id = cb.get("id")
        try:
            self.api.call("answerCallbackQuery", callback_query_id=callback_id)
        except TelegramError:
            LOG.warning("Could not answer callback query")
        action = str(cb.get("data") or "")
        if action.startswith("scan:") and re.fullmatch(r"scan:\d{1,3}", action):
            self.show_chats(int(action.split(":", 1)[1]))
        elif action.startswith("select:") and re.fullmatch(r"select:-?\d{1,20}", action):
            self.select_chat(int(action.split(":", 1)[1]))
        elif action == "enter_id":
            state = self.store.state(self.admin_id)
            self.store.set_state(self.admin_id,
                                 connection=state["selected_connection"],
                                 chat=state["selected_chat"], mode="await_chat_id")
            self.send_admin("Отправьте числовой ID чата, который уже доступен боту.")
        elif action == "compose":
            chat = self.selected_chat()
            if not chat:
                self.send_admin("Сначала выберите чат через «Скан чатов».", self.menu())
                return
            self.store.set_state(self.admin_id, connection=chat["connection_id"],
                                 chat=chat["chat_id"], mode="await_message")
            self.send_admin(f"Напишите сообщение для <b>{escape(chat['title'])}</b> "
                            f"(<code>{chat['chat_id']}</code>).")
        elif action == "offer_help":
            state = self.store.state(self.admin_id)
            self.store.set_state(self.admin_id, connection=state["selected_connection"],
                                 chat=state["selected_chat"], mode="await_offer")
            self.send_admin("Введите команду:\n<code>.mq 5000 "
                            "https://t.me/nft/DurovsCap-1234</code>\n\n"
                            "Выберите чат заранее или добавьте CHAT_ID в конце. "
                            "Оффер не подтверждает оплату.")
        elif action == "status":
            self.show_status()

    def process(self, update: dict):
        if "business_connection" in update:
            self.business_connection(update["business_connection"])
        elif "business_message" in update:
            self.business_message(update["business_message"])
        elif "edited_business_message" in update:
            self.business_message(update["edited_business_message"])
        elif "message" in update:
            self.private_message(update["message"])
        elif "callback_query" in update:
            self.callback(update["callback_query"])


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/health", "/healthz"):
            self.send_error(404)
            return
        body = b'{"ok":true,"service":"telegram-business-manager"}\n'
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        LOG.debug("HTTP: " + format, *args)


def start_health_server():
    try:
        port = int(os.getenv("PORT", str(DEFAULT_PORT)))
    except ValueError as exc:
        raise SystemExit("PORT должен быть числом") from exc
    if port == 0:
        LOG.info("Health server disabled: PORT=0")
        return
    if not 1 <= port <= 65535:
        raise SystemExit("Недопустимый PORT")
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    LOG.info("Health endpoint listening on PORT=%s", port)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("BOT_TOKEN", "").strip()
    try:
        admin_id = int(os.getenv("ADMIN_ID", str(ADMIN_ID)))
    except ValueError as exc:
        raise SystemExit("ADMIN_ID должен быть числом") from exc
    if not token or ":" not in token:
        raise SystemExit("Установите новый BOT_TOKEN в настройках или переменной окружения.")
    if admin_id <= 0:
        raise SystemExit("ADMIN_ID должен быть положительным числом.")

    start_health_server()
    api = BotAPI(token)
    store = Store(os.getenv("DB_PATH", DB_PATH))
    manager = Manager(api, store, admin_id)
    info = api.call("getMe")
    LOG.info("Started @%s", info.get("username"))
    api.call("deleteWebhook", drop_pending_updates=False)
    offset = store.offset()
    while True:
        try:
            updates = api.call(
                "getUpdates", offset=offset, timeout=25,
                allowed_updates=["message", "business_connection", "business_message",
                                 "edited_business_message", "callback_query"],
            )
            for update in updates:
                update_id = int(update["update_id"])
                if update_id < offset:
                    continue
                try:
                    manager.process(update)
                except Exception:
                    LOG.exception("Error processing update %s", update_id)
                finally:
                    offset = update_id + 1
                    store.set_offset(offset)
            manager.housekeeping()
        except TelegramError as exc:
            LOG.warning("Telegram API: %s", exc.description)
            time.sleep(min(max(exc.retry_after, 3), 60))
        except KeyboardInterrupt:
            LOG.info("Stopped")
            break
        except Exception:
            LOG.exception("Polling failure")
            time.sleep(5)



# ==================== PERSONAL TELEGRAM ACCOUNT ====================
# The existing Business-mode offer templates above are intentionally unchanged.

import asyncio
import getpass
import sys
import secrets
from urllib.parse import parse_qs


def personal_credentials() -> tuple[int, str]:
    raw_id = os.getenv("TG_API_ID", str(TG_API_ID)).strip()
    secret = os.getenv("TG_API_HASH", TG_API_HASH).strip()
    if not raw_id.isdecimal() or not secret:
        raise SystemExit("Проверьте TG_API_ID и TG_API_HASH в настройках bot.py.")
    return int(raw_id), secret


def personal_telethon():
    try:
        from telethon import TelegramClient
    except ImportError:
        import subprocess
        print("Telethon не найден. Устанавливаю telethon>=1.42,<2 ...", flush=True)
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "telethon>=1.42,<2"])
            from telethon import TelegramClient
        except (subprocess.CalledProcessError, ImportError) as exc:
            raise SystemExit("Не удалось установить Telethon. Проверьте доступ к pip.") from exc
    return TelegramClient


def session_source() -> str:
    """Local SQLite session file, not a required environment string."""
    source = os.getenv("TG_SESSION_PATH", "telegram_account").strip() or "telegram_account"
    path = Path(source).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


async def local_account_login():
    """Authorize locally; never submit Telegram verification codes to a bot."""
    api_id, api_hash = personal_credentials()
    TelegramClient = personal_telethon()
    client = TelegramClient(session_source(), api_id, api_hash)
    try:
        phone = os.getenv("TG_PHONE", "").strip() or input("Номер Telegram (+...): ").strip()
        if not re.fullmatch(r"\+[1-9][0-9]{5,14}", phone):
            raise SystemExit("Номер указывается в международном формате: +79991234567")
        await client.start(
            phone=phone,
            code_callback=lambda: re.sub(r"\D", "", input("Код Telegram: ")),
            password=lambda: getpass.getpass("Пароль двухэтапной проверки: "),
        )
        me = await client.get_me()
        if me is None or getattr(me, "bot", False):
            raise SystemExit("Авторизуйтесь в обычном аккаунте, не в боте.")
        filename = Path(session_source())
        if filename.suffix != ".session":
            filename = filename.with_name(filename.name + ".session")
        try:
            os.chmod(filename, 0o600)
        except OSError:
            pass
        print(f"Вход выполнен. Аккаунт: {me.id}")
        print(f"Сессия сохранена: {filename.resolve()}")
        print("Храните файл в закрытой папке. Для Render необходим постоянный диск (Persistent Disk).")
    finally:
        await client.disconnect()


class PersonalChatManager:
    """Single-operator bridge: Telegram bot commands -> own Telegram client."""

    PAGE_SIZE = 7
    MAX_DIALOGS = 300

    def __init__(self, api: BotAPI, store: Store, owner_id: int, client):
        self.api = api
        self.store = store
        self.owner_id = owner_id
        self.client = client
        self.dialogs: dict[int, object] = {}
        self.dialog_order: list[int] = []
        self.selected: int | None = None
        self.mode = ""
        self.login_flow: dict | None = None
        self.last_login_request = 0.0

    async def bot_call(self, method: str, **params):
        return await asyncio.to_thread(self.api.call, method, **params)

    async def message(self, text: str, buttons: dict | None = None):
        args = {
            "chat_id": self.owner_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if buttons is not None:
            args["reply_markup"] = buttons
        return await self.bot_call("sendMessage", **args)

    @staticmethod
    def login_url(token: str) -> str | None:
        base = (os.getenv("PUBLIC_BASE_URL", "").strip()
                or os.getenv("RENDER_EXTERNAL_URL", "").strip())
        if not base:
            hostname = os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip()
            if hostname:
                base = "https://" + hostname
        base = base.rstrip("/")
        # Never send authorization links over cleartext HTTP.
        if not base.startswith("https://") or any(x in base for x in ("?", "#", "@")):
            return None
        return base + "/auth/" + token

    async def begin_login(self, phone: str):
        if not re.fullmatch(r"\+[1-9][0-9]{5,14}", phone):
            await self.message("Формат: <code>/login +79991234567</code>")
            return
        if await self.client.is_user_authorized():
            me = await self.client.get_me()
            await self.message(f"Аккаунт уже подключён: <code>{me.id}</code>. Откройте «Скан чатов».", self.menu())
            return
        if time.monotonic() - self.last_login_request < 60:
            await self.message("Повторный запрос кода возможен не чаще одного раза в минуту.")
            return
        self.last_login_request = time.monotonic()
        try:
            sent = await self.client.send_code_request(phone)
        except Exception as exc:
            LOG.warning("Telegram login code request failed: %s", type(exc).__name__)
            await self.message("Не удалось запросить код: " + escape(str(exc))[:250])
            return
        token = secrets.token_urlsafe(32)
        self.login_flow = {
            "token": token, "phone": phone, "phone_code_hash": sent.phone_code_hash,
            "stage": "code", "expires": time.monotonic() + 600, "attempts": 0,
        }
        url = self.login_url(token)
        if url:
            await self.message(
                "Код Telegram отправлен. Откройте одноразовую страницу для подтверждения входа. "
                "Не пересылайте ссылку другим. Она действительна 10 минут.\n\n"
                f'<a href="{escape(url, quote=True)}">Открыть защищённую страницу входа</a>',
                {"inline_keyboard": [[{"text": "Открыть страницу входа", "url": url}]]},
            )
        else:
            await self.message(
                "Код отправлен. Откройте HTTPS-адрес своего Render-сервиса "
                "и добавьте следующий путь:\n"
                f"<code>/auth/{escape(token)}</code>\n\n"
                "Для автоматической кнопки задайте PUBLIC_BASE_URL "
                "равным HTTPS-адресу сервиса. Ссылка действует 10 минут."
            )

    async def get_login_stage(self, token: str) -> str | None:
        flow = self.login_flow
        if not flow or not secrets.compare_digest(str(flow["token"]), token):
            return None
        if time.monotonic() > flow["expires"]:
            self.login_flow = None
            return None
        return str(flow["stage"])

    async def submit_login(self, token: str, submitted: str) -> tuple[str, str]:
        flow = self.login_flow
        if not flow or not secrets.compare_digest(str(flow["token"]), token):
            return "invalid", "Ссылка недействительна. Запросите /login заново."
        if time.monotonic() > flow["expires"] or flow["attempts"] >= 5:
            self.login_flow = None
            return "invalid", "Время подтверждения вышло. Запросите /login заново."
        flow["attempts"] += 1
        stage = flow["stage"]
        try:
            if stage == "code":
                code = re.sub(r"\D", "", submitted)
                if not 4 <= len(code) <= 8:
                    return "code", "Введите код из сообщения Telegram. Точки и пробелы допускаются."
                await self.client.sign_in(
                    phone=flow["phone"], code=code,
                    phone_code_hash=flow["phone_code_hash"])
            elif stage == "password":
                if not 1 <= len(submitted) <= 256:
                    return "password", "Введите пароль двухэтапной проверки."
                await self.client.sign_in(password=submitted)
            else:
                return "invalid", "Ссылка больше не действует."
        except Exception as exc:
            from telethon.errors import (SessionPasswordNeededError, PhoneCodeInvalidError,
                                         PhoneCodeExpiredError, PasswordHashInvalidError,
                                         FloodWaitError)
            if isinstance(exc, SessionPasswordNeededError):
                flow["stage"] = "password"
                flow["attempts"] = 0
                return "password", "Теперь введите облачный пароль Telegram."
            if isinstance(exc, (PhoneCodeInvalidError, PasswordHashInvalidError)):
                if flow["attempts"] >= 5:
                    self.login_flow = None
                    return "invalid", "Слишком много попыток. Повторите /login."
                return stage, "Неверный код или пароль. Попробуйте снова."
            if isinstance(exc, PhoneCodeExpiredError):
                self.login_flow = None
                return "invalid", "Код просрочен. Запросите /login заново."
            if isinstance(exc, FloodWaitError):
                self.login_flow = None
                return "invalid", f"Telegram ограничил вход. Повторите через {exc.seconds} сек."
            LOG.warning("Telegram sign-in failed: %s", type(exc).__name__)
            return stage, "Ошибка авторизации. Проверьте данные и попробуйте снова."
        me = await self.client.get_me()
        if me is None or getattr(me, "bot", False):
            self.login_flow = None
            return "invalid", "Не удалось подтвердить обычный аккаунт."
        self.login_flow = None
        file = Path(session_source())
        if file.suffix != ".session":
            file = file.with_name(file.name + ".session")
        try:
            os.chmod(file, 0o600)
        except OSError:
            pass
        try:
            await self.message(
                f"Аккаунт <code>{me.id}</code> подключён. Нажмите «Скан чатов».", self.menu())
        except Exception:
            LOG.warning("Login completed, but bot DM notification failed")
        return "success", "Аккаунт подключён. Вернитесь в бот и нажмите «Скан чатов»."

    @staticmethod
    def menu():
        return {"inline_keyboard": [
            [{"text": "Скан чатов", "callback_data": "user:scan:0"}],
            [{"text": "Ввести ID", "callback_data": "user:enter"},
             {"text": "Отправить сообщение", "callback_data": "user:write"}],
            [{"text": "Статус", "callback_data": "user:status"}],
        ]}

    async def scan(self, page: int = 0, *, refresh: bool = True):
        if refresh or not self.dialog_order:
            found: dict[int, object] = {}
            order: list[int] = []
            try:
                async for dialog in self.client.iter_dialogs(limit=self.MAX_DIALOGS):
                    if dialog.id not in found:
                        found[dialog.id] = dialog
                        order.append(dialog.id)
            except Exception as exc:
                LOG.exception("Cannot enumerate account dialogs")
                await self.message(f"Ошибка сканирования: {escape(str(exc))}")
                return
            self.dialogs = found
            self.dialog_order = order
        page = max(0, min(page, max(0, (len(self.dialog_order) - 1) // self.PAGE_SIZE)))
        if not self.dialog_order:
            await self.message("Диалоги не найдены.", self.menu())
            return
        start = page * self.PAGE_SIZE
        chunk = self.dialog_order[start:start + self.PAGE_SIZE]
        rows = []
        for ident in chunk:
            dialog = self.dialogs[ident]
            name = str(dialog.name or "Диалог").replace("\n", " ")[:35]
            rows.append([{"text": name, "callback_data": f"user:select:{ident}"}])
        navigation = []
        if page > 0:
            navigation.append({"text": "◀ Назад", "callback_data": f"user:scan:{page-1}"})
        if start + self.PAGE_SIZE < len(self.dialog_order):
            navigation.append({"text": "Далее ▶", "callback_data": f"user:scan:{page+1}"})
        if navigation:
            rows.append(navigation)
        rows.append([{"text": "Обновить список", "callback_data": "user:refresh"}])
        rows.append([{"text": "Меню", "callback_data": "user:menu"}])
        lines = [f"<b>Чаты аккаунта: {len(self.dialog_order)}</b> (показаны до {self.MAX_DIALOGS})"]
        lines.extend(f"{escape(str(self.dialogs[i].name or 'Диалог'))[:80]} — <code>{i}</code>" for i in chunk)
        await self.message("\n".join(lines), {"inline_keyboard": rows})

    async def select(self, ident: int):
        dialog = self.dialogs.get(ident)
        if dialog is None:
            await self.message("Нет этого ID в списке. Нажмите «Скан чатов». Хитрый произвольный ID здесь не сработает.")
            return
        self.selected = ident
        self.mode = ""
        await self.message(f"Выбран: <b>{escape(str(dialog.name))}</b> · <code>{ident}</code>", self.menu())

    async def send_to(self, ident: int, text: str):
        dialog = self.dialogs.get(ident)
        if dialog is None:
            await self.message("Чат не найден. Сначала нажмите «Скан чатов».")
            return
        if not text.strip() or len(text) > MAX_TEXT:
            await self.message("Введите сообщение длиной от 1 до 4096 символов.")
            return
        try:
            await self.client.send_message(dialog.input_entity, text, link_preview=False)
        except Exception as exc:
            LOG.warning("Cannot send to peer %s: %s", ident, type(exc).__name__)
            await self.message("Telegram не разрешил отправку: " + escape(str(exc))[:350])
            return
        self.mode = ""
        await self.message(f"Отправлено в <b>{escape(str(dialog.name))}</b> (<code>{ident}</code>).", self.menu())

    async def send_offer(self, raw: str):
        """Existing offer text is reused unchanged, without payment impersonation."""
        parts = raw.split()
        if len(parts) not in (3, 4):
            await self.message("Формат: <code>.mq 5000 https://t.me/nft/DurovsCap-1234 [CHAT_ID]</code>")
            return
        try:
            nft = parse_offer(" ".join(parts[:3]))
        except ValueError as exc:
            await self.message(escape(str(exc)))
            return
        ident = self.selected
        if len(parts) == 4:
            if not re.fullmatch(r"-?\d{1,20}", parts[3]):
                await self.message("CHAT_ID должен быть числом.")
                return
            ident = int(parts[3])
        if ident is None or ident not in self.dialogs:
            await self.message("Выберите чат через кнопку «Скан чатов».")
            return
        obj = {
            "name": nft.name, "number": nft.number, "url": nft.url,
            "stars": nft.stars, "expires_at": int(time.time()) + OFFER_TTL_HOURS * 3600,
        }
        dialog = self.dialogs[ident]
        try:
            await self.client.send_message(dialog.input_entity, render_offer(obj, False), parse_mode="html", link_preview=False)
        except Exception as exc:
            await self.message("Не удалось отправить: " + escape(str(exc))[:350])
            return
        await self.message(f"Оффер отправлен в <code>{ident}</code>. Кнопки inline поддерживаются только ботами, не обычными аккаунтами.")

    async def private_message(self, data: dict):
        sender = data.get("from") or {}
        chat = data.get("chat") or {}
        if int(sender.get("id") or 0) != self.owner_id or chat.get("type") != "private":
            return
        raw = data.get("text")
        if not isinstance(raw, str):
            return
        raw = raw.strip()
        command, _, argument = raw.partition(" ")
        command = command.split("@", 1)[0].lower()
        argument = argument.strip()
        if command in ("/code", "/password"):
            try:
                await self.bot_call("deleteMessage", chat_id=self.owner_id,
                                    message_id=data["message_id"])
            except Exception:
                pass
            await self.message("Коды и пароли не отправляйте в бот. "
                               "Для входа используйте одноразовую HTTPS-страницу, "
                               "ссылку на которую выдаёт /login +номер.")
            return
        if command == "/login":
            await self.begin_login(argument)
            return
        if not await self.client.is_user_authorized():
            await self.message(
                "Аккаунт ещё не подключён. Введите <code>/login +79991234567</code>. "
                "Бот запросит код Telegram и отправит одноразовую ссылку на страницу "
                "подтверждения. Войдите через неё, затем используйте «Скан чатов»."
            )
            return
        if command in ("/start", "/menu", "/help"):
            await self.message(
                "<b>Telegram · управление своим аккаунтом</b>\n\n"
                "Кнопка «Скан чатов» показывает диалоги обычного Telegram-аккаунта.\n"
                "Выберите чат и отправляйте сообщение через /send Текст.\n"
                "Команды: /login +номер, /scan, /chat ID, /send Текст, /send ID Текст, /status.",
                self.menu(),
            )
        elif command == "/scan":
            await self.scan(refresh=True)
        elif command in ("/chat", "/select"):
            if not re.fullmatch(r"-?\d{1,20}", argument):
                await self.message("Формат: <code>/chat 123456789</code>")
            else:
                await self.select(int(argument))
        elif command == "/send":
            match = re.fullmatch(r"(-?\d{1,20})\s+([\s\S]+)", argument)
            if match:
                await self.send_to(int(match.group(1)), match.group(2))
            elif self.selected is None:
                await self.message("Сначала выберите чат через /scan или /chat ID.")
            else:
                await self.send_to(self.selected, argument)
        elif command == ".mq":
            await self.send_offer(raw)
        elif command == "/status":
            me = await self.client.get_me()
            await self.message(
                f"Аккаунт: <code>{me.id}</code>\n"
                f"Найдено диалогов: {len(self.dialog_order)}\n"
                f"Выбранный чат: <code>{self.selected}</code>", self.menu())
        elif not raw.startswith("/"):
            if self.mode == "id":
                if re.fullmatch(r"-?\d{1,20}", raw):
                    await self.select(int(raw))
                else:
                    await self.message("Отправьте числовой ID.")
            elif self.mode == "write":
                if self.selected is None:
                    await self.message("Сначала выберите чат через /scan.")
                else:
                    await self.send_to(self.selected, raw)

    async def callback(self, cb: dict):
        if int((cb.get("from") or {}).get("id") or 0) != self.owner_id:
            return
        query = cb.get("id")
        if query:
            try:
                await self.bot_call("answerCallbackQuery", callback_query_id=query)
            except Exception:
                pass
        if not await self.client.is_user_authorized():
            await self.message("Сначала выполните <code>/login +номер</code> и подтвердите вход на одноразовой странице.")
            return
        action = str(cb.get("data") or "")
        if action == "user:menu":
            await self.message("Выберите действие:", self.menu())
        elif action == "user:refresh":
            await self.scan(refresh=True)
        elif re.fullmatch(r"user:scan:\d{1,4}", action):
            await self.scan(page=int(action.rsplit(":", 1)[-1]), refresh=False)
        elif re.fullmatch(r"user:select:-?\d{1,20}", action):
            await self.select(int(action.rsplit(":", 1)[-1]))
        elif action == "user:enter":
            self.mode = "id"
            await self.message("Введите ID из списка доступных диалогов.")
        elif action == "user:write":
            if self.selected is None:
                await self.message("Сначала выберите чат через «Скан чатов». ", self.menu())
            else:
                self.mode = "write"
                await self.message("Напишите текст для отправки в выбранный диалог.")
        elif action == "user:status":
            await self.private_message({"from": {"id": self.owner_id},
                                        "chat": {"type": "private"}, "text": "/status"})

    async def process(self, update: dict):
        if "message" in update:
            await self.private_message(update["message"])
        elif "callback_query" in update:
            await self.callback(update["callback_query"])


class PersonalLoginHandler(BaseHTTPRequestHandler):
    # One-time-link login portal. HTTPS terminated by Render proxy.

    def log_message(self, fmt, *args):
        # Never log paths containing auth tokens or confidential POST bodies.
        pass

    def respond(self, status: int, html_text: str, *, cookie: str | None = None):
        body = html_text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def html_page(self, stage: str, token: str, alert: str = "") -> str:
        safe_alert = escape(alert)
        if stage == "success":
            form = '<div class="ok">Вход подтверждён. Вернитесь в Telegram-бот.</div>'
        elif stage == "invalid":
            form = '<div class="error">Ссылка истекла или недействительна. Отправьте /login в бот.</div>'
        else:
            password = stage == "password"
            label = "Облачный пароль Telegram" if password else "Код из Telegram"
            placeholder = "Введите пароль" if password else "Например, 1.2.3.4.5"
            typ = "password" if password else "text"
            name = "secret"
            form = (f'<form method="post" action="/auth/{escape(token, quote=True)}">'
                    f'<label>{label}</label>'
                    f'<input name="{name}" type="{typ}" autocomplete="off" '
                    f'placeholder="{placeholder}" minlength="1" maxlength="256" required autofocus>'
                    '<button type="submit">Подтвердить вход</button></form>')
        alert_html = f'<div class="note">{safe_alert}</div>' if alert else ""
        return f'''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Авторизация своего Telegram-аккаунта</title>
<style>
:root{{color-scheme:dark}}*{{box-sizing:border-box}}
body{{background:#10151f;color:#e8ecf3;font-family:system-ui,Arial,sans-serif;display:grid;place-items:center;min-height:100vh;margin:0;padding:18px}}
main{{max-width:450px;width:100%;background:#192231;border:1px solid #374456;border-radius:20px;padding:30px;box-shadow:0 10px 40px #0004}}
h1{{font-size:23px;margin:0 0 15px}}p{{line-height:1.5;color:#aabbd0;font-size:14px}}
label{{display:block;font-weight:600;margin:24px 0 9px}}
input{{width:100%;padding:14px;border-radius:10px;background:#121923;border:1px solid #586779;color:white;font-size:17px}}
button{{width:100%;padding:14px;border:0;border-radius:10px;background:#3899ee;color:#07101d;font-weight:750;font-size:16px;margin-top:18px;cursor:pointer}}
.note,.ok,.error{{margin-top:17px;padding:12px;border-radius:9px;background:#273e56;font-size:14px}}
.ok{{background:#17392d}}.error{{background:#512629}}
small{{display:block;margin-top:24px;color:#95a9c3;line-height:1.6}}
</style></head><body><main><h1>Подключение Telegram-аккаунта</h1>
<p>Приватная страница вашего собственного менеджера чатов. Не является официальным сайтом Telegram. Не пересылайте эту ссылку.</p>
{alert_html}{form}
<small>Эта страница действует 10 минут. Введённые данные передаются только вашему серверу через HTTPS и не сохраняются в базе или переписке с ботом.</small>
</main></body></html>'''

    def get_token(self):
        match = re.fullmatch(r"/auth/([A-Za-z0-9_-]{35,70})", urlsplit(self.path).path)
        return match.group(1) if match else None

    def call_loop(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.server.bot_loop).result(timeout=45)

    def do_GET(self):
        if urlsplit(self.path).path in ("/", "/health", "/healthz"):
            body = b'{"ok":true,"service":"telegram-personal-manager"}\n'
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        token = self.get_token()
        if token is None:
            self.send_error(404)
            return
        try:
            stage = self.call_loop(self.server.login_manager.get_login_stage(token))
        except Exception:
            self.send_error(503)
            return
        self.respond(200 if stage else 410,
                     self.html_page(stage or "invalid", token))

    def do_POST(self):
        token = self.get_token()
        if token is None:
            self.send_error(404)
            return
        # Explicit same-origin check against off-site form submissions.
        origin = self.headers.get("Origin", "")
        host = self.headers.get("Host", "")
        if origin and origin not in ("https://" + host, "http://127.0.0.1:" + host.split(":")[-1], "http://localhost:" + host.split(":")[-1]):
            self.send_error(403)
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
            self.send_error(415)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if not 0 < size <= 1024:
            self.send_error(413)
            return
        try:
            body = self.rfile.read(size).decode("utf-8", "strict")
            payload = parse_qs(body, keep_blank_values=True)
            value = payload.get("secret", [""])[0]
            stage, note = self.call_loop(self.server.login_manager.submit_login(token, value))
        except Exception:
            LOG.exception("Login page processing failed (credentials redacted)")
            self.send_error(503)
            return
        self.respond(200 if stage != "invalid" else 410, self.html_page(stage, token, note))


def start_personal_web_server(manager: PersonalChatManager, loop):
    try:
        port = int(os.getenv("PORT", str(DEFAULT_PORT)))
    except ValueError as exc:
        raise SystemExit("PORT должен быть числом") from exc
    if port == 0:
        raise SystemExit("Для HTTPS-страницы авторизации Render Web Service нужен PORT, например 10000.")
    if not 1 <= port <= 65535:
        raise SystemExit("Недопустимый PORT")
    server = ThreadingHTTPServer(("0.0.0.0", port), PersonalLoginHandler)
    server.daemon_threads = True
    server.login_manager = manager
    server.bot_loop = loop
    threading.Thread(target=server.serve_forever, daemon=True, name="login-portal").start()
    LOG.info("Health and private login web server running on port %s", port)
    return server


async def run_personal_account():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    api_id, api_hash = personal_credentials()
    TelegramClient = personal_telethon()
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token or ":" not in token:
        raise SystemExit("Укажите BOT_TOKEN, полученный у BotFather.")
    try:
        admin = int(os.getenv("ADMIN_ID", str(ADMIN_ID)))
    except ValueError as exc:
        raise SystemExit("ADMIN_ID должен быть числом.") from exc
    if admin <= 0:
        raise SystemExit("Укажите ADMIN_ID.")
    client = TelegramClient(session_source(), api_id, api_hash)
    await client.connect()
    try:
        me = await client.get_me()
        if me is not None and getattr(me, "bot", False):
            raise SystemExit("Нужна сессия обычного аккаунта, не бота.")
        if me is None:
            LOG.info("Аккаунт не авторизован: отправьте /login +номер администраторскому боту.")
        api = BotAPI(token)
        store = Store(os.getenv("DB_PATH", DB_PATH))
        manager = PersonalChatManager(api, store, admin, client)
        web_server = start_personal_web_server(manager, asyncio.get_running_loop())
        info = await manager.bot_call("getMe")
        if me:
            LOG.info("Bot @%s manages personal Telegram account %s", info.get("username"), me.id)
        else:
            LOG.info("Bot @%s ждёт авторизации личного Telegram-аккаунта", info.get("username"))
        await manager.bot_call("deleteWebhook", drop_pending_updates=False)
        offset = store.offset()
        while True:
            try:
                updates = await manager.bot_call(
                    "getUpdates", offset=offset, timeout=25,
                    allowed_updates=["message", "callback_query"],
                )
                for update in updates:
                    index = int(update["update_id"])
                    if index < offset:
                        continue
                    try:
                        await manager.process(update)
                    except Exception:
                        LOG.exception("Failed to process update %s", index)
                    finally:
                        offset = index + 1
                        store.set_offset(offset)
            except TelegramError as exc:
                LOG.warning("Telegram Bot API: %s", exc.description)
                await asyncio.sleep(min(max(3, exc.retry_after), 60))
            except KeyboardInterrupt:
                break
            except Exception:
                LOG.exception("Telegram polling error")
                await asyncio.sleep(5)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].lower() == "auth":
        asyncio.run(local_account_login())
    else:
        asyncio.run(run_personal_account())
