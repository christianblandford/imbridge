"""Searching messages, and paging through a chat's history."""

import sqlite3

import pytest
from helpers import ALEX, CREW, at

from imbridge import IMBridge, MessageNotFound
from imbridge.chatdb import ChatDB


def add(path, guid, text, t, chat=1):
    db = sqlite3.connect(path)
    rowid = db.execute(
        "INSERT INTO message (guid, text, handle_id, is_from_me, date, service) VALUES (?, ?, 1, 0, ?, 'iMessage')",
        (guid, text, at(t)),
    ).lastrowid
    db.execute("INSERT INTO chat_message_join VALUES (?, ?, ?)", (chat, rowid, at(t)))
    db.commit()
    db.close()


def guids(messages):
    return [message.guid for message in messages]


def test_searching(chat_db):
    add(chat_db, "CAFE", "Café au lait?", 10)
    add(chat_db, "SMILE", "see you 🙂", 11, chat=2)
    add(chat_db, "PERCENT", "100% sure", 12)
    db = ChatDB(chat_db)

    def search(query, **options):
        return guids(db.search(query, **options))

    assert search("HEY") == ["M1"]  # not the tapback quoting it (M4)
    assert search("a body") == ["M2"]  # text Messages kept only in attributedBody
    assert search("hey", chat_guid=CREW) == []
    assert search("CAFÉ") == search("café") == ["CAFE"]  # case, beyond ASCII too
    assert search("🙂") == ["SMILE"]
    assert search("%") == ["PERCENT"] and search("_") == []  # LIKE's wildcards are only text here
    assert search("e") == ["PERCENT", "SMILE", "M3", "M1"]  # newest first; "é" is another letter
    assert search("e", before="SMILE") == ["M3", "M1"]
    with pytest.raises(ValueError):
        search("  ")
    with pytest.raises(MessageNotFound):
        search("e", before="NOPE")


def test_paging_through_history(chat_db):
    db = ChatDB(chat_db)
    assert guids(db.history(ALEX, 2)) == ["M3", "M4"]  # the latest, oldest first
    assert guids(db.history(ALEX, 2, before="M3")) == ["M1", "M2"]
    assert guids(db.history(ALEX, 2, before="M1")) == []  # the start of the chat
    assert guids(db.history(ALEX, 2, after="M1")) == ["M2", "M3"]  # reading on from a message
    assert guids(db.history(ALEX, 5, after="M2", before="M4")) == ["M3"]


def test_searching_through_the_api(chat_db):
    im = IMBridge(chat_db=chat_db, token="t", inject=False)
    assert guids(im.search("hey")) == ["M1"] and guids(im.chat(ALEX).search("threaded")) == ["M3"]
    newest, older = im.search("e", limit=1), None
    older = im.search("e", limit=1, before=newest[0])
    assert guids(newest) == ["M3"] and guids(older) == ["M1"]
    assert guids(im.chat(ALEX).history(1, before="M2")) == ["M1"]
