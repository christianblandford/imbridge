"""Changes to group chats (people added, removed or leaving, renames), and @mentions."""

import asyncio
import sqlite3

from helpers import CREW, at
from test_typedstream import archive, mention

from imbridge import IMBridge
from imbridge.chatdb import ChatDB, GroupEvent


def with_events(path):
    """The fixture's chat.db, with the columns group changes use and one of each change in the Crew group."""
    db = sqlite3.connect(path)
    for column in ("other_handle INTEGER DEFAULT 0", "group_title TEXT", "group_action_type INTEGER DEFAULT 0"):
        db.execute(f"ALTER TABLE message ADD COLUMN {column}")
    events = [
        # guid, item_type, action, actor (handle_id; 0 is you), other_handle, title
        ("ADD", 1, 0, 2, 1, None),  # sam added +15551234567
        ("KICK", 1, 1, 0, 2, None),  # you removed sam
        ("NAME", 2, 0, 1, 0, "Crew 2.0"),
        ("BYE", 3, 0, 1, 0, None),  # +15551234567 left
        ("ODD", 3, 7, 1, 0, None),  # a group action imbridge has no name for
    ]
    for t, (guid, item_type, action, actor, other, title) in enumerate(events, 10):
        rowid = db.execute(
            "INSERT INTO message (guid, handle_id, is_from_me, date, service, item_type, group_action_type,"
            " other_handle, group_title) VALUES (?, ?, 0, ?, 'iMessage', ?, ?, ?, ?)",
            (guid, actor, at(t), item_type, action, other, title),
        ).lastrowid
        db.execute("INSERT INTO chat_message_join VALUES (2, ?, ?)", (rowid, at(t)))
    db.commit()
    db.close()
    return path


def test_reading_group_changes(chat_db):
    db = ChatDB(with_events(chat_db))
    found = {message.guid: message for message in db.history(CREW, events=True) if message.event}
    assert found["ADD"].event == GroupEvent("added", person="+15551234567", code=(1, 0))
    assert found["ADD"].sender == "sam@example.com"  # who made the change
    assert found["KICK"].event.kind == "removed" and found["KICK"].event.person == "sam@example.com"
    assert found["KICK"].sender is None  # you did
    assert found["NAME"].event == GroupEvent("renamed", name="Crew 2.0", code=(2, 0))
    assert found["BYE"].event == GroupEvent("left", person="+15551234567", code=(3, 0))
    assert found["ODD"].event == GroupEvent("other", code=(3, 7))
    assert not any(message.event for message in db.history(CREW))  # only when asked for


def test_group_changes_in_the_stream(chat_db):
    path = with_events(chat_db)

    async def scenario():
        im = IMBridge(chat_db=path, token="t", inject=False, poll_interval=0.01, allow=[CREW])
        start = ChatDB(path).max_rowid() - 5  # just before the changes
        events = im.chat(CREW).messages(since=start, include_events=True)
        first = await asyncio.wait_for(anext(events), 5)
        found, _ = await im.new_messages(since=start, include_events=True)
        nothing, _ = await im.new_messages(since=start)  # without include_events
        return first, found, nothing

    first, found, nothing = asyncio.run(scenario())
    assert first.guid == "ADD" and first.event.kind == "added"
    assert [message.guid for message in found] == ["ADD", "KICK", "NAME", "BYE", "ODD"]
    assert nothing == []


def test_mentions(chat_db):
    db = sqlite3.connect(chat_db)
    db.execute("ALTER TABLE message ADD COLUMN destination_caller_id TEXT")
    db.execute("UPDATE message SET destination_caller_id = '+15550002222'")  # everything here is on your bot's number
    body = archive("@Bot what's up") + mention("+15550002222")
    rowid = db.execute(
        "INSERT INTO message (guid, attributedBody, handle_id, date, service, destination_caller_id)"
        " VALUES ('AT', ?, 2, ?, 'iMessage', '+15550002222')", (body, at(20)),
    ).lastrowid
    db.execute("INSERT INTO chat_message_join VALUES (2, ?, ?)", (rowid, at(20)))
    db.commit()
    db.close()

    message = ChatDB(chat_db).message("AT")
    assert (message.text, message.mentions) == ("@Bot what's up", ("+15550002222",))
    plain = ChatDB(chat_db).message("M1")
    bot = IMBridge(chat_db=chat_db, token="t", inject=False, address="+1 (555) 000-2222")
    assert bot.mentions_me(message) and not bot.mentions_me(plain)
    assert IMBridge(chat_db=chat_db, token="t", inject=False).mentions_me(message)  # no address: any of yours
    other = IMBridge(chat_db=chat_db, token="t", inject=False, address="+15550001111")
    assert not other.mentions_me(message)
