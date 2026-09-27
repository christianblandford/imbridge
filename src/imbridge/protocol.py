"""The socket protocol between imbridge and its helper inside Messages.app.

The helper (helper/ in the repository) runs inside Messages, injected with DYLD_INSERT_LIBRARIES. Once loaded it
connects *to us* on localhost (IMBRIDGE_PORT) and speaks newline-delimited JSON:

    us -> helper   {"action": "...", "data": {...}, "transactionId": "...", "token": "..."}\\n
    helper -> us   {"transactionId": "...", "identifier": "...", "error": "..."}\\r\\n
                   {"event": "ping" | "started-typing" | ..., ...}\\r\\n

Replies carry their request's transactionId. When Messages was launched with IMBRIDGE_TOKEN, requests without that
token are answered with {"error": "unauthorized"}. Only one request is in flight at a time: some actions report the
chat's last sent message as their result, which concurrent sends could mix up.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)


class HelperError(RuntimeError):
    """The helper answered a request with an error."""


class HelperNotConnected(HelperError):
    """No helper is connected, or it disconnected mid-request."""


class HelperBusy(HelperNotConnected):
    """Another program on this Mac is using the helper; only one imbridge program can send at a time. Nothing was
    sent, so it's safe to retry once that program is done."""


class HelperUnauthorized(HelperError):
    """The helper in Messages was launched with a different token, or by another tool."""


class HelperServer:
    """Accepts the helper's connection and turns actions into awaitable requests."""

    def __init__(
        self,
        port: int,
        token: str | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.port = port
        self.token = token
        self.on_event = on_event
        self.process: str | None = None  # bundle id the connected helper reported in its ping
        self.build: str | None = None  # which helper build it said it is (None: one from before builds were named)
        self._server: asyncio.Server | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._ready = asyncio.Event()
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._request_lock = asyncio.Lock()

    async def start(self) -> None:
        # The helper dials "localhost", which can resolve to either address family.
        try:
            self._server = await asyncio.start_server(self._handle_client, host=["127.0.0.1", "::1"], port=self.port)
        except OSError:
            if not self._ipv4_only_host():
                raise
            self._server = await asyncio.start_server(self._handle_client, host="127.0.0.1", port=self.port)

    @staticmethod
    def _ipv4_only_host() -> bool:
        import socket

        try:
            with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
                probe.bind(("::1", 0))
        except OSError:
            return True
        return False

    async def close(self) -> None:
        if self._writer is not None:
            self._drop_connection(self._writer)
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def listening(self) -> bool:
        return self._server is not None

    @property
    def connected(self) -> bool:
        return self._ready.is_set()

    async def wait_connected(self, timeout: float | None = None) -> None:
        await asyncio.wait_for(self._ready.wait(), timeout)

    async def request(
        self, action: str, data: dict[str, Any] | None = None, timeout: float = 30.0
    ) -> dict[str, Any]:
        """Send one action and wait for the reply carrying the same transactionId."""
        async with self._request_lock:
            writer = self._writer
            if writer is None or not self._ready.is_set():
                raise HelperNotConnected("the helper in Messages is not connected")
            transaction_id = str(uuid.uuid4())
            reply: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
            self._pending[transaction_id] = reply
            try:
                message = {"action": action, "data": data or {}, "transactionId": transaction_id}
                if self.token:
                    message["token"] = self.token
                writer.write(json.dumps(message).encode() + b"\n")
                await writer.drain()
                result = await asyncio.wait_for(reply, timeout)
            finally:
                self._pending.pop(transaction_id, None)
        if error := result.get("error"):
            if error == "unauthorized":
                raise HelperUnauthorized("the helper in Messages was started with a different token")
            raise HelperError(f"{action}: {error}")
        return result

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        log.debug("helper connected from %s", peer)
        if self._writer is not None:
            # A relaunched Messages can reconnect before the old socket notices it's dead.
            self._drop_connection(self._writer)
        self._writer = writer
        buffer = b""
        try:
            while chunk := await reader.read(65536):
                buffer += chunk
                *lines, buffer = buffer.split(b"\n")
                for line in lines:
                    if line.strip():
                        self._dispatch(line)
        except ConnectionError:
            pass
        finally:
            log.debug("helper disconnected (%s)", peer)
            self._drop_connection(writer)

    def _drop_connection(self, writer: asyncio.StreamWriter) -> None:
        writer.close()
        if self._writer is not writer:
            return
        self._writer = None
        self._ready.clear()
        self.process = None
        self.build = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(HelperNotConnected("the helper disconnected"))

    def _dispatch(self, line: bytes) -> None:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            log.warning("undecodable line from the helper: %r", line[:200])
            return
        if not isinstance(message, dict):
            return
        if transaction_id := message.get("transactionId"):
            future = self._pending.get(transaction_id)
            if future is not None and not future.done():
                future.set_result(message)
            return
        if message.get("event") == "ping":
            self.process = message.get("process")
            self.build = message.get("build")
            self._ready.set()
        if self.on_event is not None:
            self.on_event(message)
