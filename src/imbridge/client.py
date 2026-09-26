"""The high-level API: chats, sending (only where you've allowed it), tapbacks, and receiving."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config, messages_app
from .addresses import ANY_ADDRESS, AddressNotChosen, AnyAddress, WrongAddress, address_key, is_phone
from .chatdb import ChatDB, ChatInfo, Message
from .guard import AnyChat, SendGuard
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
    "shooting_star": "com.apple.messages.effect.CKShootingStarEffect",
}


class ChatNotFound(LookupError):
    """No existing conversation matches the chat, phone number or email given."""


class WrongChat(ValueError):
    """A Chat was asked to reply or react to a message from another chat."""


def _target_ref(guid: str, part: int) -> str:
    return f"p:{part}/{guid}" if part else guid


def _choose_address(phones: list[str], what: str) -> str:
    return (
        f"{what}: {', '.join(phones)}. Tell imbridge which one this program is, e.g. "
        f'IMBridge(address="{phones[-1]}") or IMBRIDGE_ADDRESS={phones[-1]}, or use address=ANY_ADDRESS to '
        "deliberately take every number."
    )


class Chat:
    """One conversation. Everything done through it stays in it.

        chat = im.chat("+15551234567")
        async for message in chat.messages():  # only this chat's messages
            await chat.reply(message, "on it")  # raises WrongChat for a message from any other chat

    Reading always works. Sending needs the chat allowed (IMBridge(allow=[...]) or `imbridge allow`).
    """

    def __init__(self, bridge: IMBridge, info: ChatInfo) -> None:
        self._bridge = bridge
        self.info = info

    @property
    def guid(self) -> str:
        return self.info.guid

    @property
    def name(self) -> str | None:
        return self.info.name

    @property
    def is_group(self) -> bool:
        return self.info.is_group

    @property
    def participants(self) -> tuple[str, ...]:
        return self.info.participants

    @property
    def last_message_at(self) -> datetime | None:
        return self.info.last_message_at

    @property
    def address(self) -> str | None:
        """Which of your addresses this conversation is on, which is the one Messages sends from."""
        return self.info.address

    @property
    def can_send(self) -> bool:
        """Whether imbridge is allowed to send here."""
        return self._bridge._guard.allows(self.guid)

    def __repr__(self) -> str:
        return f"Chat({self.name or ', '.join(self.participants) or self.guid!r})"

    def to_dict(self) -> dict[str, Any]:
        return self.info.to_dict()

    async def messages(self, *, since: int | None = None, include_from_me: bool = False) -> AsyncIterator[Message]:
        """This chat's new messages, tapbacks and inline replies as they arrive (from now, or after a ROWID)."""
        async for message in self._bridge._stream(since, include_from_me, self.guid):
            yield message

    def history(self, limit: int = 50) -> list[Message]:
        """This chat's latest messages, oldest first."""
        return self._bridge.history(self.guid, limit)

    async def send(self, text: str, *, effect: str | None = None, subject: str | None = None) -> str:
        return await self._bridge.send(self.guid, text, effect=effect, subject=subject)

    async def reply(self, message: Message | str, text: str) -> str:
        """Reply inline to one of this chat's messages (a Message or its GUID)."""
        guid, part = await self._own(message)
        return await self._bridge.send(self.guid, text, reply_to=_target_ref(guid, part))

    async def react(self, message: Message | str, reaction: str, *, remove: bool = False) -> str:
        """Tapback one of this chat's messages with a classic reaction or any emoji."""
        guid, part = await self._own(message)
        return await self._bridge._react(self.guid, guid, part, reaction, remove)

    async def typing(self, on: bool = True) -> None:
        await self._bridge.typing(self.guid, on)

    async def mark_read(self) -> None:
        await self._bridge.mark_read(self.guid)

    async def _own(self, message: Message | str) -> tuple[str, int]:
        guid, chat_guid, part = await self._bridge._target(message, None)
        if chat_guid != self.guid:
            raise WrongChat(f"message {guid} is in {chat_guid}, not in this chat ({self.guid})")
        return guid, part


class IMBridge:
    """iMessage through Messages.app itself: a helper inside Messages sends, chat.db says what arrived.

    imbridge only sends to chats you've allowed (in code with allow=[...], or with `imbridge allow`) and rate-limits
    what it sends, so a bug can't message your contacts. Reading works for every chat.

        async with IMBridge(allow=["+15551234567", "Family"], address="+15550001111") as im:
            chat = im.chat("+15551234567")
            async for message in chat.messages():
                await chat.reply(message, "got it")

    allow: the chats this program may send to (phone numbers, emails, group names or chat GUIDs), on top of any
        allowed with `imbridge allow`; ANY_CHAT allows every chat. start() checks that each one matches a chat.
    address: which of your own addresses this program is, when your Apple ID has several (say, two iPhones with
        two numbers). It only sees messages sent to that address and only sends in chats on it. Defaults to
        IMBRIDGE_ADDRESS. If unset and messages arrive at more than one of your phone numbers, imbridge raises
        AddressNotChosen rather than guess; ANY_ADDRESS deliberately takes them all.
    max_per_chat, max_total: messages and tapbacks per minute, per chat and in total, across all imbridge processes.
    inject: load the helper into Messages (restarting Messages) when none answers.
    """

    def __init__(
        self,
        *,
        allow: Iterable[str] | AnyChat = (),
        address: str | AnyAddress | None = None,
        max_per_chat: int = 10,
        max_total: int = 30,
        port: int | None = None,
        token: str | None = None,
        dylib: Path | str | None = None,
        chat_db: Path | str = config.CHAT_DB,
        inject: bool = True,
        poll_interval: float = 0.5,
    ) -> None:
        if address is None and (env := os.environ.get("IMBRIDGE_ADDRESS", "").strip()):
            address = ANY_ADDRESS if env.lower() == "any" else env
        self.address = address
        self._address_key = address_key(address) if isinstance(address, str) else None
        self._pinned_phone: str | None = None  # with no address set: the one phone number seen so far
        self._address_checked = False
        self.dylib = Path(dylib) if dylib else config.helper_dylib()
        self.inject = inject
        self.poll_interval = poll_interval
        self.db = ChatDB(chat_db)
        self._guard = SendGuard(allow, resolve=self.resolve_chat, max_per_chat=max_per_chat, max_total=max_total)
        self._server = HelperServer(port or config.helper_port(), token or config.helper_token())
        # The chat of each message we sent, so replies and tapbacks work before chat.db catches up.
        self._chat_of: dict[str, str] = {}

    async def __aenter__(self) -> IMBridge:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def start(self, timeout: float = 45.0) -> None:
        """Check the allowlist and your address, then make sure a helper is connected (injecting it if needed)."""
        await asyncio.to_thread(self._guard.resolve_allowed)  # a typo in allow=[...] fails here, not at a send
        await asyncio.to_thread(self._check_address)
        await self._ensure_helper(timeout)

    async def close(self) -> None:
        await self._server.close()
        self.db.close()

    @property
    def connected(self) -> bool:
        return self._server.connected

    # --- chats ---------------------------------------------------------------------------------------------------

    def chat(self, chat: str) -> Chat:
        """One conversation: a phone number or email (its one-to-one chat), a group's name, or a chat GUID."""
        guid = self.resolve_chat(chat)
        return Chat(self, self.db.chat(guid) or ChatInfo(guid, None, None, None, False, (), None))

    def chats(self, limit: int = 50) -> list[Chat]:
        """Chats, most recently active first."""
        return [Chat(self, info) for info in self.db.chats(limit)]

    def resolve_chat(self, chat: str) -> str:
        """The chat GUID for a chat GUID, a phone number or email (its one-to-one chat), or a group's name."""
        if ";" in chat:
            return chat
        if guid := self.db.chat_for_handle(chat):
            return guid
        named = self.db.chats_named(chat)
        if len(named) == 1:
            return named[0]
        if named:
            raise ChatNotFound(f"{len(named)} chats are named {chat!r}; use one of their GUIDs: {', '.join(named)}")
        raise ChatNotFound(f"no existing conversation with or named {chat!r}; start one in Messages first")

    # --- sending (allowed chats only) ----------------------------------------------------------------------------

    async def send(
        self,
        chat: str,
        text: str,
        *,
        reply_to: str | None = None,
        effect: str | None = None,
        subject: str | None = None,
    ) -> str:
        """Send text to an allowed chat (a chat GUID, a phone number or email, or a group's name).

        Returns the new message's GUID. reply_to makes it an inline reply to that message GUID; effect is a key of
        EFFECTS (or a raw Messages effect identifier).
        """
        chat_guid = self.resolve_chat(chat)
        self._check_send(chat_guid)
        self._guard.record_send(chat_guid)
        guid, part = parse_target(reply_to) if reply_to else (None, 0)
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
        """Reply inline (threaded) to a message in an allowed chat: a Message, or a message GUID."""
        guid, chat_guid, part = await self._target(message, chat)
        return await self.send(chat_guid, text, reply_to=_target_ref(guid, part))

    async def react(
        self, message: Message | str, reaction: str, *, remove: bool = False, chat: str | None = None
    ) -> str:
        """Tapback a message in an allowed chat: a classic reaction (love, like, dislike, laugh, emphasize, question)
        or any emoji. iMessage keeps one tapback per person per message, so a new one replaces your previous one.
        """
        guid, chat_guid, part = await self._target(message, chat)
        return await self._react(chat_guid, guid, part, reaction, remove)

    async def typing(self, chat: str, on: bool = True) -> None:
        """Show (or stop showing) the typing indicator in an allowed chat."""
        chat_guid = self.resolve_chat(chat)
        self._check_send(chat_guid)
        await self._request("start-typing" if on else "stop-typing", {"chatGuid": chat_guid})

    async def mark_read(self, chat: str) -> None:
        """Mark an allowed chat as read (the other side sees a read receipt if you send them)."""
        chat_guid = self.resolve_chat(chat)
        self._check_send(chat_guid)
        await self._request("mark-chat-read", {"chatGuid": chat_guid})

    async def account(self) -> dict[str, Any]:
        """The signed-in account (apple_id, login_status_message, aliases, ...); also proves the helper answers."""
        result = await self._request("get-account-info", {})
        return {key: value for key, value in result.items() if key != "transactionId"}

    # --- receiving -----------------------------------------------------------------------------------------------

    async def all_messages(self, *, since: int | None = None, include_from_me: bool = False) -> AsyncIterator[Message]:
        """New messages from every chat as they arrive, tapbacks and inline replies included.

        For a bot that answers in one conversation, use chat.messages() instead. Starts from now, or after the given
        chat.db ROWID; your own messages are skipped unless include_from_me.
        """
        async for message in self._stream(since, include_from_me, None):
            yield message

    def history(self, chat: str, limit: int = 50) -> list[Message]:
        """A chat's latest messages to this program's address, oldest first."""
        self._check_address()
        found = self.db.history(self.resolve_chat(chat), limit)
        return [message for message in found if self._admits(message, strict=False)]

    def message(self, guid: str) -> Message | None:
        """One message by GUID (a "p:N/GUID" tapback target works too), or None."""
        return self.db.message(parse_target(guid)[0])

    # --- which of your addresses this program is -----------------------------------------------------------------

    def _check_address(self) -> None:
        # With no address set, refuse while messages arrive at several of your phone numbers; otherwise remember
        # the one in use, so a second one appearing later stops things too (see _admits).
        if self._address_checked or self.address is not None:
            return
        phones = [address for address in self.db.my_addresses() if is_phone(address)]
        if len(phones) > 1:
            raise AddressNotChosen(_choose_address(phones, "Messages here arrives at several of your phone numbers"))
        self._pinned_phone = address_key(phones[0]) if phones else None
        self._address_checked = True

    def _admits(self, message: Message, *, strict: bool) -> bool:
        """Whether this program should see a message, given which of your addresses it is."""
        if isinstance(self.address, AnyAddress):
            return True
        key = address_key(message.address)
        if self._address_key is not None:
            return key == self._address_key
        if key is None or "@" in key:
            return True
        if self._pinned_phone is None:
            self._pinned_phone = key
        if key == self._pinned_phone:
            return True
        if strict:
            raise AddressNotChosen(
                _choose_address([message.address or "?"], "A message just arrived at a second phone number of yours")
            )
        return False

    def _check_send(self, chat_guid: str) -> None:
        """Everything that must hold before Messages is asked to do anything in a chat."""
        self._guard.check_allowed(chat_guid)
        self._check_address()
        if isinstance(self.address, AnyAddress):
            return
        info = self.db.chat(chat_guid)
        on = info.address if info else None
        key = address_key(on)
        if self._address_key is not None:
            if key != self._address_key:
                raise WrongAddress(
                    f"{chat_guid} is on {on or 'no address of yours'}, not {self.address}, and Messages would send "
                    "from that; imbridge won't."
                )
        elif key is not None and "@" not in key:
            if self._pinned_phone is None:
                self._pinned_phone = key
            elif key != self._pinned_phone:
                raise AddressNotChosen(_choose_address([on], f"{chat_guid} is on a second phone number of yours"))

    # --- internals -----------------------------------------------------------------------------------------------

    async def _stream(self, since: int | None, include_from_me: bool, chat_guid: str | None) -> AsyncIterator[Message]:
        await asyncio.to_thread(self._check_address)
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
                if chat_guid is not None and message.chat_guid != chat_guid:
                    continue
                if not self._admits(message, strict=True):
                    continue
                if include_from_me or not message.is_from_me:
                    yield message
            if waiting or not batch:
                await asyncio.sleep(self.poll_interval)

    async def _react(self, chat_guid: str, guid: str, part: int, reaction: str, remove: bool) -> str:
        kind = reaction_type(reaction, remove)
        self._check_send(chat_guid)
        self._guard.record_send(chat_guid)
        result = await self._request(
            "send-reaction",
            {"chatGuid": chat_guid, "selectedMessageGuid": guid, "reactionType": kind, "partIndex": part},
        )
        return result.get("identifier")

    async def _target(self, message: Message | str, chat: str | None) -> tuple[str, str, int]:
        """The GUID, chat and part of a message to reply to or react to, after checking it was sent to us."""
        if isinstance(message, Message):
            guid, part, chat_guid, found = message.guid, 0, chat or message.chat_guid, message
        else:
            guid, part = parse_target(message)
            chat_guid, found = chat or self._chat_of.get(guid), None
            # Our own sends went out from our address; anything else is looked up to find its chat or check its address.
            if guid not in self._chat_of and (chat_guid is None or self._address_key is not None):
                found = await self._message_from_db(guid)
                chat_guid = chat_guid or found.chat_guid
        if found is not None and self._address_key is not None and address_key(found.address) != self._address_key:
            raise WrongAddress(f"message {guid} was sent to {found.address or 'no address'}, not {self.address}")
        return guid, self.resolve_chat(chat_guid), part

    async def _message_from_db(self, guid: str) -> Message:
        # A message sent a moment ago may not be in chat.db yet, or not linked to its chat yet.
        deadline = time.monotonic() + CHAT_LINK_GRACE
        while True:
            found = await asyncio.to_thread(self.db.message, guid)
            if found is not None and found.chat_guid:
                return found
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
