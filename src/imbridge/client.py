"""The high-level API: send, reply, react with any emoji, and receive."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from . import config, messages_app
from .chatdb import Chat, ChatDB, Message
from .protocol import HelperNotConnected, HelperServer, HelperUnauthorized
from .reactions import parse_target, reaction_type

log = logging.getLogger(__name__)

# Messages writes the row linking a message to its chat a moment after the message itself.
CHAT_LINK_GRACE = 2.0

# Friendly names for Messages' send effects (bubble effects first, then screen effects).
EFFECTS = {
    "slam": "com.apple.MobileSMS.expressivesend.impact",
    "loud": "com.apple.MobileSMS.expressivesend.loud",
    "gentle": "com.apple.MobileSMS.expressivesend.gentle",
    "invisible_ink": "com.apple.MobileSMS.expressivesend.invisibleink",
    "echo": "com.apple.messages.effect.CKEchoEffect",
    "spotlight": "com.apple.messages.effect.CKSpotlightEffect",
    "balloons": "com.apple.messages.effect.CKHappyBirthdayEffect",
    "confetti": "com.apple.messages.effect.CKConfettiEffect",
    "love": "com.apple.messages.effect.CKHeartEffect",
    "lasers": "com.apple.messages.effect.CKLasersEffect",
    "fireworks": "com.apple.messages.effect.CKFireworksEffect",
    "celebration": "com.apple.messages.effect.CKSparklesEffect",
}


class ChatNotFound(LookupError):
    """No existing conversation matches the chat, phone number or email given."""


class IMBridge:
    """iMessage through Messages.app itself: the helper inside Messages sends, chat.db tells us what arrived.

        async with IMBridge() as im:
            guid = await im.send("+15551234567", "hi")
            await im.react(guid, "👀")
            async for message in im.messages():
                if message.text:
                    await im.reply(message, "got it")

    With inject=True (the default), start() relaunches Messages with the helper loaded when no helper answers.
    Receiving (messages, history, chats) reads chat.db only and works without the helper.
    """

    def __init__(
        self,
        *,
        port: int | None = None,
        token: str | None = None,
        dylib: Path | str | None = None,
        chat_db: Path | str = config.CHAT_DB,
        inject: bool = True,
        poll_interval: float = 0.5,
    ) -> None:
        self.dylib = Path(dylib) if dylib else config.helper_dylib()
        self.inject = inject
        self.poll_interval = poll_interval
        self.db = ChatDB(chat_db)
        self._server = HelperServer(port or config.helper_port(), token or config.helper_token())
        # The chat of each message we sent, so replies and tapbacks work before chat.db catches up.
        self._chat_of: dict[str, str] = {}

    async def __aenter__(self) -> IMBridge:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def start(self, timeout: float = 45.0) -> None:
        """Listen for the helper and make sure one is connected, injecting it into Messages if needed."""
        await self._ensure_helper(timeout)

    async def close(self) -> None:
        await self._server.close()
        self.db.close()

    @property
    def connected(self) -> bool:
        return self._server.connected

    # --- sending -------------------------------------------------------------------------------------------------

    async def send(
        self,
        chat: str,
        text: str,
        *,
        reply_to: str | None = None,
        effect: str | None = None,
        subject: str | None = None,
    ) -> str:
        """Send text to a chat (a chat GUID, or the phone number / email of an existing conversation).

        Returns the new message's GUID. reply_to makes it an inline reply to that message GUID; effect is a key of
        EFFECTS (or a raw Messages effect identifier).
        """
        guid, part = parse_target(reply_to) if reply_to else (None, 0)
        chat_guid = self.resolve_chat(chat)
        result = await self._request(
            "send-message",
            {
                "chatGuid": chat_guid,
                "subject": subject,
                "message": text,
                "attributedBody": None,
                "effectId": EFFECTS.get(effect, effect) if effect else None,
                "selectedMessageGuid": guid,
                "partIndex": part,
                "ddScan": 0,
            },
        )
        sent = result.get("identifier")
        if sent:
            if len(self._chat_of) >= 1000:
                self._chat_of.clear()
            self._chat_of[sent] = chat_guid
        return sent

    async def reply(self, message: Message | str, text: str, *, chat: str | None = None) -> str:
        """Reply inline (threaded) to a message: a Message, or a message GUID."""
        guid, chat_guid, _ = await self._target(message, chat)
        return await self.send(chat_guid, text, reply_to=guid)

    async def react(
        self, message: Message | str, reaction: str, *, remove: bool = False, chat: str | None = None
    ) -> str:
        """Tapback a message with a classic reaction (love, like, dislike, laugh, emphasize, question) or any emoji.

        iMessage keeps one tapback per person per message, so a new one replaces your previous one.
        """
        guid, chat_guid, part = await self._target(message, chat)
        result = await self._request(
            "send-reaction",
            {
                "chatGuid": chat_guid,
                "selectedMessageGuid": guid,
                "reactionType": reaction_type(reaction, remove),
                "partIndex": part,
            },
        )
        return result.get("identifier")

    async def typing(self, chat: str, on: bool = True) -> None:
        """Show (or stop showing) the typing indicator in a chat."""
        await self._request("start-typing" if on else "stop-typing", {"chatGuid": self.resolve_chat(chat)})

    async def mark_read(self, chat: str) -> None:
        await self._request("mark-chat-read", {"chatGuid": self.resolve_chat(chat)})

    async def account(self) -> dict[str, Any]:
        """The signed-in account (apple_id, login_status_message, aliases, ...); also proves the helper answers."""
        result = await self._request("get-account-info", {})
        return {key: value for key, value in result.items() if key != "transactionId"}

    # --- receiving -----------------------------------------------------------------------------------------------

    async def messages(self, *, since: int | None = None, include_from_me: bool = False) -> AsyncIterator[Message]:
        """New messages as they arrive, tapbacks and inline replies included.

        Starts from now, or after the given chat.db ROWID. Your own messages are skipped unless include_from_me.
        """
        last = since if since is not None else await asyncio.to_thread(self.db.max_rowid)
        first_seen: dict[int, float] = {}
        while True:
            batch = await asyncio.to_thread(self.db.messages_after, last)
            waiting = False
            for message in batch:
                if message.rowid <= last:  # a message linked to two chats appears twice
                    continue
                if message.chat_guid is None:
                    seen = first_seen.setdefault(message.rowid, time.monotonic())
                    if time.monotonic() - seen < CHAT_LINK_GRACE:
                        waiting = True  # read it again once its chat link lands
                        break
                first_seen.pop(message.rowid, None)
                last = message.rowid
                if include_from_me or not message.is_from_me:
                    yield message
            if waiting or not batch:
                await asyncio.sleep(self.poll_interval)

    def history(self, chat: str, limit: int = 50) -> list[Message]:
        return self.db.history(self.resolve_chat(chat), limit)

    def chats(self, limit: int = 50) -> list[Chat]:
        return self.db.chats(limit)

    def message(self, guid: str) -> Message | None:
        return self.db.message(parse_target(guid)[0])

    def resolve_chat(self, chat: str) -> str:
        """A chat GUID as-is, or the chat GUID of the existing conversation with a phone number or email."""
        if ";" in chat:
            return chat
        if guid := self.db.chat_for_handle(chat):
            return guid
        raise ChatNotFound(f"no existing conversation with {chat}; start one in Messages first")

    # --- internals -----------------------------------------------------------------------------------------------

    async def _target(self, message: Message | str, chat: str | None) -> tuple[str, str, int]:
        if isinstance(message, Message):
            guid, part, chat_guid = message.guid, 0, chat or message.chat_guid
        else:
            guid, part = parse_target(message)
            chat_guid = chat or self._chat_of.get(guid)
        if not chat_guid:
            chat_guid = await self._chat_from_db(guid)
        return guid, self.resolve_chat(chat_guid), part

    async def _chat_from_db(self, guid: str) -> str:
        # A message sent a moment ago may not be in chat.db yet, or not linked to its chat yet.
        deadline = time.monotonic() + CHAT_LINK_GRACE
        while True:
            found = await asyncio.to_thread(self.db.message, guid)
            if found is not None and found.chat_guid:
                return found.chat_guid
            if time.monotonic() > deadline:
                raise ChatNotFound(f"can't find which chat message {guid} belongs to")
            await asyncio.sleep(0.2)

    async def _request(self, action: str, data: dict[str, Any]) -> dict[str, Any]:
        await self._ensure_helper()
        try:
            return await self._server.request(action, data)
        except HelperUnauthorized:
            # Messages is running a helper started with another token; restart it with ours.
            if not self.inject:
                raise
            await self._relaunch()
        except HelperNotConnected:
            await self._ensure_helper()
        return await self._server.request(action, data)

    async def _ensure_helper(self, timeout: float = 45.0) -> None:
        if not self._server.listening:
            await self._server.start()
        if self._server.connected:
            return
        try:
            await self._server.wait_connected(timeout=3)  # an injected helper redials every second
            return
        except (TimeoutError, asyncio.TimeoutError):  # the same class from Python 3.11 on
            if not self.inject:
                raise HelperNotConnected(
                    "no helper answered; run `imbridge start` (or use inject=True) to load it into Messages"
                ) from None
        await self._relaunch(timeout)

    async def _relaunch(self, timeout: float = 45.0) -> None:
        log.info("launching Messages with the helper")
        env = {"IMBRIDGE_PORT": str(self._server.port), "IMBRIDGE_TOKEN": self._server.token or ""}
        await asyncio.to_thread(messages_app.launch_with_helper, self.dylib, env)
        await self._server.wait_connected(timeout=timeout)
