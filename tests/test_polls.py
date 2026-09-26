"""Polls: reading them, their votes, and the tally, from rows shaped like the ones Messages writes."""

import asyncio
import base64
import json
import plistlib
import sqlite3
import uuid

import pytest
from helpers import ALEX, CREW, at
from test_sending import actions, run

from imbridge import IMBridge, PollOption, PollVote, RateLimited, SendNotAllowed, WrongChat
from imbridge.chatdb import ChatDB
from imbridge.cli import describe
from imbridge.polls import POLLS_BUNDLE, parse_poll

SESSION = "6F9619FF-8B86-D011-B42D-00C04FC964FF"
PIZZA, SUSHI, TACOS = "A1", "B2", "C3"  # option ids (Messages uses UUIDs)


def payload(item: dict, *, session: str = SESSION) -> bytes:
    """payload_data as Messages archives it: an NSKeyedArchiver dictionary whose URL holds the poll as base64 JSON."""
    encoded = base64.b64encode(json.dumps({"version": 1, "item": item}).encode()).decode()
    objects = ["$null"]

    def add(value) -> plistlib.UID:
        objects.append(value)
        return plistlib.UID(len(objects) - 1)

    root = {"URL": {"NS.relative": add(f"data:,{encoded}?src=p&c=3")},
            "sessionIdentifier": {"NS.uuidbytes": uuid.UUID(session).bytes},
            "ldtext": "Sent a poll", "an": "Polls"}
    keys = [add(key) for key in root]
    values = [add(value) for value in root.values()]
    objects.append({"NS.keys": keys, "NS.objects": values})
    top = {"root": plistlib.UID(len(objects) - 1)}
    return plistlib.dumps({"$archiver": "NSKeyedArchiver", "$version": 100000, "$top": top, "$objects": objects},
                          fmt=plistlib.FMT_BINARY)


def options(*chosen) -> list[dict]:
    names = {PIZZA: "Pizza", SUSHI: "Sushi", TACOS: "Tacos"}
    return [{"optionIdentifier": o, "text": names[o], "creatorHandle": "sam@example.com"} for o in chosen]


def votes(voter: str, *chosen) -> dict:
    return {"votes": [{"voteOptionIdentifier": o, "participantHandle": voter} for o in chosen]}


def with_poll(path):
    """Sam's poll in the Crew group, as Messages records it, then an added choice and some votes."""
    db = sqlite3.connect(path)
    for column in ("balloon_bundle_id TEXT", "payload_data BLOB"):
        db.execute(f"ALTER TABLE message ADD COLUMN {column}")
    poll = {"title": "", "creatorHandle": "sam@example.com", "orderedPollOptions": options(PIZZA, SUSHI)}
    added = {**poll, "orderedPollOptions": options(PIZZA, SUSHI, TACOS)}
    rows = [
        # guid, handle (0 is you), seconds, text, balloon payload, associated type, associated guid
        ("P1", 2, 10.0, "�", payload(poll), 0, None),
        ("FALLBACK", 2, 10.1, "Sent a poll", None, 0, None),  # Messages' own "Sent a poll", not shown
        ("QUESTION", 2, 10.3, "Lunch where?", None, 0, None),
        ("U1", 0, 12.0, "�", payload(added), 2, "P1"),  # you added Tacos
        ("V1", 2, 13.0, " ", payload(votes("sam@example.com", PIZZA)), 4000, "P1"),
        ("V2", 1, 14.0, " ", payload(votes("+15551234567", PIZZA, SUSHI)), 4000, "U1"),  # two choices
        ("V3", 0, 15.0, " ", payload(votes("+15550002222", TACOS)), 4000, "U1"),
        ("V4", 2, 16.0, " ", payload(votes("sam@example.com", SUSHI)), 4000, "U1"),  # Sam changes his mind
        ("V5", 1, 17.0, " ", payload({"votes": []}), 4000, "U1"),  # and +15551234567 takes theirs back
        ("LATER", 2, 30.0, "Sent a poll", None, 0, None),  # typed by hand, long after: a real message
    ]
    for guid, handle, seconds, text, data, kind, target in rows:
        date = at(0) + int(seconds * 1e9)
        rowid = db.execute(
            "INSERT INTO message (guid, text, handle_id, is_from_me, date, service, balloon_bundle_id, payload_data,"
            " associated_message_type, associated_message_guid) VALUES (?, ?, ?, ?, ?, 'iMessage', ?, ?, ?, ?)",
            (guid, text, handle, int(handle == 0), date, POLLS_BUNDLE if data else None, data, kind, target),
        ).lastrowid
        db.execute("INSERT INTO chat_message_join VALUES (2, ?, ?)", (rowid, date))
    db.commit()
    db.close()
    return path


def test_reading_poll_messages(chat_db):
    db = ChatDB(with_poll(chat_db))
    poll = db.message("P1")
    assert poll.text is None and poll.vote is None
    assert poll.poll.session == SESSION and poll.poll.update_of is None
    sam = "sam@example.com"
    assert poll.poll.options == (PollOption(PIZZA, "Pizza", sam), PollOption(SUSHI, "Sushi", sam))
    assert db.message("U1").poll.update_of == "P1"
    assert db.message("V2").vote == PollVote(SESSION, "U1", (PIZZA, SUSHI))
    assert db.message("V5").vote.options == ()  # a vote taken back


def test_the_fallback_text_is_skipped_like_messages_does(chat_db):
    db = ChatDB(with_poll(chat_db))
    shown = [message.guid for message in db.history(CREW, 50)]
    assert "FALLBACK" not in shown and "QUESTION" in shown
    assert "LATER" in shown  # the same words, typed long after a poll, are a real message
    assert db.message("FALLBACK").text == "Sent a poll"  # still there when asked for by GUID
    assert "FALLBACK" not in [message.guid for message in db.messages_after(0)]


def test_the_tally(chat_db):
    db = ChatDB(with_poll(chat_db))
    for asked in ("P1", "U1", "V2"):  # the poll, its update, or any vote in it
        results = db.poll(asked)
        assert results.guid == "P1" and results.chat_guid == CREW
    assert results.question == "Lunch where?"
    assert [option.text for option in results.options] == ["Pizza", "Sushi", "Tacos"]
    assert results.choices == {"sam@example.com": (SUSHI,), None: (TACOS,)}  # latest vote each; None is you
    assert results.counts() == {"Pizza": 0, "Sushi": 1, "Tacos": 1}
    assert results.voters("Tacos") == (None,) and results.voters(SUSHI) == ("sam@example.com",)
    assert results.to_dict()["options"][2] == {"id": TACOS, "text": "Tacos", "votes": 1, "voters": ["me"]}
    assert db.poll("QUESTION") is None and db.poll("nope") is None


def test_polls_through_the_api(chat_db):
    path = with_poll(chat_db)
    im = IMBridge(chat_db=path, token="t", inject=False)
    assert im.poll("V4").counts()["Sushi"] == 1
    assert im.chat(CREW).poll(im.message("P1")).guid == "P1"
    with pytest.raises(WrongChat):
        im.chat(ALEX).poll("P1")
    lines = {message.guid: describe(message) for message in im.history(CREW)}
    assert lines["P1"].endswith("sent a poll: Pizza / Sushi  <P1>")
    assert "added a choice to a poll: Pizza / Sushi / Tacos" in lines["U1"]
    assert "voted in poll U1" in lines["V2"] and "took back their vote" in lines["V5"]


def test_a_poll_as_a_new_message_in_the_stream(chat_db):
    path = with_poll(chat_db)

    async def first_of_each():
        im = IMBridge(chat_db=path, token="t", inject=False, poll_interval=0.01)
        found, _ = await im.new_messages(since=7, chat=CREW)
        return [message.guid for message in found]

    # U1 and V3 are yours, so left out like your other messages; the fallback never shows
    assert asyncio.run(first_of_each()) == ["P1", "QUESTION", "V1", "V2", "V4", "V5", "LATER"]


def test_unreadable_payloads_are_not_polls():
    assert parse_poll(0, None, b"not a plist") is None
    assert parse_poll(0, None, None) is None
    assert parse_poll(1000, None, payload({"title": "", "orderedPollOptions": []})) is None  # not a poll row type


# --- sending polls and voting, against a stand-in for the helper --------------------------------------------------

def on_my_number(path):
    """Say which of your addresses the fixture's chats are on, as chat.db does: polls and votes name you by it."""
    db = sqlite3.connect(path)
    db.execute("ALTER TABLE chat ADD COLUMN last_addressed_handle TEXT")
    db.execute("UPDATE chat SET last_addressed_handle = '+15550002222'")
    db.commit()
    db.close()
    return path


CREATED = {"send-poll": {"sessionIdentifier": SESSION, "optionIdentifiers": [PIZZA, SUSHI]}}


def test_sending_a_poll(chat_db):
    guid, requests = run(
        on_my_number(chat_db), lambda im: im.chat(CREW).send_poll(["Pizza", " Sushi "], question="Lunch?"), CREATED,
        allow=[CREW],
    )
    assert actions(requests) == ["send-poll", "send-message"]  # the question follows the poll, as Messages sends it
    poll = requests[0]["data"]
    assert (poll["chatGuid"], poll["options"], poll["creatorHandle"]) == (CREW, ["Pizza", "Sushi"], "+15550002222")
    assert requests[1]["data"]["message"] == "Lunch?"
    assert guid == "SENT-1"


def test_a_poll_and_its_question_are_rate_limited_together(chat_db):
    log = []
    with pytest.raises(RateLimited):
        run(on_my_number(chat_db), lambda im: im.send_poll(CREW, ["a", "b"], question="?"), CREATED, log,
            allow=[CREW], max_per_chat=1)
    assert log == []  # refused before either went out


def test_polls_need_options_and_an_allowed_chat(chat_db):
    path = on_my_number(chat_db)
    long = [letter * 900 for letter in "abcde"]
    for options, problem in [(["only one"], "at least two"), (["Tea", "tea"], "different"), (long, "too long")]:
        log = []
        with pytest.raises(ValueError, match=problem):
            run(path, lambda im, options=options: im.send_poll(CREW, options), None, log, allow=[CREW])
        assert log == []
    with pytest.raises(SendNotAllowed):
        run(path, lambda im: im.send_poll(CREW, ["a", "b"]))


def test_votes_carry_your_whole_choice(chat_db):
    path = on_my_number(with_poll(chat_db))  # you already picked Tacos (V3)

    def vote(scenario):
        result, requests = run(path, scenario, allow=[CREW])
        return result, [request["data"] for request in requests]

    _, (sent,) = vote(lambda im: im.vote("P1", "pizza"))  # by text, any case
    assert sent["optionIdentifiers"] == [TACOS, PIZZA]  # your earlier choice stays, as tapping does in Messages
    assert (sent["pollGuid"], sent["sessionIdentifier"], sent["participantHandle"]) == ("U1", SESSION, "+15550002222")
    _, (sent,) = vote(lambda im: im.unvote("V2", TACOS))  # from a vote in the poll, by id
    assert sent["optionIdentifiers"] == []
    _, (sent,) = vote(lambda im: im.chat(CREW).unvote("P1"))  # everything
    assert sent["optionIdentifiers"] == []
    assert vote(lambda im: im.vote("P1", "Tacos")) == (None, [])  # already chosen: nothing to send
    assert vote(lambda im: im.unvote("P1", "Pizza")) == (None, [])  # never chosen
    with pytest.raises(ValueError, match="'Nachos' isn't an option in this poll; it has 'Pizza', 'Sushi', 'Tacos'"):
        vote(lambda im: im.vote("P1", "Nachos"))


def test_votes_stay_in_their_chat(chat_db):
    path = on_my_number(with_poll(chat_db))
    log = []
    with pytest.raises(WrongChat):
        run(path, lambda im: im.chat(ALEX).vote("P1", "Pizza"), None, log, allow=[ALEX, CREW])
    with pytest.raises(SendNotAllowed):  # a poll in a chat that isn't allowed
        run(path, lambda im: im.vote("P1", "Pizza"), None, log, allow=[ALEX])
    assert log == []


def test_your_votes_from_another_of_your_addresses_count_once(chat_db):
    # in a chat with yourself, each vote comes back as a received copy from your own number
    path = on_my_number(with_poll(chat_db))
    db = sqlite3.connect(path)
    db.execute("INSERT INTO handle (id) VALUES ('+15550002222')")
    rowid = db.execute(
        "INSERT INTO message (guid, text, handle_id, is_from_me, date, service, balloon_bundle_id, payload_data,"
        " associated_message_type, associated_message_guid)"
        " VALUES ('ECHO', ' ', 3, 0, ?, 'iMessage', ?, ?, 4000, 'U1')",
        (at(18), POLLS_BUNDLE, payload(votes("+15550002222", TACOS))),
    ).lastrowid
    db.execute("INSERT INTO chat_message_join VALUES (2, ?, ?)", (rowid, at(18)))
    db.commit()
    db.close()
    results = IMBridge(chat_db=path, token="t", inject=False).poll("P1")
    assert results.choices == {"sam@example.com": (SUSHI,), None: (TACOS,)}
