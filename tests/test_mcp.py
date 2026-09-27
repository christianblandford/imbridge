"""The MCP server's tools, called in-process against the fixture chat.db. None of them reaches Messages."""

import asyncio
import sqlite3
import time

import pytest

pytest.importorskip("mcp")

from helpers import ALEX, CREW, at  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from imbridge import IMBridge  # noqa: E402
from imbridge.mcp_server import build_server  # noqa: E402


def server_for(path, **kwargs):
    return build_server(IMBridge(chat_db=path, token="t", inject=False, poll_interval=0.01, **kwargs))


def call(server, tool, **arguments):
    result = asyncio.run(server.call_tool(tool, arguments))
    return result.structured_content["result"] if "result" in result.structured_content else result.structured_content


def add_message(path, guid, text, t):
    db = sqlite3.connect(path)
    rowid = db.execute(
        "INSERT INTO message (guid, text, handle_id, is_from_me, date, service) VALUES (?, ?, 1, 0, ?, 'iMessage')",
        (guid, text, at(t)),
    ).lastrowid
    db.execute("INSERT INTO chat_message_join VALUES (1, ?, ?)", (rowid, at(t)))
    db.commit()
    db.close()


def test_tools_are_described_and_annotated(chat_db):
    tools = {tool.name: tool for tool in asyncio.run(server_for(chat_db).list_tools())}
    assert set(tools) == {
        "list_chats", "read_messages", "search_messages", "check_messages", "read_poll", "send_message", "reply",
        "react",
        "send_poll", "vote", "send_location", "send_link", "send_voice_message", "message_status", "send_later",
        "list_scheduled", "cancel_scheduled",
        "edit_message", "unsend_message", "show_typing", "typing_status", "focus_status", "whoami",
    }
    assert all(tool.description for tool in tools.values())
    assert tools["read_messages"].annotations.read_only_hint
    assert tools["send_message"].annotations.open_world_hint
    assert tools["unsend_message"].annotations.destructive_hint
    assert "confetti" in tools["send_message"].description
    assert "love" in tools["react"].description


def test_list_and_read(chat_db):
    server = server_for(chat_db)
    chats = call(server, "list_chats")
    assert [chat["chat"] for chat in chats] == [CREW, ALEX]
    assert not any(chat["can_send"] for chat in chats)
    messages = call(server, "read_messages", chat="555-123-4567")
    assert [message["guid"] for message in messages] == ["M1", "M2", "M3", "M4"]
    assert messages[2]["reply_to"] == "M2"
    assert messages[3]["tapback"] == {"reaction": "love", "removed": False, "on": "M1"}


def test_paging_and_search(chat_db):
    server = server_for(chat_db)
    assert [chat["chat"] for chat in call(server, "list_chats", limit=1, offset=1)] == [ALEX]
    older = call(server, "read_messages", chat=ALEX, limit=2, before="M3")
    assert [message["guid"] for message in older] == ["M1", "M2"]
    around = call(server, "read_messages", chat=ALEX, limit=1, after="M1")
    assert [message["guid"] for message in around] == ["M2"]
    found = call(server, "search_messages", query="HEY")
    assert [(message["guid"], message["chat"]) for message in found] == [("M1", ALEX)]
    assert call(server, "search_messages", query="hey", chat="Crew") == []
    with pytest.raises(ToolError):
        call(server, "read_messages", chat=ALEX, before="NOT-A-MESSAGE")


def test_check_messages_returns_each_new_message_once(chat_db):
    server = server_for(chat_db)
    assert call(server, "check_messages") == []  # only what arrives after the server started
    add_message(chat_db, "M8", "new!", 8)
    assert [message["guid"] for message in call(server, "check_messages", chat=ALEX)] == ["M8"]
    assert [message["guid"] for message in call(server, "check_messages")] == ["M8"]  # each view has its own place
    assert call(server, "check_messages") == []


def test_check_messages_can_wait_for_a_reply(chat_db):
    server = server_for(chat_db)

    async def scenario():
        waiting = asyncio.ensure_future(server.call_tool("check_messages", {"wait_seconds": 5}))
        await asyncio.sleep(0.2)
        add_message(chat_db, "M9", "a reply", 9)
        started = time.monotonic()
        result = await waiting
        return result.structured_content["result"], time.monotonic() - started

    found, waited = asyncio.run(scenario())
    assert [message["guid"] for message in found] == ["M9"]
    assert waited < 2  # returned when the message arrived, not at the end of the wait


def test_sends_to_chats_that_arent_allowed_are_refused_before_messages_is_touched(chat_db):
    server = server_for(chat_db)
    for tool, arguments in [
        ("send_message", {"chat": ALEX, "text": "hi"}),
        ("reply", {"message_guid": "M1", "text": "hi"}),
        ("react", {"message_guid": "M1", "reaction": "👀"}),
        ("show_typing", {"chat": ALEX}),
        ("send_poll", {"chat": ALEX, "options": ["a", "b"]}),
        ("send_later", {"chat": ALEX, "text": "hi", "at": "2099-01-01T09:00"}),
    ]:
        started = time.monotonic()
        with pytest.raises(ToolError, match="Only the user can allow a chat"):
            asyncio.run(server.call_tool(tool, arguments))
        assert time.monotonic() - started < 1  # refused right away: no waiting for, or launching, the helper


def test_whoami(chat_db):
    info = call(server_for(chat_db, allow=[CREW]), "whoami")
    assert [chat["chat"] for chat in info["allowed_chats"]] == [CREW]
    assert info["address"] is None


def test_read_poll(chat_db):
    from test_polls import with_poll

    server = server_for(with_poll(chat_db))
    poll = call(server, "read_poll", message_guid="V2")  # from a vote in it
    assert poll["question"] == "Lunch where?"
    tally = [(option["text"], option["votes"]) for option in poll["options"]]
    assert tally == [("Pizza", 0), ("Sushi", 1), ("Tacos", 1)]
    assert poll["options"][2]["voters"] == ["me"]
    with pytest.raises(ToolError, match="isn't a poll"):
        asyncio.run(server.call_tool("read_poll", {"message_guid": "QUESTION"}))
    shown = {message["guid"]: message for message in call(server, "read_messages", chat=CREW, limit=50)}
    assert shown["P1"]["poll"] == {"options": ["Pizza", "Sushi"]}
    assert shown["V5"]["vote"] == {"in_poll": "U1", "took_back": True}


def test_text_effects_are_checked_before_sending(chat_db):
    server = server_for(chat_db, allow=[ALEX])
    with pytest.raises(ToolError, match="isn't a text effect"):
        asyncio.run(server.call_tool("send_message", {"chat": ALEX, "text": "hi", "text_effect": "wobble"}))
    assert "explode" in {tool.name: tool for tool in asyncio.run(server.list_tools())}["send_message"].description
