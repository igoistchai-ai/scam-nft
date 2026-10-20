#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram Business chat manager. Python 3.10+, standard library only.

The bot indexes only chats received through authorized Telegram Business
updates. Telegram Bot API cannot list all chats of a personal account.

Commands (admin only):
/start, /scan, /chat CHAT_ID, /send MESSAGE, /send CHAT_ID MESSAGE, /status
.mq STARS NFT_URL [CHAT_ID] creates an unpaid NFT offer in an authorized chat.

No Telegram account verification codes are collected or stored.
"""

from __future__ import annotations

# ======================= SETTINGS =======================
# Rotate any BotFather token that has been shared in screenshots or chats.
BOT_TOKEN = "8731504349:AAGTIGZz3nsBdLMF94zQiP5zsQLHF4wgsEA"
ADMIN_ID = 8357023784
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
        rights = connection.get("rights") or {}
        reply = bool(rights.get("can_reply", connection.get("can_reply", False)))
        try:
            self.send_admin(
                ("Business-бот подключён." if enabled else "Business-бот отключён.")
                + ("\nПраво ответа: включено." if reply else "\nПраво ответа: выключено.")
                + "\nНажмите «Скан чатов» для просмотра доступных диалогов.",
                self.menu(),
            )
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
    token = os.getenv("BOT_TOKEN", BOT_TOKEN).strip()
    try:
        admin_id = int(os.getenv("ADMIN_ID", str(ADMIN_ID)))
    except ValueError as exc:
        raise SystemExit("ADMIN_ID должен быть числом") from exc
    if not token or token == "PASTE_NEW_BOT_TOKEN_HERE" or ":" not in token:
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


if __name__ == "__main__":
    main()
