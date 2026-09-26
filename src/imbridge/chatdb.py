"""Read Messages' database: new messages (tapbacks and threaded replies included), chats, and history.

Reading ~/Library/Messages/chat.db needs Full Disk Access for whatever runs Python (your terminal app, or the Python
binary itself for a background service). The database is opened read-only.
"""

from __future__ import annotations

import plistlib
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .addresses import address_key, display_address, same_address
from .config import CHAT_DB
from .polls import POLL_TYPES, POLLS_BUNDLE, Poll, PollOption, PollResults, PollVote, fallback_text, parse_poll
from .reactions import Reaction, parse_reaction
from .typedstream import attributed_body_mentions, attributed_body_text

APPLE_EPOCH = 978_307_200  # 2001-01-01T00:00:00Z as a Unix timestamp
GROUP_STYLE = 43  # chat.style for group chats (45 is one-to-one)
EVENT_TYPES = (1, 2, 3)  # message.item_type: someone added or removed, the group renamed, a group action
_EVENT_KINDS = {(1, 0): "added", (1, 1): "removed", (3, 0): "left", (3, 1): "photo_changed", (3, 2): "photo_removed"}


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
class GroupEvent:
    """A change to a group, on the Message that records it. Its sender is who made the change (None: you)."""

    kind: str  # "added", "removed", "left", "renamed", "photo_changed", "photo_removed", or "other"
    person: str | None = None  # who was added, removed, or left (None: you)
    name: str | None = None  # for "renamed", the new name (None when the name was cleared)
    code: tuple[int, int] = (0, 0)  # chat.db's (item_type, group_action_type): tells apart kinds imbridge doesn't name


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
    edited_at: datetime | None = None  # when it was last edited; text is the edited text
    edit_count: int = 0  # how many times it has been edited
    unsent_at: datetime | None = None  # when its sender took it back (unsent); text is then None
    mentions: tuple[str, ...] = ()  # the phone numbers and emails it @mentions
    event: GroupEvent | None = None  # set when this row records a change to a group rather than a message
    poll: Poll | None = None  # set when this message is a poll, or an update adding a choice to one
    vote: PollVote | None = None  # set when this message is a vote in a poll
    scheduled_for: datetime | None = None  # for your own message waiting in Send Later: when it goes out

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["date"] = self.date.isoformat() if self.date else None
        data["edited_at"] = self.edited_at.isoformat() if self.edited_at else None
        data["unsent_at"] = self.unsent_at.isoformat() if self.unsent_at else None
        data["scheduled_for"] = self.scheduled_for.isoformat() if self.scheduled_for else None
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
        self._edited = "0"  # message.date_edited, likewise
        self._retracted = "0"  # message.date_retracted, likewise
        self._message_address = "NULL"  # message.destination_caller_id, likewise
        self._has_polls = False  # message.balloon_bundle_id and payload_data, likewise
        self._has_schedules = False  # message.schedule_type, schedule_state and is_delivered, likewise

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def max_rowid(self) -> int:
        return self._query("SELECT max(ROWID) FROM message")[0][0] or 0

    def messages_after(self, rowid: int, limit: int = 500, *, events: bool = False) -> list[Message]:
        """Messages newer than rowid, oldest first; with events, group changes too."""
        return self._messages(f"m.ROWID > ? AND {_kinds(events)} ORDER BY m.ROWID LIMIT ?", (rowid, limit))

    def message_at(self, rowid: int) -> Message | None:
        found = self._messages("m.ROWID = ?", (rowid,), fallbacks=True)
        return found[0] if found else None

    def recent_floor(self, seconds: float) -> int:
        """The ROWID just before the first message dated in the last `seconds` (everything after it is recent)."""
        since = int((time.time() - APPLE_EPOCH - seconds) * 1e9)
        first = self._query("SELECT min(ROWID) FROM message WHERE date > ?", (since,))[0][0]
        return first - 1 if first else self.max_rowid()

    def change_marks(self, after_rowid: int) -> list[tuple[int, int, int, int]]:
        """(ROWID, date, date_edited, date_retracted) of the messages after a ROWID: what edits and unsends touch."""
        self._connect()
        columns = f"ROWID, date, {self._edited}, {self._retracted}"
        rows = self._query(f"SELECT {columns} FROM message WHERE ROWID > ?", (after_rowid,))
        return [(row[0], row[1] or 0, row[2] or 0, row[3] or 0) for row in rows]

    def message(self, guid: str) -> Message | None:
        found = self._messages("m.guid = ?", (guid,), fallbacks=True)
        return found[0] if found else None

    def history(self, chat_guid: str, limit: int = 50, *, events: bool = False) -> list[Message]:
        """The chat's latest messages, oldest first; with events, group changes too."""
        found = self._messages(f"c.guid = ? AND {_kinds(events)} ORDER BY m.ROWID DESC LIMIT ?", (chat_guid, limit))
        return found[::-1]

    def scheduled(self, chat_guid: str | None = None) -> list[Message]:
        """Your messages waiting in Send Later (in one chat, or all), soonest first."""
        self._connect()
        if not self._has_schedules:
            return []
        where = "m.is_from_me = 1 AND m.schedule_type = 2 AND m.is_delivered = 0"
        if chat_guid is None:
            return self._messages(f"{where} ORDER BY m.date", ())
        return self._messages(f"{where} AND c.guid = ? ORDER BY m.date", (chat_guid,))

    def schedule_state(self, guid: str) -> int | None:
        """A Send Later message's schedule_state: 1 while it's on its way to Apple's servers, 2 once they hold it
        (only then does cancelling stick), 3 once sent. None if there's no such message or no such column."""
        self._connect()
        if not self._has_schedules:
            return None
        rows = self._query("SELECT schedule_state FROM message WHERE guid = ?", (guid,))
        return rows[0][0] if rows else None

    def poll(self, guid: str, *, mine: Iterable[str] = ()) -> PollResults | None:
        """The current state of a poll, from the poll's GUID, an update's, or a vote's. Votes sent from any address
        in `mine` (address_key form) are yours, like the ones marked as from you."""
        found = self.message(guid)
        item = found and (found.poll or found.vote)
        if item is None or not self._has_polls:
            return None
        where, params = "m.balloon_bundle_id = ?", [POLLS_BUNDLE]
        if found.chat_guid:
            where, params = where + " AND c.guid = ?", [*params, found.chat_guid]
        rows = [m for m in self._messages(f"{where} ORDER BY m.date, m.ROWID", tuple(params)) if m.poll or m.vote]
        polls = [m for m in rows if m.poll and m.poll.session == item.session]
        if not polls:
            return None
        options: dict[str, PollOption] = {}
        for message in polls:  # each update lists every option; keep them all, in the newest order
            newest = {option.id: option for option in message.poll.options}
            options = newest | {key: option for key, option in options.items() if key not in newest}
        choices: dict[str | None, tuple[str, ...]] = {}
        for message in rows:
            if message.vote and message.vote.session == item.session:
                voter = None if message.is_from_me or address_key(message.sender) in mine else message.sender
                if message.vote.options:
                    choices[voter] = message.vote.options
                else:
                    choices.pop(voter, None)  # they took their vote back
        first = polls[0]
        return PollResults(
            guid=first.guid,
            chat_guid=first.chat_guid,
            session=item.session,
            creator=first.sender,
            question=self._poll_question(first),
            options=tuple(options.values()),
            choices=choices,
            latest=polls[-1].guid,
        )

    def _poll_question(self, poll: Message) -> str | None:
        """What the poll's sender typed with it: Messages sends it as its own message, right after the poll."""
        if poll.date is None or not poll.chat_guid:
            return None
        at = int((poll.date.timestamp() - APPLE_EPOCH) * 1e9)
        after = self._messages(
            "c.guid = ? AND m.item_type = 0 AND m.balloon_bundle_id IS NULL AND m.associated_message_type = 0"
            " AND m.date BETWEEN ? AND ? ORDER BY m.date LIMIT 5",
            (poll.chat_guid, at - 1_000_000, at + 5_000_000_000),
        )
        sent = [m for m in after if m.is_from_me == poll.is_from_me and m.sender == poll.sender and m.text]
        return sent[0].text if sent else None

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

    def texting_address(self) -> str | None:
        """The number the last SMS (or RCS) sent from this Mac went out through: the iPhone forwarding texts here."""
        self._connect()
        rows = self._query(
            f"SELECT {self._message_address} FROM message WHERE is_from_me = 1 AND service IN ('SMS', 'RCS')"
            f" AND {self._message_address} IS NOT NULL ORDER BY date DESC LIMIT 1"
        )
        return display_address(rows[0][0]) if rows else None

    def broken_chats(self) -> list[str]:
        """One-to-one conversations addressed to an internal form of an address ("e:you@example.com"). IMCore can
        create one when it misfiles a message, then treat it as your note-to-self chat; texts sent into it fail."""
        rows = self._query(
            f"SELECT chat_identifier FROM chat WHERE style != {GROUP_STYLE}"
            " AND (lower(chat_identifier) LIKE 'e:%' OR lower(chat_identifier) LIKE 'p:%')"
        )
        return [row[0] for row in rows]

    def chat_for_handle(self, handle: str) -> str | None:
        """The most recently active one-to-one chat with a phone number or email, if there is one."""
        handle = handle.strip()
        self._connect()
        rows = self._query(
            f"SELECT c.guid, c.chat_identifier, {self._last_activity} AS last_date FROM chat c"
            f" WHERE c.style != {GROUP_STYLE} ORDER BY last_date DESC"
        )
        # Only a chat with that very address: a phone number never matches an email or other identifier that
        # happens to contain its digits (an MMS gateway or bounce address like "...-4805550100=mms.att.net@...").
        matches = [row for row in rows if same_address(row["chat_identifier"], handle)]
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
            self._select = _message_select(message_columns, chat_columns, join_columns)
            self._has_polls = {"balloon_bundle_id", "payload_data"} <= message_columns
            self._has_schedules = {"schedule_type", "schedule_state", "is_delivered"} <= message_columns
            if "last_addressed_handle" in chat_columns:
                self._chat_address = "c.last_addressed_handle"
            if "destination_caller_id" in message_columns:
                self._message_address = "destination_caller_id"
            if "date_edited" in message_columns:
                self._edited = "date_edited"
            if "date_retracted" in message_columns:
                self._retracted = "date_retracted"
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

    def _messages(self, where: str, params: tuple, *, fallbacks: bool = False) -> list[Message]:
        self._connect()
        rows = self._query(f"{self._select} WHERE {where}", params)
        attachments = self._attachments([row["rowid"] for row in rows if row["cache_has_attachments"]])
        messages = []
        for row in rows:
            text = row["text"] or attributed_body_text(row["attributedBody"])
            text = (text or "").replace("￼", "").strip() or None  # U+FFFC stands in for attachments
            event = _event(row)
            address = row["destination_caller_id"] or (row["chat_address"] if event else None)  # some events lack it
            poll = parse_poll(row["associated_message_type"], row["associated_message_guid"], row["poll_payload"])
            if poll is not None:
                text = None  # a placeholder, or the fallback text
            elif row["poll_before"] and text and text == fallback_text(row["poll_before"]) and not fallbacks:
                continue  # the "Sent a poll" Messages sends along with a poll, and doesn't show
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
                    address=display_address(address),
                    **_changes(row),
                    attachments=attachments.get(row["rowid"], ()),
                    mentions=tuple(map(display_address, attributed_body_mentions(row["attributedBody"]))),
                    event=event,
                    poll=poll if isinstance(poll, Poll) else None,
                    vote=poll if isinstance(poll, PollVote) else None,
                    scheduled_for=apple_time(row["date"]) if _waiting(row) else None,
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


def _kinds(events: bool) -> str:
    return f"m.item_type IN (0, {', '.join(map(str, EVENT_TYPES))})" if events else "m.item_type = 0"


def _message_select(columns: set[str], chat_columns: set[str], join_columns: set[str]) -> str:
    # Older macOS versions lack some of these columns; select NULL in their place.
    def column(name: str) -> str:
        return f"m.{name}" if name in columns else f"NULL AS {name}"

    others = "other_handle" in columns
    if {"balloon_bundle_id", "payload_data"} <= columns:
        # A poll's payload, and for a plain message, the payload of a poll its sender sent just before it: Messages
        # follows each poll with a plain "Sent a poll" that it doesn't show (see polls.py).
        when = "pj.message_date" if "message_date" in join_columns else "p.date"
        polls = (
            f"CASE WHEN m.balloon_bundle_id = '{POLLS_BUNDLE}' THEN m.payload_data END AS poll_payload,"
            " CASE WHEN m.balloon_bundle_id IS NULL AND m.item_type = 0 AND m.associated_message_type = 0 THEN"
            " (SELECT p.payload_data FROM chat_message_join pj JOIN message p ON p.ROWID = pj.message_id"
            f" WHERE pj.chat_id = cmj.chat_id AND p.balloon_bundle_id = '{POLLS_BUNDLE}'"
            f" AND p.associated_message_type IN ({', '.join(map(str, POLL_TYPES))})"
            f" AND p.is_from_me = m.is_from_me AND p.handle_id = m.handle_id"
            f" AND {when} BETWEEN m.date - 3000000000 AND m.date ORDER BY {when} DESC LIMIT 1) END AS poll_before,"
        )
    else:
        polls = "NULL AS poll_payload, NULL AS poll_before,"
    return (
        "SELECT m.ROWID AS rowid, m.guid, m.text, m.attributedBody, m.is_from_me, m.date, m.service,"
        f" m.cache_has_attachments, {column('associated_message_guid')}, {column('associated_message_type')},"
        f" {column('associated_message_emoji')}, {column('thread_originator_guid')}, {column('destination_caller_id')},"
        f" {column('date_edited')}, {column('date_retracted')}, {column('message_summary_info')},"
        f" {column('item_type')}, {column('group_action_type')}, {column('group_title')}, {polls}"
        f" {column('schedule_type')}, {column('is_delivered')},"
        f" {'oh.id' if others else 'NULL'} AS other_handle_id,"
        f" {'c.last_addressed_handle' if 'last_addressed_handle' in chat_columns else 'NULL'} AS chat_address,"
        " h.id AS sender, c.guid AS chat_guid, c.display_name AS chat_name, c.style AS chat_style"
        " FROM message m"
        " LEFT JOIN handle h ON h.ROWID = m.handle_id"
        f"{' LEFT JOIN handle oh ON oh.ROWID = m.other_handle' if others else ''}"
        " LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID"
        " LEFT JOIN chat c ON c.ROWID = cmj.chat_id"
    )


def _waiting(row: sqlite3.Row) -> bool:
    """Your own message that Send Later is still holding (schedule_type 2 until it goes out)."""
    return bool(row["is_from_me"]) and row["schedule_type"] == 2 and not row["is_delivered"]


def _event(row: sqlite3.Row) -> GroupEvent | None:
    item_type, action = row["item_type"] or 0, row["group_action_type"] or 0
    if item_type not in EVENT_TYPES:
        return None
    if item_type == 2:
        return GroupEvent("renamed", name=row["group_title"] or None, code=(2, action))
    kind = _EVENT_KINDS.get((item_type, action), "other")
    if kind in ("added", "removed"):
        person = row["other_handle_id"]
    elif kind == "left":
        person = None if row["is_from_me"] else row["sender"]
    else:
        person = None
    return GroupEvent(kind, person=person, code=(item_type, action))


def _changes(row: sqlite3.Row) -> dict[str, Any]:
    """edited_at, edit_count and unsent_at for a message row.

    On macOS 26+ an unsend leaves date_retracted at 0: it clears the text, sets date_edited, and lists the retracted
    parts under "rp" in message_summary_info (a binary plist). Edits list their parts under "ep", with each part's
    history under "ec" (the original text first, then one entry per edit).
    """
    edited, retracted = row["date_edited"] or 0, row["date_retracted"] or 0
    if not (edited or retracted):
        return {}
    try:
        info = plistlib.loads(row["message_summary_info"]) if row["message_summary_info"] else {}
    except Exception:
        info = {}
    if retracted or info.get("rp"):
        return {"unsent_at": apple_time(retracted or edited)}
    history = info.get("ec") or {}
    count = max((len(versions) - 1 for versions in history.values() if isinstance(versions, list)), default=0)
    return {"edited_at": apple_time(edited), "edit_count": max(count, 1)}
