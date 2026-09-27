"""Voice messages (reading transcripts, sending audio or speech) and where your messages have got to."""

import asyncio
import sqlite3
from pathlib import Path

import pytest
from helpers import ALEX, CREW, at
from test_sending import actions, run

from imbridge import IMBridge, SendNotAllowed
from imbridge import client as client_module
from imbridge.chatdb import ChatDB
from imbridge.typedstream import attributed_body_transcript

STATUS_COLUMNS = ("is_sent", "is_delivered", "error", "date_delivered", "date_read", "date_played", "is_audio_message")


def voice_body(transcript: str | None) -> bytes:
    """attributedBody for a voice message: the placeholder character, with the transcript as an attribute."""
    body = b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject\x00"
    body += b"\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+\x03\xef\xbf\xbc\x86\x84\x02iI\x01\x01\x92\x84\x84\x84"
    body += b"\x0cNSDictionary\x00\x94\x84\x01i\x02\x92\x84\x96\x96\x1d__kIMMessagePartAttributeName\x86"
    if transcript is not None:
        text = transcript.encode()
        body += b"\x92\x84\x96\x96\x14IMAudioTranscription\x86\x92\x84\x96\x96" + bytes([len(text)]) + text + b"\x86"
    return body + b"\x86"


def add(path, guid, *, chat=1, me=1, t=20, text="hi", body=None, service="iMessage", **columns):
    db = sqlite3.connect(path)
    existing = {row[1] for row in db.execute("PRAGMA table_info(message)")}
    for column in STATUS_COLUMNS:
        if column not in existing:
            db.execute(f"ALTER TABLE message ADD COLUMN {column} INTEGER DEFAULT 0")
    names = ", ".join(["guid", "text", "attributedBody", "handle_id", "is_from_me", "date", "service", *columns])
    marks = ", ".join("?" * (7 + len(columns)))
    rowid = db.execute(f"INSERT INTO message ({names}) VALUES ({marks})",
                       (guid, text, body, 0 if me else 1, me, at(t), service, *columns.values())).lastrowid
    db.execute("INSERT INTO chat_message_join VALUES (?, ?, ?)", (chat, rowid, at(t)))
    db.commit()
    db.close()


def test_reading_transcripts():
    assert attributed_body_transcript(voice_body("See you at six")) == "See you at six"
    assert attributed_body_transcript(voice_body(None)) is None  # not transcribed (yet)
    assert attributed_body_transcript(None) is None


def test_voice_messages_in_chat_db(chat_db):
    add(chat_db, "VOICE", me=0, text=None, body=voice_body("See you at six"), is_audio_message=1, date_played=at(30))
    add(chat_db, "QUIET", me=0, t=21, text=None, body=voice_body(None), is_audio_message=1)
    db = ChatDB(chat_db)
    voice, quiet = db.message("VOICE"), db.message("QUIET")
    assert (voice.is_voice, voice.transcript, voice.text) == (True, "See you at six", None)
    assert voice.played_at is not None and voice.status is None  # someone else's: no status
    assert (quiet.is_voice, quiet.transcript) == (True, None)
    assert db.message("M1").is_voice is False


def test_statuses(chat_db):
    add(chat_db, "SENDING")
    add(chat_db, "SENT", is_sent=1)
    add(chat_db, "DELIVERED", is_sent=1, is_delivered=1, date_delivered=at(25))
    add(chat_db, "READ", is_sent=1, is_delivered=1, date_delivered=at(25), date_read=at(26))
    add(chat_db, "FAILED", error=4)  # what Messages shows as Not Delivered
    db = ChatDB(chat_db)
    statuses = {guid: db.message(guid).status for guid in ("SENDING", "SENT", "DELIVERED", "READ", "FAILED")}
    assert statuses == {"SENDING": "sending", "SENT": "sent", "DELIVERED": "delivered", "READ": "read",
                        "FAILED": "failed"}
    read = db.message("READ")
    assert read.delivered_at < read.read_at and db.message("M1").status is None  # M1 isn't yours


def test_waiting_for_delivery(chat_db):
    add(chat_db, "GROUP", chat=2, is_sent=1)  # a group never learns about delivery: "sent" is final
    add(chat_db, "SMS", is_sent=1, service="SMS")
    add(chat_db, "PENDING", is_sent=1)
    add(chat_db, "FAILED", error=4)
    im = IMBridge(chat_db=chat_db, token="t", inject=False)

    async def scenario():
        started = asyncio.get_running_loop().time()
        quick = [await im.wait_for_delivery(guid, timeout=5) for guid in ("GROUP", "SMS", "FAILED")]
        elapsed = asyncio.get_running_loop().time() - started
        return quick, elapsed, await im.wait_for_delivery("PENDING", timeout=0.3), await im.wait_for_delivery("NOPE", 0)

    quick, elapsed, pending, missing = asyncio.run(scenario())
    assert quick == ["sent", "sent", "failed"] and elapsed < 2
    assert (pending, missing) == ("sent", None)


class FakeTools:
    """Stands in for say and afconvert: records each command and writes the file it would."""

    def __init__(self, voices=("Samantha  en_US    # Hello! My name is Samantha.",)):
        self.voices = "\n".join(voices)
        self.commands = []

    async def __call__(self, *command):
        self.commands.append(command)
        if command[:3] == ("say", "-v", "?"):
            return 0, self.voices
        Path(command[2] if command[0] == "say" else command[-1]).write_bytes(b"audio")
        return 0, ""


def test_sending_voice_messages(chat_db, monkeypatch, tmp_path):
    tools = FakeTools()
    monkeypatch.setattr(client_module, "_run", tools)
    clip = tmp_path / "memo.m4a"
    clip.write_bytes(b"not really audio")
    guid = "5a8f1f0e-4b7e-4c55-9a52-8a1f3b2c6d7e"
    sent, (request,) = run(chat_db, lambda im: im.chat(ALEX).send_voice(clip, guid=guid), allow=[ALEX])
    data = request["data"]
    assert request["action"] == "send-attachment" and data["isAudioMessage"] == 1 and data["guid"] == guid.upper()
    assert data["filePath"].endswith("/Audio Message.caf") and request["file_existed"]
    assert [command[0] for command in tools.commands] == ["afconvert", "afconvert"]  # to mono PCM, then to Opus
    assert "opus@24000" in tools.commands[1]

    tools.commands.clear()
    _, (request,) = run(chat_db, lambda im: im.send_voice(ALEX, text="On my way!", voice="samantha"), allow=[ALEX])
    said = next(command for command in tools.commands if command[0] == "say" and "-o" in command)
    assert said[-2:] == ("-v", "Samantha") and request["data"]["isAudioMessage"] == 1


def test_voice_messages_are_checked_before_anything_runs(chat_db, monkeypatch, tmp_path):
    tools = FakeTools()
    monkeypatch.setattr(client_module, "_run", tools)
    log = []
    with pytest.raises(SendNotAllowed):
        run(chat_db, lambda im: im.send_voice(CREW, text="hi"), None, log)
    assert tools.commands == []
    for bad in (
        lambda im: im.send_voice(ALEX),  # neither a file nor text
        lambda im: im.send_voice(ALEX, tmp_path / "memo.m4a", text="both"),
        lambda im: im.send_voice(ALEX, text="  "),
        lambda im: im.send_voice(ALEX, text="hi", voice="Nobody"),  # say would quietly use its default voice
    ):
        with pytest.raises(ValueError):
            run(chat_db, bad, None, log, allow=[ALEX])
    with pytest.raises(FileNotFoundError):
        run(chat_db, lambda im: im.send_voice(ALEX, tmp_path / "missing.m4a"), None, log, allow=[ALEX])
    assert log == []


def test_audio_that_cant_be_converted(chat_db, monkeypatch, tmp_path):
    async def refuse(*command):
        return 1, "Error: Couldn't open input file"

    monkeypatch.setattr(client_module, "_run", refuse)
    junk = tmp_path / "junk.mp3"
    junk.write_text("not audio")
    log = []
    with pytest.raises(ValueError, match="junk.mp3 isn't audio"):
        run(chat_db, lambda im: im.send_voice(ALEX, junk), None, log, allow=[ALEX])
    assert actions(log) == []


def test_status_and_voice_in_the_mcp_server(chat_db):
    pytest.importorskip("mcp")
    from test_mcp import call, server_for

    add(chat_db, "VOICE", me=0, text=None, body=voice_body("See you at six"), is_audio_message=1)
    add(chat_db, "FAILED", t=21, error=4)
    server = server_for(chat_db)
    voice, failed = call(server, "read_messages", chat=ALEX)[-2:]
    assert (voice["voice"], voice["transcript"]) == (True, "See you at six")
    assert failed["status"] == "failed"
    assert call(server, "message_status", message_guid="FAILED") == {"guid": "FAILED", "status": "failed"}
