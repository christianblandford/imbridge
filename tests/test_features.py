"""What this Mac's macOS supports: supports(), refusing features it doesn't have, and the MCP tools it offers."""

import asyncio
from datetime import datetime, timedelta

import pytest
from helpers import CREW
from test_sending import run

from imbridge import FEATURES, IMBridge, Unsupported


def on_macos(monkeypatch, major, minor=0):
    monkeypatch.setattr("imbridge.client.macos_version", lambda: (major, minor))


def test_supports(monkeypatch, chat_db):
    im = IMBridge(chat_db=chat_db, token="t", inject=False)
    on_macos(monkeypatch, 15, 5)  # Sequoia: Send Later and emoji tapbacks, but no polls
    assert (im.supports("send_later"), im.supports("emoji_tapbacks"), im.supports("polls")) == (True, True, False)
    on_macos(monkeypatch, 26)
    assert all(im.supports(feature) for feature in FEATURES)
    with pytest.raises(ValueError, match="unknown feature"):
        im.supports("teleport")


def test_features_this_macos_lacks_are_refused_before_anything_is_sent(monkeypatch, chat_db):
    on_macos(monkeypatch, 15, 5)
    log = []
    with pytest.raises(Unsupported, match="macOS 26 or later on this Mac, which has 15.5"):
        run(chat_db, lambda im: im.send_poll(CREW, ["a", "b"]), None, log, allow=[CREW])
    with pytest.raises(Unsupported):
        run(chat_db, lambda im: im.vote("POLL", "a"), None, log, allow=[CREW])
    on_macos(monkeypatch, 14)
    later = datetime.now() + timedelta(hours=1)
    with pytest.raises(Unsupported, match="Send Later"):
        run(chat_db, lambda im: im.send_later(CREW, "hi", later), None, log, allow=[CREW])
    assert log == []


def test_the_mcp_server_offers_what_this_macos_has(monkeypatch, chat_db):
    pytest.importorskip("mcp")
    from imbridge.mcp_server import build_server

    def tools_on(major):
        on_macos(monkeypatch, major)
        server = build_server(IMBridge(chat_db=chat_db, token="t", inject=False))
        return {tool.name for tool in asyncio.run(server.list_tools())}, server.instructions

    tools, instructions = tools_on(26)
    assert {"read_poll", "send_poll", "vote", "send_later"} <= tools and "read_poll" in instructions
    tools, instructions = tools_on(15)
    assert not {"read_poll", "send_poll", "vote"} & tools and "read_poll" not in instructions
    assert {"send_later", "list_scheduled", "cancel_scheduled", "send_message"} <= tools
    tools, instructions = tools_on(14)
    assert not {"send_later", "list_scheduled", "cancel_scheduled"} & tools and "send_later" not in instructions
