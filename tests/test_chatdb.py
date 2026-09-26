"""ChatDB and the Chat/IMBridge read and safety paths, against a small database shaped like Messages' chat.db."""

import asyncio
import sqlite3

import pytest
from helpers import ALEX, CREW, at

from imbridge import ChatNotFound, IMBridge, NewContact, SendNotAllowed, WrongChat
from imbridge.chatdb import ChatDB, FullDiskAccessError


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


def test_groups_by_name(chat_db):
    im = bridge(chat_db)
    assert im.chat("Crew").guid == CREW
    assert im.chat("crew").guid == CREW


def test_ambiguous_group_names_are_refused(chat_db):
    db = sqlite3.connect(chat_db)
    db.execute(
        "INSERT INTO chat (guid, chat_identifier, display_name, style) VALUES ('any;+;chat999', 'chat999', 'Crew', 43)"
    )
    db.commit()
    db.close()
    with pytest.raises(ChatNotFound, match="2 chats are named"):
        bridge(chat_db).chat("Crew")


def test_inline_allowlist(chat_db):
    im = bridge(chat_db, allow="Crew")  # a single chat can be given on its own
    assert im.chat(CREW).can_send
    assert not im.chat(ALEX).can_send


def test_allowlist_typos_fail_at_start(chat_db):
    im = bridge(chat_db, allow=["Crew", "Crue"])
    with pytest.raises(ChatNotFound, match="Crue"):
        asyncio.run(im.start())  # before it ever waits for the helper


def test_allowing_someone_you_havent_messaged_yet(chat_db):
    with pytest.raises(ChatNotFound, match="19998887777"):  # a plain entry must match a chat: this could be a typo
        asyncio.run(bridge(chat_db, allow=["+19998887777"]).start())
    im = bridge(chat_db, allow=[NewContact("+19998887777"), NewContact("new@example.com")])
    assert im._guard.resolve_allowed() == set()  # no chats yet, and no error
    assert im._guard.allows_handle("+1 (999) 888-7777")
    assert im._guard.allows_handle("NEW@example.com")
    assert im._guard.allows("any;-;+19998887777")  # the chat once it exists
    assert not im._guard.allows_handle("+15551234567")
    assert not im._guard.allows(ALEX)


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


def test_broken_conversations(chat_db):
    db = sqlite3.connect(chat_db)
    db.execute("INSERT INTO chat (guid, chat_identifier, display_name, service_name, style)"
               " VALUES ('any;-;e:me@example.com', 'e:me@example.com', '', 'iMessage', 45)")
    db.commit()
    db.close()
    assert ChatDB(chat_db).broken_chats() == ["e:me@example.com"]  # the fixture's real chats aren't listed


def test_a_phone_number_only_matches_that_phone_number(chat_db):
    # a more recently active one-to-one chat whose identifier merely contains Alex's digits (an MMS bounce address)
    db = sqlite3.connect(chat_db)
    rowid = db.execute(
        "INSERT INTO chat (guid, chat_identifier, display_name, service_name, style) VALUES (?, ?, '', 'SMS', 45)",
        ("any;-;bounces+1792755-274f-5551234567=mms.att.net@sendgrid.net",
         "bounces+1792755-274f-5551234567=mms.att.net@sendgrid.net"),
    ).lastrowid
    message = db.execute("INSERT INTO message (guid, text, handle_id, is_from_me, date, service)"
                         " VALUES ('B1', 'bounce', 0, 0, ?, 'SMS')", (at(99),)).lastrowid
    db.execute("INSERT INTO chat_message_join VALUES (?, ?, ?)", (rowid, message, at(99)))
    db.commit()
    db.close()
    chats = ChatDB(chat_db)
    for written in ["+15551234567", "(555) 123-4567", "15551234567"]:
        assert chats.chat_for_handle(written) == ALEX
    assert chats.chat_for_handle("+445551234567") is None  # same last 10 digits, another country
