"""doctor --live: finding your note-to-self chat (and never someone else's), and the image it sends."""

import asyncio
import sqlite3
import struct
import zlib

from helpers import ALEX, FakeMessages, free_port

from imbridge import IMBridge
from imbridge.selftest import _dot, find_self_chat


def chat_addresses(path, address):
    """Say which of your addresses the fixture's chats are on (chat.last_addressed_handle)."""
    db = sqlite3.connect(path)
    db.execute("ALTER TABLE chat ADD COLUMN last_addressed_handle TEXT")
    db.execute("UPDATE chat SET last_addressed_handle = ?", (address,))
    db.commit()
    db.close()


def self_chat(path, aliases):
    async def main():
        port = free_port()
        fake = FakeMessages(port, {"get-account-info": {"aliases": [{"Alias": alias} for alias in aliases]}})
        task = asyncio.create_task(fake.run())
        im = IMBridge(chat_db=path, token="t", inject=False, port=port)
        try:
            await im.start()
            return await find_self_chat(im)
        finally:
            await im.close()
            task.cancel()

    return asyncio.run(main())


def test_the_note_to_self_chat(chat_db):
    chat_addresses(chat_db, "me@example.com")
    # ALEX's other side, +15551234567, is one of your own addresses, and the chat is on another: a chat with yourself
    assert self_chat(chat_db, ["+1 (555) 123-4567", "me@example.com"]) == ALEX
    assert self_chat(chat_db, ["me@example.com"]) is None  # someone else: never picked


def test_a_chat_between_your_address_and_someone_elses_isnt_yours(chat_db):
    # one of your numbers is the other side, but the chat is on a number that isn't yours (a bot sharing this Mac)
    chat_addresses(chat_db, "+15559990000")
    assert self_chat(chat_db, ["+15551234567", "me@example.com"]) is None


def test_the_dot_is_a_png():
    png = _dot()
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    width, height, depth, kind = struct.unpack(">IIBB", png[16:26])
    assert (width, height, depth, kind) == (96, 96, 8, 6)  # RGBA, so its corners are transparent
    length = struct.unpack(">I", png[33:37])[0]
    pixels = zlib.decompress(png[41 : 41 + length])
    assert len(pixels) == 96 * (1 + 96 * 4) and pixels[1:5] == bytes(4)  # the top-left corner: transparent
