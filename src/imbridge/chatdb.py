"""Read Messages' database: new messages (tapbacks and threaded replies included), chats, and history.

Reading ~/Library/Messages/chat.db needs Full Disk Access for whatever runs Python (your terminal app, or the Python
binary itself for a background service). The database is opened read-only.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .addresses import address_key, display_address
from .config import CHAT_DB
from .reactions import Reaction, parse_reaction
from .typedstream import attributed_body_text

APPLE_EPOCH = 978_307_200  # 2001-01-01T00:00:00Z as a Unix timestamp
GROUP_STYLE = 43  # chat.style for group chats (45 is one-to-one)


class FullDiskAccessError(PermissionError):
    """chat.db can't be opened: the process running Python needs Full Disk Access."""


def apple_time(value: int | None) -> datetime | None:
    if not value:
        return None
    seconds = value / 1e9 if value > 1e11 else value  # nanoseconds since macOS 10.13, seconds before that
    return datetime.fromtimestamp(seconds + APPLE_EPOCH, tz=timezone.utc)


@dataclass(frozen=True)
class Attachment:
    guid: str
    path: str | None  # on disk, once downloaded
    mime_type: str | None
    name: str | None


@dataclass(frozen=True)
class Message:
    rowid: int
    guid: str
    chat_guid: str | None
    sender: str | None  # phone number or email of whoever sent it; None when you did
    is_from_me: bool
    text: str | None
    date: datetime | None
    service: str | None  # "iMessage", "SMS", "RCS"
    is_group: bool = False
    chat_name: str | None = None  # a group's name, if it has one
    reply_to: str | None = None  # guid of the message this is an inline reply to
    reaction: Reaction | None = None  # set when this row is a tapback or sticker
    attachments: tuple[Attachment, ...] = ()
    address: str | None = None  # which of your addresses it was sent to (or, for your own messages, sent from)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["date"] = self.date.isoformat() if self.date else None
        return data


@dataclass(frozen=True)
class ChatInfo:
    guid: str
    identifier: str | None  # the handle for one-to-one chats, chatNNN for groups
    name: str | None
    service: str | None
    is_group: bool
    participants: tuple[str, ...]
    last_message_at: datetime | None
    address: str | None = None  # which of your addresses the conversation is on (what Messages sends from)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["last_message_at"] = self.last_message_at.isoformat() if self.last_message_at else None
        return data


class ChatDB:
    def __init__(self, path: Path | str = CHAT_DB) -> None:
        self.path = Path(path)
        self._db: sqlite3.Connection | None = None
        self._select = ""
        self._last_activity = ""
        self._chat_address = "NULL"  # chat.last_addressed_handle, where this macOS has it
        self._message_address = "NULL"  # message.destination_caller_id, likewise

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def max_rowid(self) -> int:
        return self._query("SELECT max(ROWID) FROM message")[0][0] or 0

    def messages_after(self, rowid: int, limit: int = 500) -> list[Message]:
        """Messages newer than rowid, oldest first."""
        return self._messages("m.ROWID > ? AND m.item_type = 0 ORDER BY m.ROWID LIMIT ?", (rowid, limit))

    def message(self, guid: str) -> Message | None:
        found = self._messages("m.guid = ?", (guid,))
        return found[0] if found else None

    def history(self, chat_guid: str, limit: int = 50) -> list[Message]:
        """The chat's latest messages, oldest first."""
        found = self._messages("c.guid = ? AND m.item_type = 0 ORDER BY m.ROWID DESC LIMIT ?", (chat_guid, limit))
        return found[::-1]

    def chats(self, limit: int = 50) -> list[ChatInfo]:
        """Chats, most recently active first."""
        return self._chats("ORDER BY last_date DESC LIMIT ?", (limit,))

    def chat(self, guid: str) -> ChatInfo | None:
        found = self._chats("WHERE c.guid = ? LIMIT 1", (guid,))
        return found[0] if found else None

    def _chats(self, clause: str, params: tuple) -> list[ChatInfo]:
        self._connect()
        rows = self._query(
            "SELECT c.ROWID AS rowid, c.guid, c.chat_identifier, c.display_name, c.service_name, c.style,"
            f" {self._chat_address} AS address, {self._last_activity} AS last_date FROM chat c {clause}",
            params,
        )
        chats = []
        for row in rows:
            participants = tuple(
                handle
                for (handle,) in self._query(
                    "SELECT h.id FROM chat_handle_join j JOIN handle h ON h.ROWID = j.handle_id WHERE j.chat_id = ?",
                    (row["rowid"],),
                )
            )
            chats.append(
                ChatInfo(
                    guid=row["guid"],
                    identifier=row["chat_identifier"],
                    name=row["display_name"] or None,
                    service=row["service_name"],
                    is_group=row["style"] == GROUP_STYLE,
                    participants=participants,
                    last_message_at=apple_time(row["last_date"]),
                    address=display_address(row["address"]),
                )
            )
        return chats

    def my_addresses(self, days: int = 90) -> list[str]:
        """Your addresses that Messages used here in the last `days` days: on messages, and on chats active since."""
        self._connect()
        since = int((time.time() - APPLE_EPOCH - days * 86_400) * 1e9)
        values = [row[0] for row in self._query(
            f"SELECT DISTINCT {self._message_address} FROM message WHERE date > ?", (since,)
        )]
        values += [row[0] for row in self._query(
            f"SELECT DISTINCT {self._chat_address} FROM chat c WHERE {self._last_activity} > ?", (since,)
        )]
        found: dict[str, str] = {}
        for value in values:
            key, shown = address_key(value), display_address(value)
            if key and (key not in found or shown.startswith("+")):  # prefer "+14805550100" over "14805550100"
                found[key] = shown
        return list(found.values())

    def chat_for_handle(self, handle: str) -> str | None:
        """The most recently active one-to-one chat with a phone number or email, if there is one."""
        handle = handle.strip()
        self._connect()
        rows = self._query(
            f"SELECT c.guid, c.chat_identifier, {self._last_activity} AS last_date FROM chat c"
            f" WHERE c.style != {GROUP_STYLE} ORDER BY last_date DESC"
        )
        if "@" in handle:
            matches = [row for row in rows if (row["chat_identifier"] or "").lower() == handle.lower()]
        else:
            digits = re.sub(r"\D", "", handle)[-10:]
            if len(digits) < 7:
                return None
            matches = [row for row in rows if re.sub(r"\D", "", row["chat_identifier"] or "").endswith(digits)]
        return matches[0]["guid"] if matches else None

    def chats_named(self, name: str) -> list[str]:
        """GUIDs of the chats with this name (a group's display name), ignoring case."""
        rows = self._query(
            "SELECT guid FROM chat WHERE display_name != '' AND lower(display_name) = lower(?)", (name.strip(),)
        )
        return list(dict.fromkeys(row["guid"] for row in rows))

    def _connect(self) -> sqlite3.Connection:
        if self._db is None:
            try:
                db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5, check_same_thread=False)
                db.row_factory = sqlite3.Row
                message_columns = {row[1] for row in db.execute("PRAGMA table_info(message)")}
                join_columns = {row[1] for row in db.execute("PRAGMA table_info(chat_message_join)")}
                chat_columns = {row[1] for row in db.execute("PRAGMA table_info(chat)")}
            except sqlite3.OperationalError as e:
                raise FullDiskAccessError(
                    f"can't read {self.path} ({e}). Give Full Disk Access to the app running Python: "
                    "System Settings > Privacy & Security > Full Disk Access."
                ) from e
            if not message_columns:
                raise FullDiskAccessError(f"{self.path} has no message table (is this Messages' chat.db?)")
            self._select = _message_select(message_columns)
            if "last_addressed_handle" in chat_columns:
                self._chat_address = "c.last_addressed_handle"
            if "destination_caller_id" in message_columns:
                self._message_address = "destination_caller_id"
            self._last_activity = (
                "(SELECT max(j.message_date) FROM chat_message_join j WHERE j.chat_id = c.ROWID)"
                if "message_date" in join_columns
                else "(SELECT max(m.date) FROM chat_message_join j JOIN message m ON m.ROWID = j.message_id"
                " WHERE j.chat_id = c.ROWID)"
            )
            self._db = db
        return self._db

    def _query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        # Messages recreates its WAL files when it restarts; if our handle went stale, reconnect once.
        for attempt in (1, 2):
            db = self._connect()
            try:
                return db.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                self.close()
                if attempt == 2:
                    raise
        return []

    def _messages(self, where: str, params: tuple) -> list[Message]:
        self._connect()
        rows = self._query(f"{self._select} WHERE {where}", params)
        attachments = self._attachments([row["rowid"] for row in rows if row["cache_has_attachments"]])
        messages = []
        for row in rows:
            text = row["text"] or attributed_body_text(row["attributedBody"])
            text = (text or "").replace("￼", "").strip() or None  # U+FFFC stands in for attachments
            messages.append(
                Message(
                    rowid=row["rowid"],
                    guid=row["guid"],
                    chat_guid=row["chat_guid"],
                    sender=None if row["is_from_me"] else row["sender"],
                    is_from_me=bool(row["is_from_me"]),
                    text=text,
                    date=apple_time(row["date"]),
                    service=row["service"],
                    is_group=row["chat_style"] == GROUP_STYLE,
                    chat_name=row["chat_name"] or None,
                    reply_to=row["thread_originator_guid"] or None,
                    reaction=parse_reaction(
                        row["associated_message_type"], row["associated_message_guid"], row["associated_message_emoji"]
                    ),
                    address=display_address(row["destination_caller_id"]),
                    attachments=attachments.get(row["rowid"], ()),
                )
            )
        return messages

    def _attachments(self, rowids: list[int]) -> dict[int, tuple[Attachment, ...]]:
        if not rowids:
            return {}
        marks = ",".join("?" * len(rowids))
        rows = self._query(
            "SELECT j.message_id, a.guid, a.filename, a.mime_type, a.transfer_name FROM message_attachment_join j"
            f" JOIN attachment a ON a.ROWID = j.attachment_id WHERE j.message_id IN ({marks})",
            rowids,
        )
        found: dict[int, list[Attachment]] = {}
        for row in rows:
            path = str(Path(row["filename"]).expanduser()) if row["filename"] else None
            found.setdefault(row["message_id"], []).append(
                Attachment(row["guid"], path, row["mime_type"], row["transfer_name"])
            )
        return {rowid: tuple(items) for rowid, items in found.items()}


def _message_select(columns: set[str]) -> str:
    # Older macOS versions lack some of these columns; select NULL in their place.
    def column(name: str) -> str:
        return f"m.{name}" if name in columns else f"NULL AS {name}"

    return (
        "SELECT m.ROWID AS rowid, m.guid, m.text, m.attributedBody, m.is_from_me, m.date, m.service,"
        f" m.cache_has_attachments, {column('associated_message_guid')}, {column('associated_message_type')},"
        f" {column('associated_message_emoji')}, {column('thread_originator_guid')}, {column('destination_caller_id')},"
        " h.id AS sender, c.guid AS chat_guid, c.display_name AS chat_name, c.style AS chat_style"
        " FROM message m"
        " LEFT JOIN handle h ON h.ROWID = m.handle_id"
        " LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID"
        " LEFT JOIN chat c ON c.ROWID = cmj.chat_id"
    )
