"""Formatted text and @mentions: what reaches the helper, and what's refused before anything is sent."""

import pytest
from helpers import ALEX, CREW
from test_sending import actions, run

from imbridge import ANY_CHAT, NewContact, Span
from imbridge.richtext import parts, plain, spans, text_effect


def test_spans():
    text = ["Meeting ", Span("moved", bold=True, italic=True), " to ", Span("3pm", effect="Big")]
    assert plain(text) == "Meeting moved to 3pm"
    assert parts(spans(text)) == [
        {"partIndex": 0, "text": "Meeting ", "styles": [], "effect": None, "mention": None},
        {"partIndex": 0, "text": "moved", "styles": ["bold", "italic"], "effect": None, "mention": None},
        {"partIndex": 0, "text": " to ", "styles": [], "effect": None, "mention": None},
        {"partIndex": 0, "text": "3pm", "styles": [], "effect": "big", "mention": None},
    ]
    assert spans("plain") is None and spans(["only ", "strings"]) is None  # nothing to format: a plain send


def test_text_effects_are_checked_up_front():
    assert text_effect("shake") == "shakeHorizontal" and text_effect("Ripple") == "bounce"
    assert text_effect("scaleRipple") == "scaleRipple"  # Messages' own names work too
    with pytest.raises(ValueError, match="isn't a text effect"):  # Messages would silently send plain text
        Span("hey", effect="wobble")
    with pytest.raises(ValueError):
        Span("")


def test_sending_formatted_text(chat_db):
    text = ["see ", Span("this", underline=True)]
    guid, requests = run(chat_db, lambda im: im.chat(ALEX).reply("M1", text), allow=[ALEX])
    (request,) = requests
    assert request["action"] == "send-multipart"
    data = request["data"]
    assert (data["chatGuid"], data["selectedMessageGuid"]) == (ALEX, "M1")
    assert [part["text"] for part in data["parts"]] == ["see ", "this"]
    assert data["parts"][1]["styles"] == ["underline"]
    assert guid == "SENT-1"


def test_plain_text_still_goes_as_plain_text(chat_db):
    _, requests = run(chat_db, lambda im: im.send(ALEX, ["just ", "text"]), allow=[ALEX])
    assert actions(requests) == ["send-message"] and requests[0]["data"]["message"] == "just text"


def test_mentions_are_only_for_people_in_the_chat(chat_db):
    _, requests = run(chat_db, lambda im: im.send(CREW, [Span("Sam", mention="sam@example.com"), " you in?"]),
                      allow=[CREW])
    assert requests[0]["data"]["parts"][0]["mention"] == "sam@example.com"
    log = []
    with pytest.raises(ValueError, match="isn't in"):
        run(chat_db, lambda im: im.send(ALEX, [Span("Sam", mention="sam@example.com")]), None, log, allow=[ALEX])
    assert log == []


def test_new_conversations_start_plain(chat_db):
    log = []
    with pytest.raises(ValueError, match="plain text"):
        run(chat_db, lambda im: im.send("+15550009999", [Span("hi", bold=True)]), None, log,
            allow=[NewContact("+15550009999")])
    assert log == []
    _, requests = run(chat_db, lambda im: im.send("+15550009999", ["hi ", "there"]),
                      {"check-imessage-availability": {"available": True}}, allow=ANY_CHAT)
    assert requests[-1]["data"]["message"] == "hi there"
