"""What imbridge asks the helper inside Messages to do, checked against a stand-in that records every request."""

import asyncio
import re
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
    Span,
    WrongAddress,
    WrongChat,
    config,
)
from imbridge.client import helper_build


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


def cards(folder, *names):
    """A contact card file for each name, as a group's members' cards would be."""
    made = []
    for name in names:
        card = folder / f"{name}.vcf"
        card.write_text(f"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:{name}\r\nEND:VCARD\r\n")
        made.append(card)
    return made


def test_several_files_go_as_one_message_with_the_text_after_them(chat_db, tmp_path):
    sam, alex = cards(tmp_path, "Sam Rivera", "Alex Kim")
    guid = "5A8F1F0E-4B7E-4C55-9A52-8A1F3B2C6D7E"
    sent, (request,) = run(
        chat_db, lambda im: im.send_files(ALEX, [sam, alex], text="everyone's numbers", guid=guid), allow=[ALEX]
    )

    assert (request["action"], sent) == ("send-multipart", "SENT-1")
    data = request["data"]
    assert (data["chatGuid"], data["guid"]) == (ALEX, guid)
    first, second, text = data["parts"]
    assert (first["partIndex"], second["partIndex"], text) == (0, 1, {"partIndex": 2, "text": "everyone's numbers"})
    for part, name in ((first, "Sam Rivera.vcf"), (second, "Alex Kim.vcf")):
        staged = Path(part["filePath"])
        assert staged.name == name and staged.parent.parent == config.OUTGOING  # each copied, in a folder of its own
        assert staged.read_text().startswith("BEGIN:VCARD")
    assert Path(first["filePath"]).parent != Path(second["filePath"]).parent


def test_files_without_text_or_with_formatting(chat_db, tmp_path):
    sam, alex = cards(tmp_path, "Sam Rivera", "Alex Kim")

    async def scenario(im):
        await im.send_files(ALEX, [sam])
        await im.chat(ALEX).send_files([sam, alex], text=[Span("all", bold=True), " of us"], reply_to="M1")

    _, (plain, formatted) = run(chat_db, scenario, allow=[ALEX])

    assert [part["partIndex"] for part in plain["data"]["parts"]] == [0]
    pieces = formatted["data"]["parts"]
    assert [(part["partIndex"], part.get("text"), part.get("styles")) for part in pieces] == [
        (0, None, None),
        (1, None, None),
        (2, "all", ["bold"]),
        (2, " of us", []),
    ]
    assert formatted["data"]["selectedMessageGuid"] == "M1"


def test_files_are_checked_before_anything_is_copied_or_sent(chat_db, tmp_path):
    (sam,) = cards(tmp_path, "Sam Rivera")
    log = []
    for bad, error in (
        (lambda im: im.send_files(ALEX, []), ValueError),
        (lambda im: im.send_files(ALEX, [sam] * 21), ValueError),
        (lambda im: im.send_files(ALEX, [sam, tmp_path / "gone.vcf"]), FileNotFoundError),
        (lambda im: im.send_files(CREW, [sam]), SendNotAllowed),
    ):
        with pytest.raises(error):
            run(chat_db, bad, None, log, allow=[ALEX])
    assert log == []
    assert not config.OUTGOING.exists() or not any(config.OUTGOING.iterdir())


def test_files_messages_refuses_leave_no_copies_behind(chat_db, tmp_path):
    sam, alex = cards(tmp_path, "Sam Rivera", "Alex Kim")
    with pytest.raises(HelperError):
        refused = {"send-multipart": {"error": "chat not found"}}
        run(chat_db, lambda im: im.send_files(ALEX, [sam, alex], text="hi"), refused, allow=[ALEX])
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


def test_an_older_helper_is_reloaded(chat_db, tmp_path, monkeypatch, caplog):
    dylib = tmp_path / "imbridge-helper.dylib"
    dylib.write_bytes(b"\xcf\xfa\xed\xfe...IMBRIDGE_BUILD=0123456789abcdef\x00...")
    assert helper_build(dylib) == "0123456789abcdef" and helper_build(tmp_path / "missing.dylib") is None
    launched = []
    monkeypatch.setattr("imbridge.messages_app.launch_with_helper", lambda path, env: launched.append(path))

    def connect(loaded, inject):
        async def scenario():
            port = free_port()
            task = asyncio.create_task(FakeMessages(port, build=loaded).run())
            im = IMBridge(chat_db=chat_db, token="t", port=port, dylib=dylib, inject=inject, poll_interval=0.01)
            try:
                await im.start()
                await im.start()  # checked once, not on every request
            finally:
                await im.close()
                task.cancel()

        asyncio.run(scenario())

    connect("0123456789abcdef", inject=True)  # this build: left alone
    assert launched == []
    connect("fedcba9876543210", inject=True)  # another build (or, with None, one from before builds were named)
    connect(None, inject=True)
    assert launched == [dylib, dylib]
    connect(None, inject=False)  # not ours to restart, so say so
    assert launched == [dylib, dylib] and "run `imbridge start`" in caplog.text


def test_sending_a_sticker(chat_db, tmp_path):
    image = tmp_path / "dot.png"
    image.write_bytes(b"\x89PNG not really")
    guid, (request,) = run(chat_db, lambda im: im.chat(ALEX).send_sticker(image, label="an orange dot"), allow=[ALEX])
    data = request["data"]
    assert request["action"] == "send-sticker" and guid == "SENT-1"
    assert (data["chatGuid"], data["label"], data["selectedMessageGuid"]) == (ALEX, "an orange dot", None)
    assert request["file_existed"] and data["filePath"].startswith(str(config.OUTGOING))  # where Messages can read it
    assert re.fullmatch(r"[0-9A-F-]{36}", data["stickerId"]) and re.fullmatch(r"[0-9a-f]{16}", data["stickerHash"])
    _, (request,) = run(chat_db, lambda im: im.chat(ALEX).send_sticker(image, on="M1"), allow=[ALEX])
    assert (request["data"]["selectedMessageGuid"], request["data"]["partIndex"]) == ("M1", 0)  # stuck onto M1


def test_stickers_are_checked_before_anything_is_sent(chat_db, tmp_path):
    image, text = tmp_path / "dot.png", tmp_path / "notes.txt"
    image.write_bytes(b"\x89PNG")
    text.write_text("not an image")
    log = []
    with pytest.raises(WrongChat):  # M1 is Alex's message, not the Crew's
        run(chat_db, lambda im: im.chat(CREW).send_sticker(image, on="M1"), None, log, allow=[CREW])
    with pytest.raises(ValueError, match="a sticker is an image"):
        run(chat_db, lambda im: im.send_sticker(ALEX, text), None, log, allow=[ALEX])
    with pytest.raises(SendNotAllowed):
        run(chat_db, lambda im: im.send_sticker(ALEX, image), None, log)
    assert log == []


def test_tapbacking_with_a_sticker(chat_db, tmp_path):
    image = tmp_path / "dot.png"
    image.write_bytes(b"\x89PNG")
    guid, (request,) = run(chat_db, lambda im: im.chat(ALEX).react_with_sticker("M1", image), allow=[ALEX])
    data = request["data"]
    assert request["action"] == "send-sticker-tapback" and request["file_existed"]
    assert (data["chatGuid"], data["selectedMessageGuid"], data["partIndex"]) == (ALEX, "M1", 0)
    assert re.fullmatch(r"[0-9a-f]{16}", data["stickerHash"])
    log = []
    with pytest.raises(WrongChat):  # a chat only tapbacks its own messages
        run(chat_db, lambda im: im.chat(CREW).react_with_sticker("M1", image), None, log, allow=[CREW])
    with pytest.raises(SendNotAllowed):
        run(chat_db, lambda im: im.react_with_sticker("M1", image), None, log)
    assert log == []


def test_choosing_the_guid(chat_db, tmp_path):
    chosen = "df2325dc-cd0c-4618-b31d-43a4376e3624"
    _, requests = run(chat_db, lambda im: im.chat(ALEX).send("hi", guid=chosen), allow=[ALEX])
    assert requests[0]["data"]["guid"] == chosen.upper()  # as Messages writes GUIDs
    _, requests = run(chat_db, lambda im: im.chat(ALEX).reply("M1", "threaded", guid=chosen), allow=[ALEX])
    assert requests[0]["data"]["guid"] == chosen.upper()
    photo = tmp_path / "cat.gif"
    photo.write_bytes(b"GIF89a")
    _, requests = run(chat_db, lambda im: im.chat(ALEX).send_file(photo, guid=chosen), allow=[ALEX])
    assert requests[0]["data"]["guid"] == chosen.upper()
    _, requests = run(chat_db, lambda im: im.send("+15550009999", "hello", guid=chosen), AVAILABLE, allow=[NEW])
    assert requests[-1]["action"] == "create-chat" and requests[-1]["data"]["guid"] == chosen.upper()
    log = []
    with pytest.raises(ValueError, match="isn't a UUID"):
        run(chat_db, lambda im: im.send(ALEX, "hi", guid="my-message-1"), None, log, allow=[ALEX])
    assert log == []
