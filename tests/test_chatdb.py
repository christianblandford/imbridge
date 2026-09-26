"""ChatDB and the Chat/IMBridge read and safety paths, against a small database shaped like Messages' chat.db."""

import asyncio
import sqlite3

import pytest

from imbridge import ChatNotFound, IMBridge, SendNotAllowed, WrongChat
from imbridge.chatdb import APPLE_EPOCH, ChatDB, FullDiskAccessError

SCHEMA = """
CREATE TABLE message (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE, text TEXT, attributedBody BLOB,
    handle_id INTEGER DEFAULT 0, is_from_me INTEGER DEFAULT 0, date INTEGER, service TEXT, item_type INTEGER DEFAULT 0,
    cache_has_attachments INTEGER DEFAULT 0, associated_message_guid TEXT, associated_message_type INTEGER DEFAULT 0,
    associated_message_emoji TEXT, thread_originator_guid TEXT);
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


@pytest.fixture
def chat_db(tmp_path):
    path = tmp_path / "chat.db"
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


def test_messages_after(chat_db):
    messages = ChatDB(chat_db).messages_after(0)
    assert [m.guid for m in messages] == ["M1", "M2", "M3", "M4", "M5", "M6"]
    first, mine, reply, love, eyes, photo = messages
    assert (first.sender, first.is_from_me, first.text, first.chat_guid) == ("+15551234567", False, "hey", ALEX)
    assert first.date.timestamp() == 1_800_000_001
    assert (mine.sender, mine.is_from_me, mine.text) == (None, True, "from a body")
    assert reply.reply_to == "M2"
    assert (love.reaction.kind, love.reaction.target_guid) == ("love", "M1")
    assert (eyes.reaction.kind, eyes.reaction.emoji, eyes.is_group, eyes.chat_name) == ("emoji", "👀", True, "Crew")
    assert photo.text is None
    assert photo.attachments[0].name == "IMG.jpg"
    assert photo.attachments[0].path.endswith("/Library/Messages/Attachments/x/IMG.jpg")
    assert not photo.attachments[0].path.startswith("~")


def test_messages_after_a_rowid(chat_db):
    assert [m.guid for m in ChatDB(chat_db).messages_after(4)] == ["M5", "M6"]
    assert ChatDB(chat_db).max_rowid() == 7


def test_history_and_lookup(chat_db):
    db = ChatDB(chat_db)
    assert [m.guid for m in db.history(ALEX, limit=2)] == ["M3", "M4"]
    assert db.message("M3").reply_to == "M2"
    assert db.message("nope") is None


def test_chats(chat_db):
    crew, alex = ChatDB(chat_db).chats()
    assert (crew.guid, crew.name, crew.is_group) == (CREW, "Crew", True)
    assert set(crew.participants) == {"+15551234567", "sam@example.com"}
    assert (alex.guid, alex.name, alex.is_group) == (ALEX, None, False)


@pytest.mark.parametrize("handle", ["+15551234567", "(555) 123-4567", "555.123.4567"])
def test_chat_for_handle(chat_db, handle):
    assert ChatDB(chat_db).chat_for_handle(handle) == ALEX


def test_chat_for_unknown_handles(chat_db):
    db = ChatDB(chat_db)
    assert db.chat_for_handle("sam@example.com") is None  # only in a group, no one-to-one chat
    assert db.chat_for_handle("12") is None


def test_resolve_chat(chat_db):
    im = IMBridge(chat_db=chat_db, token="t", inject=False)
    assert im.resolve_chat(CREW) == CREW
    assert im.resolve_chat("555-123-4567") == ALEX
    with pytest.raises(ChatNotFound):
        im.resolve_chat("+19998887777")


def test_unreadable_database(tmp_path):
    with pytest.raises(FullDiskAccessError):
        ChatDB(tmp_path / "missing.db").max_rowid()


def bridge(chat_db, **kwargs) -> IMBridge:
    return IMBridge(chat_db=chat_db, token="t", inject=False, poll_interval=0.01, **kwargs)


def test_chat_handles(chat_db):
    im = bridge(chat_db)
    alex = im.chat("555-123-4567")
    assert (alex.guid, alex.is_group, alex.can_send) == (ALEX, False, False)
    assert [m.guid for m in alex.history()] == ["M1", "M2", "M3", "M4"]
    crew = im.chat(CREW)
    assert (crew.name, crew.is_group) == ("Crew", True)
    assert [chat.guid for chat in im.chats()] == [CREW, ALEX]


def test_chat_messages_stay_in_their_chat(chat_db):
    async def first(count, stream):
        return [await anext(stream) for _ in range(count)]

    crew = bridge(chat_db).chat(CREW)
    messages = asyncio.run(asyncio.wait_for(first(2, crew.messages(since=0)), 5))
    assert [m.guid for m in messages] == ["M5", "M6"]


def test_a_chat_refuses_messages_from_other_chats(chat_db):
    im = bridge(chat_db, allow=[CREW])
    crew = im.chat(CREW)
    with pytest.raises(WrongChat):
        asyncio.run(crew.react(im.message("M1"), "👍"))
    with pytest.raises(WrongChat):
        asyncio.run(crew.reply("M1", "hi"))


def test_sending_needs_an_allowed_chat(chat_db):
    im = bridge(chat_db, allow=[CREW])
    with pytest.raises(SendNotAllowed):
        asyncio.run(im.send("555-123-4567", "hi"))
    with pytest.raises(SendNotAllowed):
        asyncio.run(im.react("M1", "👍"))
    with pytest.raises(SendNotAllowed):
        asyncio.run(im.chat(ALEX).send("hi"))
    assert not im.chat(ALEX).can_send
    assert im.chat(CREW).can_send
