"""A small database with the tables, columns and joins of Messages' chat.db, shared by the tests."""

import sqlite3

from imbridge.chatdb import APPLE_EPOCH

SCHEMA = """
CREATE TABLE message (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE, text TEXT, attributedBody BLOB,
    handle_id INTEGER DEFAULT 0, is_from_me INTEGER DEFAULT 0, date INTEGER, service TEXT, item_type INTEGER DEFAULT 0,
    cache_has_attachments INTEGER DEFAULT 0, associated_message_guid TEXT, associated_message_type INTEGER DEFAULT 0,
    associated_message_emoji TEXT, thread_originator_guid TEXT, date_edited INTEGER DEFAULT 0,
    date_retracted INTEGER DEFAULT 0, message_summary_info BLOB);
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT, chat_identifier TEXT, display_name TEXT,
    service_name TEXT, style INTEGER);
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER, message_date INTEGER);
CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT, filename TEXT, mime_type TEXT,
    transfer_name TEXT);
CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
"""

ALEX, CREW = "any;-;+15551234567", "any;+;chat123"
BODY = b"\x84\x84\x08NSString\x01\x94\x84\x01+\x0bfrom a body\x86"


def at(seconds: int) -> int:
    return (1_800_000_000 + seconds - APPLE_EPOCH) * 1_000_000_000


def make_chat_db(path):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.executemany("INSERT INTO handle (id) VALUES (?)", [("+15551234567",), ("sam@example.com",)])
    db.executemany(
        "INSERT INTO chat (guid, chat_identifier, display_name, service_name, style) VALUES (?, ?, ?, ?, ?)",
        [(ALEX, "+15551234567", "", "iMessage", 45), (CREW, "chat123", "Crew", "iMessage", 43)],
    )
    db.executemany("INSERT INTO chat_handle_join VALUES (?, ?)", [(1, 1), (2, 1), (2, 2)])
    rows = [
        # guid, text, body, handle, from_me, t, item_type, attachments, assoc_guid, assoc_type, emoji, thread, chat
        ("M1", "hey", None, 1, 0, 1, 0, 0, None, 0, None, None, 1),
        ("M2", None, BODY, 0, 1, 2, 0, 0, None, 0, None, None, 1),
        ("M3", "threaded", None, 1, 0, 3, 0, 0, None, 0, None, "M2", 1),
        ("M4", "Loved “hey”", None, 0, 1, 4, 0, 0, "p:0/M1", 2000, None, None, 1),
        ("M5", "Reacted 👀", None, 2, 0, 5, 0, 0, "p:0/M1", 2006, "👀", None, 2),
        ("M6", "￼", None, 2, 0, 6, 0, 1, None, 0, None, None, 2),
        ("M7", None, None, 2, 0, 7, 1, 0, None, 0, None, None, 2),  # a group event, not a message
    ]
    for rowid, (guid, text, body, handle, me, t, item, att, aguid, atype, emoji, thread, chat) in enumerate(rows, 1):
        db.execute(
            "INSERT INTO message (guid, text, attributedBody, handle_id, is_from_me, date, service, item_type,"
            " cache_has_attachments, associated_message_guid, associated_message_type, associated_message_emoji,"
            " thread_originator_guid) VALUES (?, ?, ?, ?, ?, ?, 'iMessage', ?, ?, ?, ?, ?, ?)",
            (guid, text, body, handle, me, at(t), item, att, aguid, atype, emoji, thread),
        )
        db.execute("INSERT INTO chat_message_join VALUES (?, ?, ?)", (chat, rowid, at(t)))
    db.execute(
        "INSERT INTO attachment (guid, filename, mime_type, transfer_name)"
        " VALUES ('A1', '~/Library/Messages/Attachments/x/IMG.jpg', 'image/jpeg', 'IMG.jpg')"
    )
    db.execute("INSERT INTO message_attachment_join VALUES (6, 1)")
    db.commit()
    db.close()
    return path


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeMessages:
    """Stands in for the helper inside Messages: dials imbridge like the real one, records requests, answers them."""

    def __init__(self, port: int, replies: dict | None = None) -> None:
        self.port = port
        self.replies = replies or {}
        self.requests: list[dict] = []

    async def run(self) -> None:
        import asyncio
        import json
        import os

        for _ in range(200):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
                break
            except OSError:
                await asyncio.sleep(0.02)
        writer.write(b'{"event": "ping", "process": "com.apple.MobileSMS"}\r\n')
        await writer.drain()
        while line := await reader.readline():
            request = json.loads(line)
            if "filePath" in request.get("data", {}):
                request["file_existed"] = os.path.exists(request["data"]["filePath"])  # when Messages would read it
            self.requests.append(request)
            reply = {"transactionId": request["transactionId"], "identifier": f"SENT-{len(self.requests)}"}
            answer = self.replies.get(request["action"], {})  # a reply, or a function of the request (to act on it)
            reply.update(answer(request) if callable(answer) else answer)
            writer.write(json.dumps(reply).encode() + b"\r\n")
            await writer.drain()
