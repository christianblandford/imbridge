"""Send Later: scheduling, the checks around it, and cancelling, with rows written the way chat.db records them."""

import sqlite3
from datetime import datetime, timedelta

import pytest
from helpers import ALEX
from test_sending import run

from imbridge import IMBridge, SendLaterFailed, SendNotAllowed, WrongChat, client
from imbridge.chatdb import APPLE_EPOCH

TOMORROW = datetime.now() + timedelta(days=1)


def schedulable(path):
    db = sqlite3.connect(path)
    for column in ("schedule_type INTEGER DEFAULT 0", "is_delivered INTEGER DEFAULT 0"):
        db.execute(f"ALTER TABLE message ADD COLUMN {column}")
    db.commit()
    db.close()
    return path


def store(path, guid, chat, when, *, schedule_type=2, delivered=0):
    """One of your messages as chat.db has it: held for later (schedule_type 2, undelivered) unless told otherwise."""
    db = sqlite3.connect(path)
    date = int((when - APPLE_EPOCH) * 1e9)
    rowid = db.execute(
        "INSERT INTO message (guid, text, handle_id, is_from_me, date, service, schedule_type, is_delivered)"
        " VALUES (?, 'later', 0, 1, ?, 'iMessage', ?, ?)", (guid, date, schedule_type, delivered),
    ).lastrowid
    db.execute("INSERT INTO chat_message_join VALUES (?, ?, ?)", (chat, rowid, date))
    db.commit()
    db.close()


def messages_does(path, chat=1, **kwargs):
    """A stand-in for Messages' send-later that records what it did in chat.db: held it in `chat` (rowid) by default."""
    def answer(request):
        store(path, "LATER", chat, request["data"]["deliverAt"], **kwargs)
        return {"identifier": "LATER"}

    return {"send-later": answer}


def test_scheduling(chat_db):
    path = schedulable(chat_db)
    store(path, "EARLIER", 1, TOMORROW.timestamp() - 3600)  # another already waiting in the chat is fine
    guid, requests = run(path, lambda im: im.chat(ALEX).send_later("see you tomorrow", TOMORROW), messages_does(path),
                         allow=[ALEX])
    assert guid == "LATER"
    (data,) = [request["data"] for request in requests if request["action"] == "send-later"]
    assert (data["chatGuid"], data["message"]) == (ALEX, "see you tomorrow")
    assert data["deliverAt"] % 60 == 0  # to the minute, as Messages schedules
    waiting = IMBridge(chat_db=path, token="t", inject=False).scheduled(ALEX)
    assert [message.guid for message in waiting] == ["EARLIER", "LATER"]  # soonest first
    assert waiting[1].scheduled_for.timestamp() == data["deliverAt"]


@pytest.mark.parametrize(("did", "problem"), [
    ({"chat": 2}, "filed the message in any;\\+;chat123 instead of"),  # IMCore's misfiling
    ({"schedule_type": 0, "delivered": 1}, "right away"),
])
def test_what_messages_did_is_checked(chat_db, did, problem):
    path = schedulable(chat_db)
    with pytest.raises(SendLaterFailed, match=problem):
        run(path, lambda im: im.send_later(ALEX, "hi", TOMORROW), messages_does(path, **did), allow=[ALEX])


def test_refused_before_anything_is_sent(chat_db):
    path = schedulable(chat_db)
    now = datetime.now()
    for at, problem in [(now + timedelta(seconds=30), "a minute to 14 days"), (now + timedelta(days=15), "14 days")]:
        log = []
        with pytest.raises(ValueError, match=problem):
            run(path, lambda im, at=at: im.send_later(ALEX, "hi", at), None, log, allow=[ALEX])
        assert log == []
    with pytest.raises(SendNotAllowed):
        run(path, lambda im: im.send_later(ALEX, "hi", TOMORROW))


def test_not_to_yourself(chat_db):
    path = schedulable(chat_db)
    account = {"get-account-info": {"aliases": [{"Alias": "+15551234567", "Status": 3}]}}  # Alex's number is yours
    log = []
    with pytest.raises(ValueError, match="to yourself"):
        run(path, lambda im: im.send_later(ALEX, "hi", TOMORROW), account, log, allow=[ALEX])
    assert "send-later" not in [request["action"] for request in log]


def test_cancelling(chat_db, monkeypatch):
    path = schedulable(chat_db)
    store(path, "WAITING", 1, TOMORROW.timestamp())

    def forget(request):  # Messages' cancel removes the message
        db = sqlite3.connect(path)
        db.execute("DELETE FROM message WHERE guid = ?", (request["data"]["messageGuid"],))
        db.commit()
        db.close()
        return {}

    _, requests = run(path, lambda im: im.chat(ALEX).cancel_scheduled("WAITING"), {"cancel-scheduled": forget},
                      allow=[ALEX])
    assert requests[0]["data"] == {"chatGuid": ALEX, "messageGuid": "WAITING"}
    with pytest.raises(ValueError, match="isn't a message of yours waiting"):
        run(path, lambda im: im.cancel_scheduled("M1"), None, allow=[ALEX])
    store(path, "STUCK", 1, TOMORROW.timestamp())
    monkeypatch.setattr(client, "CANCEL_CHECK", 0.3)
    with pytest.raises(SendLaterFailed, match="still has STUCK scheduled"):
        run(path, lambda im: im.cancel_scheduled("STUCK"), None, allow=[ALEX])
    with pytest.raises(WrongChat):
        run(path, lambda im: im.chat("any;+;chat123").cancel_scheduled("STUCK"), None, allow=[ALEX, "any;+;chat123"])
