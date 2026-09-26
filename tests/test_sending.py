"""What imbridge asks the helper inside Messages to do, checked against a stand-in that records every request."""

import asyncio
import sqlite3
from pathlib import Path

import pytest
from helpers import ALEX, CREW, FakeMessages, at, free_port

from imbridge import (
    ANY_CHAT,
    AddressNotChosen,
    ChatNotFound,
    HelperBusy,
    HelperError,
    IMBridge,
    NewContact,
    SendNotAllowed,
    WrongAddress,
    WrongChat,
    config,
)


def run(chat_db, scenario, replies=None, log=None, **kwargs):
    """Run scenario(im) against a stand-in Messages; returns its result and the requests Messages got (which also go
    into `log`, for scenarios that raise)."""
    async def main():
        port = free_port()
        fake = FakeMessages(port, replies)
        im = IMBridge(chat_db=chat_db, token="t", inject=False, port=port, poll_interval=0.01, **kwargs)
        task = asyncio.create_task(fake.run())
        try:
            await im.start()
            return await scenario(im), fake.requests
        finally:
            if log is not None:
                log.extend(fake.requests)
            await im.close()
            task.cancel()

    return asyncio.run(main())


def actions(requests):
    return [request["action"] for request in requests]


AVAILABLE = {"check-imessage-availability": {"available": True}}
NEW = NewContact("+15550009999")  # someone you haven't messaged yet


def test_starting_a_conversation(chat_db):
    replies = {**AVAILABLE, "get-account-info": {"active_alias": "+15550001111"}}
    guid, requests = run(
        chat_db, lambda im: im.send("+1 555 000 9999", "hello"), replies, allow=[NEW], address="+15550001111"
    )
    assert actions(requests) == ["check-imessage-availability", "get-account-info", "create-chat"]
    create = requests[2]["data"]
    assert (create["addresses"], create["service"], create["message"]) == (["+15550009999"], "iMessage", "hello")
    assert guid == "SENT-3"


def test_new_conversations_from_another_address_are_refused(chat_db):
    # Messages starts every new conversation from its "Start new conversations from" address, whatever ours is
    replies = {**AVAILABLE, "get-account-info": {"active_alias": "you@example.com"}}
    log = []
    with pytest.raises(WrongAddress, match="Start new conversations from"):
        run(
            chat_db, lambda im: im.send("+15550009999", "hi"), replies, log,
            allow=[NEW], address="+15550001111",
        )
    assert "create-chat" not in actions(log)


def test_new_conversations_fall_back_to_sms(chat_db):
    _, requests = run(chat_db, lambda im: im.send("+15550009999", "hi"), allow=[NEW])
    assert requests[-1]["data"]["service"] == "SMS"


def with_addresses(chat_db, *statements):
    """Give the test chat.db the column saying which of your addresses each message is on, then run statements."""
    db = sqlite3.connect(chat_db)
    db.execute("ALTER TABLE message ADD COLUMN destination_caller_id TEXT")
    for statement, *parameters in statements:
        db.execute(statement, parameters)
    db.commit()
    db.close()


def test_new_conversations_from_a_second_phone_number_are_refused(chat_db):
    # no address given, and everything here is on +15550001111: a new conversation can't start from another number
    with_addresses(chat_db, ("UPDATE message SET destination_caller_id = '+15550001111'",))
    replies = {**AVAILABLE, "get-account-info": {"active_alias": "+15550002222"}}
    log = []
    with pytest.raises(AddressNotChosen, match="second phone number"):
        run(chat_db, lambda im: im.send("+15550009999", "hi"), replies, log, allow=[NEW])
    assert "create-chat" not in actions(log)


def test_texts_start_only_from_the_number_they_go_out_through(chat_db):
    with_addresses(chat_db, (  # the last text sent from this Mac went out through the iPhone with +15550002222
        "INSERT INTO message (guid, text, is_from_me, date, service, destination_caller_id)"
        " VALUES ('S1', 'hi', 1, ?, 'SMS', '+15550002222')", at(9),
    ))
    _, requests = run(chat_db, lambda im: im.send("+15550009999", "hi"), allow=[NEW], address="+15550002222")
    assert actions(requests) == ["check-imessage-availability", "create-chat"]
    assert requests[-1]["data"]["service"] == "SMS"
    log = []
    with pytest.raises(WrongAddress, match=r"through \+15550002222"):
        run(
            chat_db, lambda im: im.send("+15550009999", "hi"), None, log,
            allow=[NEW], address="+15550001111",
        )
    assert "create-chat" not in actions(log)


def test_new_conversations_need_allowing(chat_db):
    with pytest.raises(SendNotAllowed, match="15550009999"):
        run(chat_db, lambda im: im.send("+15550009999", "hi"), AVAILABLE)


def test_local_numbers_cant_start_conversations(chat_db):
    with pytest.raises(ChatNotFound, match="country code"):
        run(chat_db, lambda im: im.send("555 000 9999", "hi"), AVAILABLE, allow=ANY_CHAT)


def test_sending_a_file(chat_db, tmp_path):
    photo = tmp_path / "cat.gif"
    photo.write_bytes(b"GIF89a")
    guid, requests = run(chat_db, lambda im: im.send_file(ALEX, photo, reply_to="M1"), allow=[ALEX])
    (request,) = requests
    assert request["action"] == "send-attachment"
    data = request["data"]
    assert data["chatGuid"] == ALEX and data["selectedMessageGuid"] == "M1"
    assert data["filePath"].startswith(str(config.OUTGOING)) and data["filePath"].endswith("/cat.gif")
    assert request["file_existed"]  # copied where sandboxed Messages can read it...
    assert Path(data["filePath"]).read_bytes() == b"GIF89a"  # ...and kept: that copy is the attachment Messages shows
    assert guid == "SENT-1"


def test_a_refused_file_leaves_no_copy_behind(chat_db, tmp_path):
    photo = tmp_path / "cat.gif"
    photo.write_bytes(b"GIF89a")
    with pytest.raises(HelperError):
        refused = {"send-attachment": {"error": "chat not found"}}
        run(chat_db, lambda im: im.send_file(ALEX, photo), refused, allow=[ALEX])
    assert not any(config.OUTGOING.iterdir())


def test_a_chat_only_attaches_replies_to_its_own_messages(chat_db, tmp_path):
    photo = tmp_path / "cat.gif"
    photo.write_bytes(b"GIF89a")
    with pytest.raises(WrongChat):
        run(chat_db, lambda im: im.chat(CREW).send_file(photo, reply_to="M1"), allow=[CREW])


def test_edit_and_unsend_requests(chat_db):
    async def scenario(im):
        await im.edit("M2", "better")
        await im.unsend("M2")

    _, requests = run(chat_db, scenario, allow=[ALEX])
    edit, unsend = requests
    assert edit["action"] == "edit-message" and edit["data"] == {
        "chatGuid": ALEX, "messageGuid": "M2", "partIndex": 0, "editedMessage": "better",
        "backwardsCompatibilityMessage": "Edited to “better”",
    }
    assert unsend["action"] == "unsend-message"
    assert unsend["data"] == {"chatGuid": ALEX, "messageGuid": "M2", "partIndex": 0}


def test_focus_status(chat_db):
    for status, expected in [(2, True), (1, False), (0, None)]:
        silenced, requests = run(chat_db, lambda im: im.focus_status("+1 555 123 4567"),
                                 {"check-focus-status": {"status": status}})
        assert silenced is expected
        assert requests[0]["data"] == {"address": "+15551234567"}  # nothing needs allowing: it's only a lookup
    _, requests = run(chat_db, lambda im: im.chat(ALEX).focus_status(), {"check-focus-status": {"status": 1}})
    assert requests[0]["data"] == {"address": "+15551234567"}
    with pytest.raises(ValueError, match="per person"):
        run(chat_db, lambda im: im.chat(CREW).focus_status())


def test_another_program_holding_the_helper_is_reported_before_anything_is_sent(chat_db):
    async def scenario():
        port = free_port()
        other = await asyncio.start_server(lambda reader, writer: None, host=["127.0.0.1", "::1"], port=port)
        try:
            im = IMBridge(chat_db=chat_db, token="t", port=port, allow=[ALEX])
            with pytest.raises(HelperBusy, match="Nothing was sent"):
                await im.send(ALEX, "hi")
            await im.close()
        finally:
            other.close()
            await other.wait_closed()

    asyncio.run(scenario())
