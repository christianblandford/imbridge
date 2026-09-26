"""Which of your addresses messages and chats are on, and the rules that keep a program to one of them."""

import asyncio
import sqlite3

import pytest

from imbridge import ANY_ADDRESS, AddressNotChosen, IMBridge, WrongAddress
from imbridge.addresses import address_key, display_address, is_phone
from imbridge.chatdb import APPLE_EPOCH

PERSONAL, BOT = "+15550001111", "+15550002222"  # two iPhones on one Apple ID
ALEX, JO = "any;-;+15551234567", "any;-;+15557778888"


@pytest.mark.parametrize(
    ("given", "shown", "key"),
    [
        ("+14805550100", "+14805550100", "4805550100"),
        ("14805550100", "14805550100", "4805550100"),
        ("+1 (480) 555-0100", "+1 (480) 555-0100", "4805550100"),
        ("mailto:You@Example.com", "you@example.com", "you@example.com"),
        ("e:you@example.com", "you@example.com", "you@example.com"),
        ("", None, None),
        (None, None, None),
    ],
)
def test_normalizing(given, shown, key):
    assert display_address(given) == shown
    assert address_key(given) == key


def test_is_phone():
    assert is_phone("14805550100")
    assert not is_phone("mailto:you@example.com")
    assert not is_phone(None)


def now_ns(offset: int = 0) -> int:
    import time

    return int((time.time() - APPLE_EPOCH + offset) * 1e9)


def make_db(path, *, second_number: bool) -> None:
    """Alex texts your personal number; with second_number, Jo texts the bot's number."""
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT, text TEXT, attributedBody BLOB,
            handle_id INTEGER, is_from_me INTEGER, date INTEGER, service TEXT, item_type INTEGER DEFAULT 0,
            cache_has_attachments INTEGER DEFAULT 0, associated_message_guid TEXT,
            associated_message_type INTEGER DEFAULT 0, associated_message_emoji TEXT, thread_originator_guid TEXT,
            destination_caller_id TEXT);
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT);
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT, chat_identifier TEXT,
            display_name TEXT, service_name TEXT, style INTEGER, last_addressed_handle TEXT);
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER, message_date INTEGER);
        CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
        CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, guid TEXT, filename TEXT, mime_type TEXT,
            transfer_name TEXT);
        CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
        """
    )
    db.executemany("INSERT INTO handle (id) VALUES (?)", [("+15551234567",), ("+15557778888",)])
    db.execute("INSERT INTO chat VALUES (1, ?, '+15551234567', '', 'iMessage', 45, '15550001111')", (ALEX,))
    db.execute("INSERT INTO chat VALUES (2, ?, '+15557778888', '', 'iMessage', 45, ?)", (JO, BOT))
    add_message(db, "A1", 1, "hey it's alex", "15550001111", chat=1)
    if second_number:
        add_message(db, "J1", 2, "hi bot", BOT, chat=2)
    db.commit()
    db.close()


def add_message(db, guid, handle, text, address, chat):
    rowid = db.execute(
        "INSERT INTO message (guid, text, handle_id, is_from_me, date, service, destination_caller_id)"
        " VALUES (?, ?, ?, 0, ?, 'iMessage', ?)",
        (guid, text, handle, now_ns(), address),
    ).lastrowid
    db.execute("INSERT INTO chat_message_join VALUES (?, ?, ?)", (chat, rowid, now_ns()))


def bridge(path, **kwargs) -> IMBridge:
    return IMBridge(chat_db=path, token="t", inject=False, poll_interval=0.01, **kwargs)


async def take(count, stream):
    return [await anext(stream) for _ in range(count)]


@pytest.fixture
def two_numbers(tmp_path):
    path = tmp_path / "chat.db"
    make_db(path, second_number=True)
    return path


@pytest.fixture
def one_number(tmp_path):
    path = tmp_path / "chat.db"
    make_db(path, second_number=False)
    return path


def test_two_numbers_and_no_address_refuses_everything(two_numbers):
    im = bridge(two_numbers, allow=[ALEX, JO])
    with pytest.raises(AddressNotChosen, match="several of your phone numbers"):
        asyncio.run(im.start())
    with pytest.raises(AddressNotChosen):
        asyncio.run(take(1, im.all_messages(since=0)))
    with pytest.raises(AddressNotChosen):
        im.history(ALEX)
    with pytest.raises(AddressNotChosen):
        asyncio.run(im.send(JO, "hi"))


def test_the_bot_sees_only_its_number(two_numbers):
    im = bridge(two_numbers, address=BOT)
    assert [m.guid for m in asyncio.run(take(1, im.all_messages(since=0)))] == ["J1"]
    assert im.history(ALEX) == []  # Alex wrote to your personal number
    assert [m.guid for m in im.history(JO)] == ["J1"]


def test_the_bot_wont_send_from_your_personal_number(two_numbers):
    im = bridge(two_numbers, address=BOT, allow=[ALEX, JO])
    with pytest.raises(WrongAddress, match="Messages would send from that"):
        asyncio.run(im.send(ALEX, "hi"))  # the chat with Alex is on your personal number
    with pytest.raises(WrongAddress):
        asyncio.run(im.react("A1", "👍"))  # a message sent to your personal number


def test_address_formats_match(two_numbers):
    im = bridge(two_numbers, address="+1 555 000 1111")  # chat.db stores it as "15550001111"
    assert [m.guid for m in im.history(ALEX)] == ["A1"]


def test_any_address_takes_everything(two_numbers):
    im = bridge(two_numbers, address=ANY_ADDRESS)
    assert [m.guid for m in asyncio.run(take(2, im.all_messages(since=0)))] == ["A1", "J1"]


def test_the_environment_sets_the_address(two_numbers, monkeypatch):
    monkeypatch.setenv("IMBRIDGE_ADDRESS", BOT)
    assert [m.guid for m in bridge(two_numbers).history(JO)] == ["J1"]


def test_one_number_needs_no_address(one_number):
    assert [m.guid for m in bridge(one_number).history(ALEX)] == ["A1"]


def test_a_second_number_appearing_later_stops_the_stream(one_number):
    async def scenario():
        im = bridge(one_number)
        stream = im.all_messages()
        pending = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0.1)  # the stream is running, pinned to your one number
        db = sqlite3.connect(one_number)
        add_message(db, "J1", 2, "hi bot", BOT, chat=2)  # Jo writes to the other number
        db.commit()
        db.close()
        with pytest.raises(AddressNotChosen, match="second phone number"):
            await asyncio.wait_for(pending, 5)

    asyncio.run(scenario())
