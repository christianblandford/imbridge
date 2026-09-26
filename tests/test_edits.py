"""Edits and unsends: reading them (macOS 26+ marks unsends only in message_summary_info), the changes stream, and
editing or unsending your own messages within iMessage's limits. Nothing here reaches Messages."""

import asyncio
import plistlib
import sqlite3
import time

import pytest
from helpers import ALEX, CREW

from imbridge import EditLimit, IMBridge, SendNotAllowed, WrongChat
from imbridge.chatdb import APPLE_EPOCH, ChatDB


def now_ns(offset: float = 0) -> int:
    return int((time.time() - APPLE_EPOCH + offset) * 1e9)


def edited_info(edits: int) -> bytes:
    versions = [{"t": b"archived text", "d": 0.0} for _ in range(edits + 1)]  # the original, then one per edit
    return plistlib.dumps({"ep": [0], "ec": {"0": versions}, "ust": True}, fmt=plistlib.FMT_BINARY)


UNSENT_INFO = plistlib.dumps({"rp": [0], "ust": True}, fmt=plistlib.FMT_BINARY)


def add(path, guid, *, from_me=False, age=0.0, chat=1, text="hello", **columns):
    db = sqlite3.connect(path)
    fields = {"guid": guid, "text": text, "handle_id": 0 if from_me else 1, "is_from_me": int(from_me),
              "date": now_ns(-age), "service": "iMessage", **columns}
    rowid = db.execute(
        f"INSERT INTO message ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})", list(fields.values())
    ).lastrowid
    db.execute("INSERT INTO chat_message_join VALUES (?, ?, ?)", (chat, rowid, fields["date"]))
    db.commit()
    db.close()


def update(path, guid, **columns):
    db = sqlite3.connect(path)
    db.execute(f"UPDATE message SET {', '.join(f'{k} = ?' for k in columns)} WHERE guid = ?", [*columns.values(), guid])
    db.commit()
    db.close()


def bridge(path, **kwargs):
    return IMBridge(chat_db=path, token="t", inject=False, poll_interval=0.01, **kwargs)


def test_reading_edits_and_unsends(chat_db):
    add(chat_db, "E1", text="fixed typo", date_edited=now_ns(), message_summary_info=edited_info(2))
    # macOS 26+ marks an unsend only in message_summary_info; older versions set date_retracted
    add(chat_db, "U1", text=None, date_edited=now_ns(), message_summary_info=UNSENT_INFO)
    add(chat_db, "U2", text=None, date_retracted=now_ns())  # older macOS
    db = ChatDB(chat_db)
    edited, unsent, older = db.message("E1"), db.message("U1"), db.message("U2")
    assert (edited.text, edited.edit_count, edited.unsent_at) == ("fixed typo", 2, None)
    assert edited.edited_at is not None
    assert (unsent.text, unsent.edited_at) == (None, None) and unsent.unsent_at is not None
    assert older.unsent_at is not None
    assert db.message("M1").edited_at is None and db.message("M1").unsent_at is None


def test_the_changes_stream_reports_edits_and_unsends(chat_db):
    add(chat_db, "A", text="see you at 5")
    add(chat_db, "B", text="oops")

    async def scenario():
        im = bridge(chat_db)
        changes = im.chat(ALEX).changes()
        first = asyncio.ensure_future(anext(changes))
        await asyncio.sleep(0.1)  # the stream has read where things stand
        update(chat_db, "A", text="see you at 6", date_edited=now_ns(), message_summary_info=edited_info(1))
        edited = await asyncio.wait_for(first, 5)
        update(chat_db, "B", text=None, date_edited=now_ns(), message_summary_info=UNSENT_INFO)
        unsent = await asyncio.wait_for(anext(changes), 5)
        return edited, unsent

    edited, unsent = asyncio.run(scenario())
    assert (edited.guid, edited.text, edited.edit_count) == ("A", "see you at 6", 1)
    assert (unsent.guid, unsent.text) == ("B", None) and unsent.unsent_at is not None


def test_changes_made_while_the_consumer_is_busy_are_not_lost(chat_db):
    async def scenario():
        changes = bridge(chat_db).chat(ALEX).changes()
        first = asyncio.ensure_future(anext(changes))
        await asyncio.sleep(0.1)
        add(chat_db, "C", text="hi")  # arrives and is edited before the stream looks again
        update(chat_db, "C", text="hi!", date_edited=now_ns(), message_summary_info=edited_info(1))
        return await asyncio.wait_for(first, 5)

    change = asyncio.run(scenario())
    assert (change.guid, change.text) == ("C", "hi!")


def test_only_your_own_messages_can_be_changed(chat_db):
    add(chat_db, "THEIRS")
    im = bridge(chat_db, allow=[ALEX])
    with pytest.raises(ValueError, match="isn't yours"):
        asyncio.run(im.edit("THEIRS", "nope"))
    with pytest.raises(ValueError, match="isn't yours"):
        asyncio.run(im.unsend("THEIRS"))


def test_changing_messages_needs_the_chat_allowed(chat_db):
    add(chat_db, "MINE", from_me=True)
    with pytest.raises(SendNotAllowed):
        asyncio.run(bridge(chat_db).edit("MINE", "better"))


def test_imessage_limits(chat_db):
    add(chat_db, "OLD", from_me=True, age=20 * 60)
    add(chat_db, "WORN", from_me=True, date_edited=now_ns(), message_summary_info=edited_info(5))
    add(chat_db, "MINUTES", from_me=True, age=5 * 60)
    add(chat_db, "GONE", from_me=True, text=None, date_edited=now_ns(), message_summary_info=UNSENT_INFO)
    im = bridge(chat_db, allow=[ALEX])
    for call, kind in [
        (im.edit("OLD", "late"), "edit_window"),
        (im.edit("WORN", "sixth"), "edit_count"),
        (im.unsend("MINUTES"), "unsend_window"),
    ]:
        with pytest.raises(EditLimit) as refused:
            asyncio.run(call)
        assert refused.value.kind == kind
    assert asyncio.run(im.unsend("GONE")) is None  # already unsent: nothing to do, nothing sent


def test_a_chat_only_changes_its_own_messages(chat_db):
    add(chat_db, "MINE", from_me=True)
    im = bridge(chat_db, allow=[ALEX, CREW])
    with pytest.raises(WrongChat):
        asyncio.run(im.chat(CREW).edit("MINE", "wrong chat"))


def test_participants_leave_out_the_address_you_run_as(chat_db):
    crew = bridge(chat_db, address="sam@example.com", allow=[CREW]).chat(CREW)
    assert crew.participants == ("+15551234567",)
    assert crew.to_dict()["participants"] == ["+15551234567"]
    assert set(bridge(chat_db).chat(CREW).participants) == {"+15551234567", "sam@example.com"}
