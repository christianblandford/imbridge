"""HelperServer against a fake helper that speaks the same newline-delimited JSON as the real one."""

import asyncio
import json
import socket

import pytest

from imbridge.protocol import HelperError, HelperNotConnected, HelperServer, HelperUnauthorized

TOKEN = "secret"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def fake_helper(port, handle, events=()):
    """Connect like the injected helper does, ping, then answer each request line with handle(request)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b'{"event": "ping", "message": "Helper Connected!", "process": "com.apple.MobileSMS"}\r\n')
    for event in events:
        writer.write(json.dumps(event).encode() + b"\r\n")
    await writer.drain()
    while line := await reader.readline():
        reply = handle(json.loads(line))
        if reply is None:  # simulate Messages quitting mid-request
            break
        writer.write(json.dumps(reply).encode() + b"\r\n")
        await writer.drain()
    writer.close()


def run(scenario):
    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_request_round_trip_carries_the_token():
    seen = []

    def handle(request):
        seen.append(request)
        return {"transactionId": request["transactionId"], "identifier": "GUID-1"}

    async def scenario():
        server = HelperServer(free_port(), TOKEN)
        await server.start()
        helper = asyncio.create_task(fake_helper(server.port, handle))
        await server.wait_connected(5)
        assert server.process == "com.apple.MobileSMS"
        result = await server.request("send-message", {"chatGuid": "any;-;+15551234567", "message": "hi"})
        assert result["identifier"] == "GUID-1"
        await server.close()
        await helper

    run(scenario)
    assert seen[0]["action"] == "send-message"
    assert seen[0]["token"] == TOKEN
    assert seen[0]["data"]["message"] == "hi"


def test_errors():
    def handle(request):
        error = "unauthorized" if request["action"] == "a" else "Chat does not exist"
        return {"transactionId": request["transactionId"], "error": error}

    async def scenario():
        server = HelperServer(free_port(), TOKEN)
        await server.start()
        helper = asyncio.create_task(fake_helper(server.port, handle))
        await server.wait_connected(5)
        with pytest.raises(HelperUnauthorized):
            await server.request("a")
        with pytest.raises(HelperError, match="Chat does not exist"):
            await server.request("b")
        await server.close()
        await helper

    run(scenario)


def test_disconnect_mid_request():
    async def scenario():
        server = HelperServer(free_port(), TOKEN)
        await server.start()
        helper = asyncio.create_task(fake_helper(server.port, lambda request: None))
        await server.wait_connected(5)
        with pytest.raises(HelperNotConnected):
            await server.request("send-message")
        assert not server.connected
        with pytest.raises(HelperNotConnected):
            await server.request("send-message")
        await server.close()
        await helper

    run(scenario)


def test_events_reach_the_callback():
    received = []

    async def scenario():
        server = HelperServer(free_port(), TOKEN, on_event=received.append)
        await server.start()
        typing = {"event": "started-typing", "guid": "any;-;+15551234567"}
        helper = asyncio.create_task(fake_helper(server.port, lambda request: None, events=[typing]))
        await server.wait_connected(5)
        await asyncio.sleep(0.2)
        await server.close()
        helper.cancel()

    run(scenario)
    assert [event["event"] for event in received] == ["ping", "started-typing"]
