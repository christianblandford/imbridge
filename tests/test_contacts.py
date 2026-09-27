"""Contact names, read from a stand-in for the Mac's Contacts databases (never the real ones)."""

import sqlite3

import pytest
from helpers import ALEX, CREW

from imbridge import IMBridge, config, contacts
from imbridge.contacts import Contacts

TABLES = """
CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, ZFIRSTNAME TEXT, ZLASTNAME TEXT, ZNICKNAME TEXT,
    ZORGANIZATION TEXT, ZDISPLAYFLAGS INTEGER DEFAULT 0);
CREATE TABLE ZABCDPHONENUMBER (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER, ZFULLNUMBER TEXT);
CREATE TABLE ZABCDEMAILADDRESS (Z_PK INTEGER PRIMARY KEY, ZOWNER INTEGER, ZADDRESS TEXT);
"""


def address_book(source="icloud", cards=()):
    """A Contacts database for one account: cards as (first, last, organization, flags, numbers, emails)."""
    path = config.ADDRESS_BOOK / "Sources" / source / "AddressBook-v22.abcddb"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(TABLES)
    for card, (first, last, organization, flags, numbers, emails) in enumerate(cards, 1):
        db.execute("INSERT INTO ZABCDRECORD (Z_PK, ZFIRSTNAME, ZLASTNAME, ZORGANIZATION, ZDISPLAYFLAGS)"
                   " VALUES (?, ?, ?, ?, ?)", (card, first, last, organization, flags))
        db.executemany("INSERT INTO ZABCDPHONENUMBER (ZOWNER, ZFULLNUMBER) VALUES (?, ?)", [(card, n) for n in numbers])
        db.executemany("INSERT INTO ZABCDEMAILADDRESS (ZOWNER, ZADDRESS) VALUES (?, ?)", [(card, e) for e in emails])
    db.commit()
    db.close()
    return path


@pytest.fixture
def people(monkeypatch):
    monkeypatch.setattr(contacts, "REFRESH", 0)  # look for changes on every lookup
    address_book(cards=[
        ("Alex", "Rivera", None, 0, ["(555) 123-4567"], []),
        ("Sam", "Lee", None, 0, ["+44 20 7946 0000"], ["Sam@Example.com"]),
        (None, None, "Acme Dental", 1, ["+1 555 010 2000"], []),
        ("Mom", None, None, 0, ["+1 555 777 8888"], []),
        ("Dad", None, None, 0, ["+1 555 777 8888"], []),  # a shared landline: two names, so neither
        ("Jo", None, None, 0, ["123-4567"], []),  # no area code: never the same as a full number
    ])
    address_book("google", cards=[("Alex", "Rivera", None, 0, ["+1 555 123 4567"], [])])  # the same card twice


def test_names(people):
    book = Contacts()
    assert book.name("+15551234567") == "Alex Rivera"  # however each side writes the number
    assert book.name("sam@example.com") == book.name("mailto:SAM@example.com") == "Sam Lee"
    assert book.name("+442079460000") == "Sam Lee"
    assert book.name("+12079460000") is None  # the same last ten digits, another country
    assert book.name("+15550102000") == "Acme Dental"  # a company card goes by its organization
    assert book.name("+15557778888") is None
    assert book.name("+15551234567") == "Alex Rivera" and book.cards == 7
    assert book.name(None) is None and book.name("chat123") is None


def test_changes_to_contacts_are_picked_up(people):
    book = Contacts()
    assert book.name("+15559990000") is None
    address_book("on-my-mac", cards=[("New", "Friend", None, 0, ["+1 555 999 0000"], [])])
    assert book.name("+15559990000") == "New Friend"


def test_no_contacts():
    assert Contacts().name("+15551234567") is None  # nothing there (or not readable): no names, no error


def test_names_in_messages_and_chats(people, chat_db):
    im = IMBridge(chat_db=chat_db, token="t", inject=False)
    first = im.message("M1")
    assert (first.sender, first.sender_name) == ("+15551234567", "Alex Rivera")
    assert im.message("M2").sender_name is None  # yours
    assert im.chat(ALEX).names == {"+15551234567": "Alex Rivera"}
    assert [chat.guid for chat in im.chats(query="rivera")] == [CREW, ALEX]  # both have Alex in them
    assert [chat.guid for chat in im.chats(query="SAM@")] == [CREW]
    assert [chat.guid for chat in im.chats(query="crew")] == [CREW]
    assert [chat.guid for chat in im.chats(query="(555) 123-45")] == [CREW, ALEX]  # a number, typed any way
    assert [chat.guid for chat in im.chats(query="rivera", offset=1)] == [ALEX]
    assert im.chats(query="nobody") == []
    assert im.contact_name("+15551234567") == "Alex Rivera"
    assert IMBridge(chat_db=chat_db, token="t", inject=False, contacts=False).message("M1").sender_name is None


def test_names_in_the_mcp_server(people, chat_db):
    pytest.importorskip("mcp")
    from test_mcp import call, server_for

    server = server_for(chat_db)
    names = {chat["chat"]: chat["names"] for chat in call(server, "list_chats", query="alex")}
    assert names == {CREW: {"+15551234567": "Alex Rivera", "sam@example.com": "Sam Lee"},
                     ALEX: {"+15551234567": "Alex Rivera"}}
    first = call(server, "read_messages", chat=ALEX)[0]
    assert (first["from"], first["from_name"]) == ("+15551234567", "Alex Rivera")
