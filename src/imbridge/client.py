"""The high-level API: chats, sending (only where you've allowed it), tapbacks, and receiving."""

from __future__ import annotations

import asyncio
import base64
import errno
import hashlib
import itertools
import json
import logging
import os
import platform
import re
import shutil
import tempfile
import time
import urllib.parse
import uuid
from collections import deque
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import cache
from pathlib import Path
from typing import Any

from . import config, messages_app
from .addresses import (
    ANY_ADDRESS,
    AddressNotChosen,
    AnyAddress,
    WrongAddress,
    address_key,
    contact_address,
    display_address,
    is_phone,
    same_address,
)
from .chatdb import APPLE_EPOCH, ChatDB, ChatInfo, Message
from .contacts import Contacts
from .guard import AnyChat, SendGuard, one_to_one_handle
from .links import is_public, preview_urls, web_url
from .locations import make_pin
from .polls import PollResults
from .protocol import HelperBusy, HelperError, HelperNotConnected, HelperServer, HelperUnauthorized
from .reactions import parse_target, reaction_label, reaction_type
from .richtext import Span, Text, parts, plain, spans

log = logging.getLogger(__name__)

# Messages writes the row linking a message to its chat a moment after the message itself.
CHAT_LINK_GRACE = 2.0
# In a chat with yourself, what you send comes back as a received copy within seconds; copies are matched this long.
ECHO_WINDOW = 60.0
# iMessage's limits on changing what you sent: edits within 15 minutes (5 at most), unsends within 2.
EDIT_WINDOW, MAX_EDITS, UNSEND_WINDOW = 15 * 60, 5, 2 * 60
# So only recent messages can change; the changes stream watches this far back.
CHANGE_WINDOW = EDIT_WINDOW + 5 * 60
CANCEL_CHECK = 10.0  # seconds to wait for a Send Later message to be held, and then to be gone once cancelled
SCHEDULE_HELD = 2  # message.schedule_state once Apple's servers hold a Send Later message (1 on the way there)
STICKER_TYPES = (".png", ".heic", ".heics", ".gif", ".jpg", ".jpeg", ".webp")
MAX_POLL_BYTES = 4096  # a poll's options, as Messages encodes them, must fit in this
LINK_TIMEOUT = 15.0  # seconds Messages gets to load a link's preview before it goes as a plain link
TYPING_TIMEOUT = 60.0  # seconds a typing bubble lasts with no news: Messages lets one go about then
# The macOS each feature needs on this Mac; the people you message need the matching iOS or macOS to see it. Messages
# on an older macOS doesn't even keep a poll someone sends: only its "Sent a poll" text.
FEATURES = {
    "focus_status": 12,
    "edits": 13,
    "unsend": 13,
    "stickers": 14,
    "emoji_tapbacks": 15,
    "sticker_tapbacks": 15,
    "formatting": 15,  # bold, italics, underline, strikethrough and text effects
    "send_later": 15,
    "polls": 26,
}


@dataclass(frozen=True)
class TypingChange:
    """Someone started (typing=True) or stopped typing in a chat: Messages' typing bubble appearing or going."""

    chat_guid: str
    typing: bool
    at: datetime


@dataclass
class _Cursor:
    last: int  # the chat.db ROWID read up to
    first_seen: dict[int, float] = field(default_factory=dict)  # rows still waiting for their chat link

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


class EditLimit(RuntimeError):
    """One of iMessage's limits on changing a sent message.

    kind is "edit_window" (edits only within 15 minutes of sending), "edit_count" (5 edits at most) or
    "unsend_window" (unsends only within 2 minutes).
    """

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


class WrongChat(ValueError):
    """A Chat was asked to reply or react to a message from another chat."""


class SendLaterFailed(RuntimeError):
    """Messages didn't hold a message for later as asked (or didn't cancel it); the message says what it did."""


class Unsupported(RuntimeError):
    """This Mac's macOS is too old for the feature (see FEATURES and IMBridge.supports). Nothing was sent."""


@cache
def macos_version() -> tuple[int, int]:
    """This Mac's macOS version, as (major, minor)."""
    parts = [int(part) for part in platform.mac_ver()[0].split(".")[:2] if part.isdigit()]
    return (parts + [0, 0])[0], (parts + [0, 0])[1]


def _poll_size(options: list[str], creator: str) -> int:
    """The size of a poll as the helper encodes it (see polls.py), option identifiers included."""
    listed = [
        {"optionIdentifier": "0" * 36, "text": o, "attributedText": o, "creatorHandle": creator, "canBeEdited": False}
        for o in options
    ]
    item = {"title": "", "creatorHandle": creator, "orderedPollOptions": listed}
    return len(json.dumps({"version": 1, "item": item}, ensure_ascii=False, separators=(",", ":")).encode())


def _chosen(guid: str | None) -> str | None:
    """A caller's message GUID, as Messages writes GUIDs (an upper-case UUID); ValueError unless it's a UUID."""
    if guid is None:
        return None
    try:
        return str(uuid.UUID(guid)).upper()
    except (ValueError, TypeError, AttributeError):
        raise ValueError(f"{guid!r} isn't a UUID; make one with str(uuid.uuid4())") from None


def _chat_matches(info: ChatInfo, query: str) -> bool:
    """Whether a chat's name, or someone in it (number, email, or name in Contacts), contains query."""
    wanted = query.casefold()
    values = [info.name, info.identifier, *info.participants, *info.names.values()]
    if any(wanted in (value or "").casefold() for value in values):
        return True
    digits = re.sub(r"\D", "", query)  # a number as someone might type it: "(555) 123-45"
    return (len(digits) >= 4 and re.fullmatch(r"[\d\s().+-]+", query) is not None
            and any(digits in re.sub(r"\D", "", handle) for handle in info.participants if "@" not in handle))


def helper_build(dylib: Path) -> str | None:
    """The build a helper dylib says it is (helper/build.sh marks it "IMBRIDGE_BUILD=<build>"), or None."""
    try:
        found = re.search(rb"IMBRIDGE_BUILD=([0-9a-f]{16})\x00", dylib.read_bytes())
    except OSError:
        return None
    return found[1].decode() if found else None


def question_guid(poll_guid: str) -> str:
    """The GUID send_poll gives the question it sends after a poll whose GUID you chose: a UUID made from the poll's."""
    return str(uuid.uuid5(uuid.UUID(poll_guid), "question")).upper()


def _sticker_file(path: str | Path) -> Path:
    source = Path(path).expanduser()
    if not source.is_file():
        raise FileNotFoundError(f"no file at {source}")
    if source.suffix.lower() not in STICKER_TYPES:
        raise ValueError(f"a sticker is an image: {', '.join(STICKER_TYPES)}")
    return source


def _option_id(results: PollResults, option: str) -> str:
    """A poll option's id, from its id or its text (ignoring case)."""
    wanted = option.strip().casefold()
    for candidate in results.options:
        if option == candidate.id or wanted == candidate.text.strip().casefold():
            return candidate.id
    texts = ", ".join(repr(candidate.text) for candidate in results.options)
    raise ValueError(f"{option!r} isn't an option in this poll; it has {texts}")


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
    def names(self) -> dict[str, str]:
        """The names in your Contacts of the people in it, for those who are in them."""
        return self.info.names

    @property
    def participants(self) -> tuple[str, ...]:
        """Everyone in the chat but the address this program is; your other addresses stay (there, you're a member)."""
        return self._bridge._others(self.info.participants)

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
        return {**self.info.to_dict(), "participants": list(self.participants)}

    async def messages(
        self, *, since: int | None = None, include_from_me: bool = False, include_events: bool = False
    ) -> AsyncIterator[Message]:
        """This chat's new messages, tapbacks and inline replies as they arrive (from now, or after a ROWID).
        include_events adds changes to the group (people added, removed or leaving, renames), with .event set."""
        async for message in self._bridge._stream(since, include_from_me, self.guid, include_events):
            yield message

    def history(
        self,
        limit: int = 50,
        *,
        include_events: bool = False,
        before: Message | str | None = None,
        after: Message | str | None = None,
    ) -> list[Message]:
        """This chat's latest messages, oldest first; before and after page (see IMBridge.history)."""
        return self._bridge.history(self.guid, limit, include_events=include_events, before=before, after=after)

    def search(self, query: str, *, limit: int = 20, before: Message | str | None = None) -> list[Message]:
        """This chat's messages containing query, newest first: see IMBridge.search."""
        return self._bridge.search(query, chat=self.guid, limit=limit, before=before)

    async def send(
        self, text: Text, *, effect: str | None = None, subject: str | None = None, guid: str | None = None
    ) -> str:
        """Send text to this chat: a string, or strings and Spans for formatting and mentions. guid: see
        IMBridge.send."""
        return await self._bridge.send(self.guid, text, effect=effect, subject=subject, guid=guid)

    async def send_file(
        self, path: str | Path, *, reply_to: Message | str | None = None, guid: str | None = None
    ) -> str:
        """Send a file (a photo, GIF, video or document); reply_to makes it an inline reply to one of this chat's
        messages. Returns the new message's GUID. guid: see IMBridge.send."""
        target = _target_ref(*await self._own(reply_to)) if reply_to is not None else None
        return await self._bridge.send_file(self.guid, path, reply_to=target, guid=guid)

    async def reply(self, message: Message | str, text: Text, *, guid: str | None = None) -> str:
        """Reply inline to one of this chat's messages (a Message or its GUID). guid: see IMBridge.send."""
        target, part = await self._own(message)
        return await self._bridge.send(self.guid, text, reply_to=_target_ref(target, part), guid=guid)

    async def react(self, message: Message | str, reaction: str, *, remove: bool = False) -> str:
        """Tapback one of this chat's messages with a classic reaction or any emoji."""
        guid, part = await self._own(message)
        return await self._bridge._react(self.guid, guid, part, reaction, remove)

    async def typing(self, on: bool = True) -> None:
        await self._bridge.typing(self.guid, on)

    def is_typing(self) -> bool:
        """Whether someone is typing in this chat right now: see IMBridge.is_typing."""
        return self._bridge.is_typing(self.guid)

    async def wait_while_typing(self, timeout: float = 30.0) -> bool:
        """Wait until nobody is typing here: see IMBridge.wait_while_typing."""
        return await self._bridge.wait_while_typing(self.guid, timeout)

    def typing_changes(self) -> AsyncIterator[TypingChange]:
        """Typing bubbles appearing and going in this chat: see IMBridge.typing_changes."""
        return self._bridge.typing_changes(self.guid)

    async def mark_read(self) -> None:
        await self._bridge.mark_read(self.guid)

    async def edit(self, message: Message | str, text: str) -> None:
        """Edit one of your own messages in this chat (iMessage allows 5 edits within 15 minutes of sending)."""
        guid, part = await self._own(message)
        await self._bridge.edit(_target_ref(guid, part), text, chat=self.guid)

    async def unsend(self, message: Message | str) -> None:
        """Take back one of your own messages in this chat (iMessage allows it within 2 minutes of sending)."""
        guid, part = await self._own(message)
        await self._bridge.unsend(_target_ref(guid, part), chat=self.guid)

    async def changes(self, *, include_from_me: bool = False) -> AsyncIterator[Message]:
        """This chat's messages as they're edited or unsent, from now on (see IMBridge.changes)."""
        async for message in self._bridge._changes(self.guid, include_from_me):
            yield message

    async def send_sticker(
        self, path: str | Path, *, on: Message | str | None = None, label: str | None = None
    ) -> str:
        """Send an image as a sticker, alone or stuck onto one of this chat's messages: see IMBridge.send_sticker."""
        return await self._bridge.send_sticker(self.guid, path, on=on, label=label)

    async def send_location(self, latitude: float, longitude: float, *, name: str | None = None) -> str:
        """Send a location pin to this chat: see IMBridge.send_location."""
        return await self._bridge.send_location(self.guid, latitude, longitude, name=name)

    async def send_link(self, url: str, *, guid: str | None = None) -> str:
        """Send a link with its preview to this chat: see IMBridge.send_link."""
        return await self._bridge.send_link(self.guid, url, guid=guid)

    async def react_with_sticker(self, message: Message | str, path: str | Path) -> str:
        """Tapback one of this chat's messages with a sticker: see IMBridge.react_with_sticker."""
        guid, part = await self._own(message)
        return await self._bridge._sticker_tapback(self.guid, guid, part, path)

    async def send_later(self, text: str, at: datetime) -> str:
        """Schedule a message for later in this chat: see IMBridge.send_later."""
        return await self._bridge.send_later(self.guid, text, at)

    def scheduled(self) -> list[Message]:
        """Your messages waiting in Send Later in this chat, soonest first."""
        return self._bridge.scheduled(self.guid)

    async def cancel_scheduled(self, message: Message | str) -> None:
        """Take back one of this chat's messages waiting in Send Later."""
        if isinstance(message, Message) and message.chat_guid != self.guid:
            raise WrongChat(f"message {message.guid} is in {message.chat_guid}, not in this chat ({self.guid})")
        found = self._bridge.message(message.guid if isinstance(message, Message) else message)
        if found is not None and found.chat_guid != self.guid:
            raise WrongChat(f"message {found.guid} is in {found.chat_guid}, not in this chat ({self.guid})")
        await self._bridge.cancel_scheduled(message)

    async def send_poll(self, options: Iterable[str], *, question: str | None = None, guid: str | None = None) -> str:
        """Send a poll offering these options, and the question as a message after it; see IMBridge.send_poll."""
        return await self._bridge.send_poll(self.guid, options, question=question, guid=guid)

    async def vote(self, poll: Message | str, *options: str) -> str | None:
        """Vote for options in one of this chat's polls, keeping your other choices; see IMBridge.vote."""
        return await self._bridge.vote(poll, *options, chat=self.guid)

    async def unvote(self, poll: Message | str, *options: str) -> str | None:
        """Take back your vote for options (or all of them) in one of this chat's polls."""
        return await self._bridge.unvote(poll, *options, chat=self.guid)

    async def focus_status(self) -> bool | None:
        """Whether the other person in this one-to-one chat has notifications silenced: see IMBridge.focus_status."""
        if self.is_group:
            raise ValueError("Focus status is per person; ask IMBridge.focus_status about someone in the group")
        return await self._bridge.focus_status(self.guid)

    def poll(self, message: Message | str) -> PollResults | None:
        """The current state of a poll in this chat: see IMBridge.poll."""
        results = self._bridge.poll(message)
        if results is not None and results.chat_guid != self.guid:
            raise WrongChat(f"poll {results.guid} is in {results.chat_guid}, not in this chat ({self.guid})")
        return results

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
    inject: load the helper into Messages (restarting Messages) when none answers, or when the one that answers is
        from another imbridge build (as after an upgrade, until Messages restarts).
    contacts: name people from your Contacts (Message.sender_name, Chat.names). Read-only, and only for the people in
        the messages and chats imbridge reads.
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
        contacts: bool = True,
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
        self.contacts = Contacts() if contacts else None
        self.db = ChatDB(chat_db, names=self.contacts.name if self.contacts else None)
        self._guard = SendGuard(allow, resolve=self.resolve_chat, max_per_chat=max_per_chat, max_total=max_total)
        self._server = HelperServer(
            port or config.helper_port(), token or config.helper_token(), on_event=self._helper_event
        )
        self._typing: dict[str, float] = {}  # chat GUID -> when its typing bubble last showed (time.monotonic)
        self._typing_listeners: set[asyncio.Queue[TypingChange]] = set()
        # The chat of each message we sent, so replies and tapbacks work before chat.db catches up.
        self._chat_of: dict[str, str] = {}
        self._helper_lock = asyncio.Lock()  # so concurrent sends don't each relaunch Messages
        self._build_checked = False  # whether a connected helper's build has been compared with self.dylib's
        # What we sent recently, as (time, chat, fingerprint), to recognize the copies a chat with yourself echoes back.
        self._recent: deque[tuple[float, str, str]] = deque(maxlen=200)
        self._mine: set[str] = set()  # address keys of your own addresses

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

    def chats(self, limit: int = 50, *, query: str | None = None, offset: int = 0) -> list[Chat]:
        """Chats, most recently active first; with an address set, only the chats on it. query keeps those whose name,
        or someone in them (their number, email or name in your Contacts), contains it. offset skips that many."""
        wanted = (query or "").strip()
        if self._address_key is None and not wanted:
            return [Chat(self, info) for info in self.db.chats(limit + offset)[offset:]]
        found = self.db.chats(-1)  # every chat (a few thousand read in milliseconds), then filtered
        if self._address_key is not None:
            found = [info for info in found if address_key(info.address) == self._address_key]
        if wanted:
            found = [info for info in found if _chat_matches(info, wanted)]
        return [Chat(self, info) for info in found[offset : offset + limit]]

    def is_typing(self, chat: str) -> bool:
        """Whether someone is typing in a chat right now, as Messages' typing bubble shows. The helper is what sees
        it, so this knows only while connected (after start(), or any send); a bubble lasts until they send, stop,
        or a minute passes."""
        return self._typing_now(self.resolve_chat(chat))

    async def wait_while_typing(self, chat: str, timeout: float = 30.0) -> bool:
        """Wait until nobody is typing in a chat, up to timeout seconds. True once they've stopped (or if they
        weren't typing), False if they still are. Call it before answering, so a reply doesn't land in the middle of
        what someone is still writing."""
        chat_guid = self.resolve_chat(chat)
        await self._ensure_helper()
        deadline = time.monotonic() + timeout
        while self._typing_now(chat_guid):
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.1)
        return True

    async def typing_changes(self, chat: str | None = None) -> AsyncIterator[TypingChange]:
        """Typing bubbles appearing and going, as it happens: in one chat, or in every chat on this program's
        address. A bubble that shows no news for TYPING_TIMEOUT seconds ends with a stop."""
        wanted = self.resolve_chat(chat) if chat else None
        await self._ensure_helper()
        queue: asyncio.Queue[TypingChange] = asyncio.Queue()
        self._typing_listeners.add(queue)
        try:
            while True:
                try:
                    change = await asyncio.wait_for(queue.get(), timeout=5)
                except (TimeoutError, asyncio.TimeoutError):
                    self._expire_typing()
                    continue
                if (wanted is None or change.chat_guid == wanted) and self._on_my_address(change.chat_guid):
                    yield change
        finally:
            self._typing_listeners.discard(queue)

    def _helper_event(self, event: dict[str, Any]) -> None:
        """What the helper reports unasked; for now, typing bubbles appearing and going."""
        kind, chat_guid = event.get("event"), event.get("guid")
        if kind in ("started-typing", "stopped-typing") and isinstance(chat_guid, str):
            self._set_typing(chat_guid, kind == "started-typing")

    def _set_typing(self, chat_guid: str, typing: bool) -> None:
        was = self._typing_now(chat_guid)
        if typing:
            self._typing[chat_guid] = time.monotonic()
        else:
            self._typing.pop(chat_guid, None)
        if typing != was:  # Messages repeats itself (a redrawn conversation list re-says "not typing")
            change = TypingChange(chat_guid, typing, datetime.now(timezone.utc))
            for queue in self._typing_listeners:
                queue.put_nowait(change)

    def _typing_now(self, chat_guid: str) -> bool:
        started = self._typing.get(chat_guid)
        return started is not None and time.monotonic() - started < TYPING_TIMEOUT

    def _expire_typing(self) -> None:
        for chat_guid in [guid for guid in self._typing if not self._typing_now(guid)]:
            del self._typing[chat_guid]
            change = TypingChange(chat_guid, False, datetime.now(timezone.utc))
            for queue in self._typing_listeners:
                queue.put_nowait(change)

    def _on_my_address(self, chat_guid: str) -> bool:
        if self._address_key is None:
            return True
        info = self.db.chat(chat_guid)
        return info is not None and address_key(info.address) == self._address_key

    def contact_name(self, address: str) -> str | None:
        """The name on someone's card in your Contacts (a phone number or email), or None."""
        return self.contacts.name(address) if self.contacts else None

    def supports(self, feature: str) -> bool:
        """Whether this Mac's macOS has a feature: a key of FEATURES, like "polls" (macOS 26) or "send_later"
        (macOS 15). The features themselves raise Unsupported where it doesn't."""
        if feature not in FEATURES:
            raise ValueError(f"unknown feature {feature!r}; features: {', '.join(FEATURES)}")
        return macos_version() >= (FEATURES[feature], 0)

    def _require(self, feature: str, what: str) -> None:
        if not self.supports(feature):
            major, minor = macos_version()
            raise Unsupported(f"{what} need macOS {FEATURES[feature]} or later on this Mac, which has {major}.{minor}")

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
        text: Text,
        *,
        reply_to: str | None = None,
        effect: str | None = None,
        subject: str | None = None,
        guid: str | None = None,
    ) -> str:
        """Send text to an allowed chat: a chat GUID, a group's name, or a phone number or email.

        text is a string, or a sequence of strings and Spans for bold, italic, underline, strikethrough, animated
        text effects and @mentions (see richtext.py).

        A phone number (in international form, +15551234567) or email with no conversation yet starts one, over
        iMessage if they have it and SMS otherwise. Messages decides which of your addresses that goes out from (its
        "Start new conversations from" setting; for SMS, your iPhone's number), so with an address set this raises
        WrongAddress unless it would be this program's.

        Returns the new message's GUID. reply_to makes it an inline reply to that message GUID; effect is a key of
        EFFECTS (or a raw Messages effect identifier). guid is one you choose (a UUID) and record before sending:
        after a timeout or a crash, message(guid) says whether it went out, and sending again with the same guid
        doesn't deliver it twice (Messages drops the duplicate).
        """
        guid = _chosen(guid)
        try:
            chat_guid = self.resolve_chat(chat)
        except ChatNotFound:
            handle = contact_address(chat)
            if handle is None or reply_to:
                if handle is None and address_key(chat) and "@" not in chat and not chat.strip().startswith("+"):
                    raise ChatNotFound(
                        f"no conversation with {chat}; to start one, give the number with its country code, "
                        "like +15551234567"
                    ) from None
                raise
            if spans(text):
                raise ValueError("start a conversation with plain text; formatting can follow") from None
            return await self._start_chat(handle, plain(text), effect=effect, subject=subject, guid=guid)
        self._check_send(chat_guid)
        self._check_mentions(chat_guid, spans(text))
        self._guard.record_send(chat_guid)
        return await self._send_text(chat_guid, text, reply_to=reply_to, effect=effect, subject=subject, guid=guid)

    async def _send_text(
        self,
        chat_guid: str,
        text: Text,
        *,
        reply_to: str | None = None,
        effect: str | None = None,
        subject: str | None = None,
        guid: str | None = None,
    ) -> str:
        # Only once the chat has passed _check_send and the send is counted (record_send).
        target, part = parse_target(reply_to) if reply_to else (None, 0)
        common = {
            "chatGuid": chat_guid,
            "subject": subject,
            "effectId": EFFECTS.get(effect, effect) if effect else None,
            "selectedMessageGuid": target,
            "partIndex": part,
            "guid": guid,
        }
        if formatted := spans(text):
            result = await self._request("send-multipart", {**common, "parts": parts(formatted)})
        else:
            message = {"message": plain(text), "attributedBody": None, "ddScan": 0}
            result = await self._request("send-message", {**common, **message})
        sent = result.get("identifier")
        self._recent.append((time.monotonic(), chat_guid, f"text:{plain(text).strip()}"))
        self._remember(sent, chat_guid)
        return sent

    async def send_poll(
        self, chat: str, options: Iterable[str], *, question: str | None = None, guid: str | None = None
    ) -> str:
        """Send a poll offering these options (two or more) to an allowed chat; returns the poll message's GUID.

        Messages never shows a poll's title, so a question goes out as its own message right after the poll, as
        Messages sends it. Both count toward the rate limits, and both are checked before either is sent. People
        need iOS 26 or macOS 26 or later to see the poll and vote.

        guid is one you choose for the poll, as in send(). The question's GUID then follows from it (question_guid),
        so sending the pair again with the same guid delivers neither twice.
        """
        self._require("polls", "polls")
        guid = _chosen(guid)
        choices = [option.strip() for option in options]
        if len(choices) < 2 or not all(choices):
            raise ValueError("a poll needs at least two options, none of them empty")
        if len({choice.casefold() for choice in choices}) < len(choices):
            raise ValueError("a poll's options must all be different")
        chat_guid = self.resolve_chat(chat)
        self._check_send(chat_guid)
        creator = self._my_handle(chat_guid)
        if _poll_size(choices, creator) > MAX_POLL_BYTES:
            raise ValueError("those options are too long for one poll")
        question = (question or "").strip()
        for _ in range(2 if question else 1):
            self._guard.record_send(chat_guid)
        request = {"chatGuid": chat_guid, "options": choices, "creatorHandle": creator, "guid": guid}
        result = await self._request("send-poll", request)
        sent = result.get("identifier")
        self._recent.append((time.monotonic(), chat_guid, f"poll:{result.get('sessionIdentifier')}"))
        self._remember(sent, chat_guid)
        if question:
            await self._send_text(chat_guid, question, guid=question_guid(guid) if guid else None)
        return sent

    async def vote(self, poll: Message | str, *options: str, chat: str | None = None) -> str | None:
        """Vote for options in a poll (by text or id), keeping your other choices, as tapping them in Messages does.

        poll is the poll's message, an update of it, or a vote in it (a Message or its GUID). Returns the vote's GUID,
        or None if you had already chosen them all, in which case nothing is sent.
        """
        if not options:
            raise ValueError("name the options to vote for")
        return await self._vote(poll, options, chat, add=True)

    async def unvote(self, poll: Message | str, *options: str, chat: str | None = None) -> str | None:
        """Take back your vote for options in a poll, or for all of them if none are named. Returns the vote's GUID,
        or None if you hadn't chosen any of them, in which case nothing is sent."""
        return await self._vote(poll, options, chat, add=False)

    async def _vote(self, poll: Message | str, options: tuple[str, ...], chat: str | None, *, add: bool) -> str | None:
        self._require("polls", "polls")
        guid = poll.guid if isinstance(poll, Message) else parse_target(poll)[0]
        results = await asyncio.to_thread(self.poll, guid)
        if results is None or results.chat_guid is None:
            raise ValueError(f"{guid} isn't a poll, or a vote in one, that this program can see")
        chat_guid = results.chat_guid
        if chat is not None and self.resolve_chat(chat) != chat_guid:
            raise WrongChat(f"poll {results.guid} is in {chat_guid}, not in {chat}")
        self._check_send(chat_guid)
        named = [_option_id(results, option) for option in options]
        current = results.choices.get(None, ())
        if add:
            choice = (*current, *(option for option in dict.fromkeys(named) if option not in current))
        else:
            choice = tuple(option for option in current if named and option not in named)
        if choice == current:
            return None
        voter = self._my_handle(chat_guid)
        self._guard.record_send(chat_guid)
        result = await self._request(
            "send-poll-vote",
            {
                "chatGuid": chat_guid,
                "pollGuid": results.latest,
                "sessionIdentifier": results.session,
                "participantHandle": voter,
                "optionIdentifiers": list(choice),
            },
        )
        self._recent.append((time.monotonic(), chat_guid, f"vote:{results.session}:{','.join(sorted(choice))}"))
        return result.get("identifier")

    def _check_mentions(self, chat_guid: str, formatted: list[Span] | None) -> None:
        """Only people in a chat can be mentioned there."""
        mentioned = [span.mention for span in formatted or () if span.mention]
        if mentioned:
            info = self.db.chat(chat_guid)
            members = info.participants if info else ()
            for handle in mentioned:
                if not any(same_address(handle, member) for member in members):
                    raise ValueError(f"{handle} isn't in {chat_guid}, so they can't be mentioned there")

    def _my_handle(self, chat_guid: str) -> str:
        """Which of your addresses you are in a chat: how Messages names you in the polls and votes you send there."""
        info = self.db.chat(chat_guid)
        mine = (info.address if info else None) or (self.address if isinstance(self.address, str) else None)
        if not mine:
            raise ValueError(f"can't tell which of your addresses {chat_guid} is on; set address= to say")
        return display_address(mine)

    async def send_sticker(
        self, chat: str, path: str | Path, *, on: Message | str | None = None, label: str | None = None
    ) -> str:
        """Send an image as a sticker: on its own, or stuck onto one of the chat's messages (`on`, a Message or GUID).

        A PNG or HEIC with a transparent background looks like one. label is what VoiceOver reads out. It's copied
        into Messages' Attachments folder first, like send_file. Returns the sticker's GUID.
        """
        source = _sticker_file(path)
        chat_guid = self.resolve_chat(chat)
        target, part = None, 0
        if on is not None:
            target, on_chat, part = await self._target(on, None)
            if on_chat != chat_guid:
                raise WrongChat(f"message {target} is in {on_chat}, not in {chat_guid}")
        self._check_send(chat_guid)
        self._guard.record_send(chat_guid)
        request = {"chatGuid": chat_guid, "label": label or "", "selectedMessageGuid": target, "partIndex": part}
        sent = await self._send_staged("send-sticker", source, request, sticker=True)
        self._recent.append((time.monotonic(), chat_guid, f"reaction:{target}:sticker:False" if target else "text:"))
        self._remember(sent, chat_guid)
        return sent

    async def send_location(self, chat: str, latitude: float, longitude: float, *, name: str | None = None) -> str:
        """Send a location pin for these coordinates (and a place name, if you give one), as Messages sends a place
        from Maps. Only the coordinates you pass: imbridge never shares where the Mac is. Returns the pin's GUID."""
        card = make_pin(latitude, longitude, name)  # checks the coordinates
        chat_guid = self.resolve_chat(chat)
        self._check_send(chat_guid)
        self._guard.record_send(chat_guid)
        filename = re.sub(r"[^\w .,'&()-]", "", (name or "").strip())[:60].strip() or "Dropped Pin"
        with tempfile.TemporaryDirectory() as folder:
            pin = Path(folder) / f"{filename}.loc.vcf"
            pin.write_text(card)
            request = {"chatGuid": chat_guid, "isAudioMessage": 0, "attributedBody": None, "subject": None,
                       "effectId": None, "selectedMessageGuid": None, "partIndex": 0}
            sent = await self._send_staged("send-attachment", pin, request)
        self._remember(sent, chat_guid)
        return sent

    async def send_link(self, chat: str, url: str, *, guid: str | None = None) -> str:
        """Send a link with its preview, as Messages does when you paste one: the page's title, summary and picture in
        a card with the link. Returns the message's GUID; guid is one you choose, as in send().

        Messages loads the page on this Mac, as it would for you. So previews are only for the public internet: a link
        to this Mac or your local network raises ValueError, and so does a page that redirects there or takes its
        pictures from there. A page with no preview to give, or that takes longer than LINK_TIMEOUT to load, goes as a
        plain link.
        """
        guid = _chosen(guid)
        url = web_url(url)
        chat_guid = self.resolve_chat(chat)
        self._check_send(chat_guid)
        host = urllib.parse.urlsplit(url).hostname
        public = await asyncio.to_thread(is_public, host)
        if public is False:
            raise ValueError(f"{host} isn't on the public internet, so imbridge won't load a preview of it")
        self._guard.record_send(chat_guid)
        preview = await self._fetch_link(url) if public else None
        if preview is None:
            return await self._send_text(chat_guid, url, guid=guid)
        folder, request = preview
        try:
            result = await self._request("send-link", {**request, "chatGuid": chat_guid, "url": url, "guid": guid})
        except HelperError as error:
            if not isinstance(error, HelperNotConnected):  # refused, so nothing refers to the pictures
                shutil.rmtree(folder, ignore_errors=True)
            raise
        sent = result.get("identifier")
        self._recent.append((time.monotonic(), chat_guid, f"text:{url}"))
        self._remember(sent, chat_guid)
        return sent

    async def _fetch_link(self, url: str) -> tuple[Path, dict[str, Any]] | None:
        """Have Messages load url's preview into a folder in its Attachments; returns the folder and what send-link
        needs, or None if the page gave no preview. Raises ValueError if the preview names an address that isn't on
        the public internet (a redirect, or a picture)."""
        folder = config.OUTGOING / uuid.uuid4().hex
        folder.mkdir(parents=True)
        try:
            result = await self._request("fetch-link", {"url": url, "directory": str(folder), "timeout": LINK_TIMEOUT})
            if not result.get("found"):
                log.info("no preview for %s (%s); sending it as a plain link", url, result.get("reason"))
                shutil.rmtree(folder, ignore_errors=True)
                return None
            payload = base64.b64decode(result["payload"])
            hosts = {urllib.parse.urlsplit(found).hostname for found in preview_urls(payload)} - {None}
            checked = await asyncio.gather(*(asyncio.to_thread(is_public, host) for host in hosts))
            if private := sorted(host for host, public in zip(hosts, checked, strict=True) if public is False):
                raise ValueError(f"the preview of {url} comes partly from {', '.join(private)}, which isn't on the "
                                 "public internet, so imbridge won't send it")
            pictures = [str(Path(path)) for path in result.get("attachments") or []]
            if any(Path(path).parent != folder for path in pictures):
                raise HelperError(f"the preview of {url} came back with pictures outside {folder}")
        except BaseException:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        return folder, {"payload": result["payload"], "attachments": pictures}

    async def react_with_sticker(self, message: Message | str, path: str | Path, *, chat: str | None = None) -> str:
        """Tapback a message with a sticker (an image, like the stickers in Messages' tapback menu). Like any tapback,
        it replaces your previous one on that message. Returns the tapback's GUID."""
        guid, chat_guid, part = await self._target(message, chat)
        return await self._sticker_tapback(chat_guid, guid, part, path)

    async def _sticker_tapback(self, chat_guid: str, guid: str, part: int, path: str | Path) -> str:
        source = _sticker_file(path)
        self._check_send(chat_guid)
        self._guard.record_send(chat_guid)
        request = {"chatGuid": chat_guid, "selectedMessageGuid": guid, "partIndex": part}
        sent = await self._send_staged("send-sticker-tapback", source, request, sticker=True)
        self._recent.append((time.monotonic(), chat_guid, f"reaction:{guid}:sticker_tapback:False"))
        return sent

    async def _send_staged(self, action: str, source: Path, request: dict[str, Any], *, sticker: bool = False) -> str:
        """Copy a file into ~/Library/Messages/Attachments/imbridge (sandboxed Messages reads nothing else; the copy
        becomes the attachment), then ask the helper to send it. Call only once the send has passed the checks."""
        folder = config.OUTGOING / uuid.uuid4().hex
        folder.mkdir(parents=True)
        staged = folder / source.name
        shutil.copyfile(source, staged)
        request = {**request, "filePath": str(staged)}
        if sticker:
            request["stickerId"] = str(uuid.uuid4()).upper()
            request["stickerHash"] = hashlib.sha256(staged.read_bytes()).hexdigest()[:16]
        try:
            result = await self._request(action, request)
        except HelperError:
            shutil.rmtree(folder, ignore_errors=True)  # refused, so nothing refers to the copy (unlike a timeout)
            raise
        return result.get("identifier")

    async def send_later(self, chat: str, text: str, at: datetime) -> str:
        """Schedule a message with Messages' Send Later; returns its GUID.

        Messages sends it at the start of the minute `at` falls in (a minute to 14 days ahead; a naive datetime is
        local time), whether or not this program is still running. The allowlist and rate limits apply now, when
        it's scheduled. imbridge checks Messages held it in the right chat, and raises SendLaterFailed if not.
        """
        self._require("send_later", "Send Later messages")
        text = text.strip()
        if not text:
            raise ValueError("there's no text to send")
        chat_guid = self.resolve_chat(chat)
        self._check_send(chat_guid)
        when = at.astimezone(timezone.utc).replace(second=0, microsecond=0)
        now = datetime.now(timezone.utc)
        if when < now + timedelta(minutes=1) or when > now + timedelta(days=14):
            raise ValueError("Send Later takes a time from a minute to 14 days ahead")
        if await self._is_own_chat(chat_guid):  # asks Messages, so after every check that doesn't
            raise ValueError("imbridge doesn't schedule messages to yourself: your own devices get them right away")
        self._guard.record_send(chat_guid)
        request = {"chatGuid": chat_guid, "message": text, "deliverAt": when.timestamp()}
        result = await self._request("send-later", request)
        sent = result.get("identifier")
        held = await self._stored(sent)  # Messages doesn't always do as asked, and says nothing: check
        if held is None or held.chat_guid != chat_guid:
            where = held.chat_guid if held else "no conversation imbridge can find"
            raise SendLaterFailed(
                f"Messages filed the message in {where} instead of {chat_guid}, so it won't reach them"
            )
        if held.scheduled_for is None:
            raise SendLaterFailed("Messages sent the message right away instead of holding it for later")
        self._remember(sent, chat_guid)
        return sent

    async def cancel_scheduled(self, message: Message | str) -> None:
        """Take back a message waiting in Send Later, before it goes out."""
        guid = message.guid if isinstance(message, Message) else parse_target(message)[0]
        found = await asyncio.to_thread(self.db.message, guid)
        if found is None or not found.is_from_me or found.scheduled_for is None or found.chat_guid is None:
            raise ValueError(f"{guid} isn't a message of yours waiting to be sent later (it may have gone out)")
        self._check_send(found.chat_guid)  # retracting reaches Messages like anything else: allowed chats only
        # A cancel sent before Apple's servers hold the message (schedule_state 2) is lost, and the message still
        # goes out at its time, although its row disappears here. So wait for that first.
        deadline = time.monotonic() + CANCEL_CHECK
        while await asyncio.to_thread(self.db.schedule_state, guid) != SCHEDULE_HELD:
            if time.monotonic() >= deadline:
                raise SendLaterFailed(
                    f"Apple's servers don't hold {guid} yet, and cancelling before they do doesn't stick; try again"
                )
            await asyncio.sleep(0.2)
        await self._request("cancel-scheduled", {"chatGuid": found.chat_guid, "messageGuid": guid})
        deadline = time.monotonic() + CANCEL_CHECK
        while (current := await asyncio.to_thread(self.db.message, guid)) and current.scheduled_for:
            if time.monotonic() >= deadline:
                raise SendLaterFailed(f"Messages still has {guid} scheduled")
            await asyncio.sleep(0.25)

    def scheduled(self, chat: str | None = None) -> list[Message]:
        """Your messages waiting in Send Later (every chat, or one), soonest first."""
        self._check_address()
        found = self.db.scheduled(self.resolve_chat(chat) if chat else None)
        return [message for message in found if self._admits(message, strict=False)]

    async def _is_own_chat(self, chat_guid: str) -> bool:
        """A one-to-one chat with an address the signed-in account uses on this Mac: a chat with yourself."""
        handle = one_to_one_handle(chat_guid)
        if handle is None:
            return False
        aliases = (await self._request("get-account-info", {})).get("aliases") or []
        return address_key(handle) in {address_key(alias.get("Alias")) for alias in aliases if isinstance(alias, dict)}

    async def _stored(self, guid: str | None, wait: float = 6.0) -> Message | None:
        """A message just sent, as chat.db records it, once it's linked to its chat."""
        deadline = time.monotonic() + wait
        while guid:
            found = await asyncio.to_thread(self.db.message, guid)
            if (found is not None and found.chat_guid) or time.monotonic() >= deadline:
                return found
            await asyncio.sleep(0.25)
        return None

    async def send_file(
        self, chat: str, path: str | Path, *, reply_to: str | None = None, guid: str | None = None
    ) -> str:
        """Send a file (a photo, GIF, video or document) to an allowed chat; returns the new message's GUID.

        reply_to makes it an inline reply, and guid is one you choose, as for send(). The file is first copied into
        ~/Library/Messages/Attachments/imbridge: Messages is sandboxed and can't read it anywhere else, and that copy
        becomes the attachment Messages keeps.
        """
        guid = _chosen(guid)
        source = Path(path).expanduser()
        if not source.is_file():
            raise FileNotFoundError(f"no file at {source}")
        chat_guid = self.resolve_chat(chat)
        self._check_send(chat_guid)
        self._guard.record_send(chat_guid)
        target, part = parse_target(reply_to) if reply_to else (None, 0)
        request = {
            "chatGuid": chat_guid,
            "isAudioMessage": 0,
            "attributedBody": None,
            "subject": None,
            "effectId": None,
            "selectedMessageGuid": target,
            "partIndex": part,
            "guid": guid,
        }
        sent = await self._send_staged("send-attachment", source, request)
        self._remember(sent, chat_guid)
        return sent

    async def _start_chat(
        self, handle: str, text: str, *, effect: str | None, subject: str | None, guid: str | None = None
    ) -> str:
        self._guard.check_allowed_handle(handle)
        self._check_address()
        chat_guid = f"any;-;{handle}"  # what the conversation will be called
        self._guard.record_send(chat_guid)  # before asking Messages anything; a refused attempt counts too
        service = await self._service_for(handle)
        await self._check_new_chat_address(service)
        result = await self._request(
            "create-chat",
            {
                "addresses": [handle],
                "service": service,
                "message": text,
                "attributedBody": None,
                "effectId": EFFECTS.get(effect, effect) if effect else None,
                "subject": subject,
                "guid": guid,
            },
        )
        self._recent.append((time.monotonic(), chat_guid, f"text:{text.strip()}"))
        return result.get("identifier")

    async def _check_new_chat_address(self, service: str) -> None:
        """Messages can't be told which address a new conversation goes out from: iMessage uses the "Start new
        conversations from" setting, SMS the number of the iPhone forwarding texts to this Mac. Refuse unless that is
        this program's address (or, with none given, not a second phone number of yours)."""
        if isinstance(self.address, AnyAddress):
            return
        if service == "iMessage":
            info = await self._request("get-account-info", {})
            used = display_address(info.get("active_alias"))
            where = f"new conversations start from {used or 'an address Messages picks'}"
            advice = (
                'Choose {} in Messages > Settings > iMessage > "Start new conversations from", or start the '
                "conversation yourself."
            )
            second = "New conversations start from a second phone number of yours"
        else:
            used = await asyncio.to_thread(self.db.texting_address)
            where = f"they aren't on iMessage, and texts go out through {used or 'whichever iPhone forwards them here'}"
            advice = "Start the conversation yourself, from the phone with {}."
            second = "Texts go out through a second phone number of yours"
        key = address_key(used)
        if self._address_key is not None:
            if key != self._address_key:
                mine = display_address(self.address)
                raise WrongAddress(f"{where}, not {mine} (the address this program uses). {advice.format(mine)}")
        elif key is not None and "@" not in key and self._pinned_phone not in (None, key):
            raise AddressNotChosen(_choose_address([used], second))

    async def _service_for(self, handle: str) -> str:
        """iMessage if they have it; otherwise SMS, which goes through your iPhone (Text Message Forwarding)."""
        kind = "email" if "@" in handle else "phone"
        answer = await self._request("check-imessage-availability", {"aliasType": kind, "address": handle})
        if answer.get("available"):
            return "iMessage"
        if kind == "email":
            raise ChatNotFound(f"{handle} isn't on iMessage, and email addresses can't get SMS")
        return "SMS"

    def _remember(self, sent: str | None, chat_guid: str) -> None:
        if sent:
            if len(self._chat_of) >= 1000:
                self._chat_of.clear()
            self._chat_of[sent] = chat_guid

    async def reply(
        self, message: Message | str, text: Text, *, chat: str | None = None, guid: str | None = None
    ) -> str:
        """Reply inline (threaded) to a message in an allowed chat: a Message, or a message GUID. guid is one you
        choose for the reply, as for send()."""
        target, chat_guid, part = await self._target(message, chat)
        return await self.send(chat_guid, text, reply_to=_target_ref(target, part), guid=guid)

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

    async def edit(self, message: Message | str, text: str, *, chat: str | None = None) -> None:
        """Edit one of your own messages in an allowed chat. iMessage allows 5 edits within 15 minutes of sending;
        past that this raises EditLimit. Readers see the new text, marked Edited.
        """
        target, chat_guid, part = await self._own_message(message, chat)
        if target.unsent_at:
            raise ValueError(f"message {target.guid} was unsent; there's nothing to edit")
        self._check_send(chat_guid)
        age = self._age(target)
        if age > EDIT_WINDOW:
            raise EditLimit(
                f"iMessage only allows editing within 15 minutes of sending; this one is {age / 60:.0f} minutes old",
                "edit_window",
            )
        if target.edit_count >= MAX_EDITS:
            raise EditLimit(f"iMessage allows {MAX_EDITS} edits per message; this one has had them all", "edit_count")
        self._guard.record_send(chat_guid)
        await self._request(
            "edit-message",
            {
                "chatGuid": chat_guid,
                "messageGuid": target.guid,
                "partIndex": part,
                "editedMessage": text,
                # what devices without edit support show instead
                "backwardsCompatibilityMessage": f"Edited to \u201c{text}\u201d",
            },
        )

    async def unsend(self, message: Message | str, *, chat: str | None = None) -> None:
        """Take back one of your own messages in an allowed chat. iMessage allows it within 2 minutes of sending;
        past that this raises EditLimit. Unsending a message that's already unsent does nothing.
        """
        target, chat_guid, part = await self._own_message(message, chat)
        if target.unsent_at:
            return
        self._check_send(chat_guid)
        age = self._age(target)
        if age > UNSEND_WINDOW:
            raise EditLimit(
                f"iMessage only allows unsending within 2 minutes of sending; this message is {age:.0f} seconds old",
                "unsend_window",
            )
        self._guard.record_send(chat_guid)
        await self._request("unsend-message", {"chatGuid": chat_guid, "messageGuid": target.guid, "partIndex": part})

    async def focus_status(self, person: str) -> bool | None:
        """Whether someone has notifications silenced by a Focus, as Messages says under their name: True or False,
        or None when they don't share their Focus status with you. person is a phone number or email, or a
        one-to-one chat. Nothing is sent to them."""
        handle = one_to_one_handle(person) if ";" in person else (contact_address(person) or person.strip())
        if not handle:
            raise ValueError(f"{person!r} isn't a person's phone number or email, or a one-to-one chat")
        status = (await self._request("check-focus-status", {"address": handle})).get("status")
        return {1: False, 2: True}.get(status)  # IMCore's availability: 0 unknown, 1 available, 2 silenced

    async def account(self) -> dict[str, Any]:
        """The signed-in account (apple_id, login_status_message, aliases, ...); also proves the helper answers."""
        result = await self._request("get-account-info", {})
        return {key: value for key, value in result.items() if key != "transactionId"}

    # --- receiving -----------------------------------------------------------------------------------------------

    async def all_messages(
        self, *, since: int | None = None, include_from_me: bool = False, include_events: bool = False
    ) -> AsyncIterator[Message]:
        """New messages from every chat as they arrive, tapbacks and inline replies included.

        For a bot that answers in one conversation, use chat.messages() instead. Starts from now, or after the given
        chat.db ROWID; your own messages are skipped unless include_from_me. include_events adds changes to groups
        (people added, removed or leaving, renames), with .event set.
        """
        async for message in self._stream(since, include_from_me, None, include_events):
            yield message

    async def new_messages(
        self,
        *,
        since: int,
        chat: str | None = None,
        wait: float = 0,
        include_from_me: bool = False,
        include_events: bool = False,
    ) -> tuple[list[Message], int]:
        """Messages after chat.db ROWID `since` (one chat, or every chat), waiting up to `wait` seconds for the first.

        Returns the messages and the ROWID to pass as `since` next time: a polling alternative to the streams, for
        request/response code such as tool calls. Start from `im.db.max_rowid()`.
        """
        await asyncio.to_thread(self._check_address)
        chat_guid = self.resolve_chat(chat) if chat else None
        cursor = _Cursor(since)
        deadline = time.monotonic() + max(wait, 0)
        while True:
            found, idle = await self._poll(cursor, include_from_me, chat_guid, include_events)
            if found or time.monotonic() >= deadline:
                return found, cursor.last
            if idle:
                await asyncio.sleep(min(self.poll_interval, max(deadline - time.monotonic(), 0)))

    async def changes(self, *, chat: str | None = None, include_from_me: bool = False) -> AsyncIterator[Message]:
        """Messages as they're edited or unsent, from now on; each arrives with edited_at or unsent_at set.

        Edits and unsends change existing messages rather than adding new ones, so they don't show up in the
        message streams. iMessage only allows them on recent messages, so only those are watched. Your own messages
        are skipped unless include_from_me.
        """
        async for message in self._changes(self.resolve_chat(chat) if chat else None, include_from_me):
            yield message

    def history(
        self,
        chat: str,
        limit: int = 50,
        *,
        include_events: bool = False,
        before: Message | str | None = None,
        after: Message | str | None = None,
    ) -> list[Message]:
        """A chat's latest messages to this program's address, oldest first. To page back, pass before: the oldest
        message you have (or its GUID); an empty list means you've reached the start. after reads on from a message
        instead: the ones just after it."""
        self._check_address()
        older, newer = (value.guid if isinstance(value, Message) else value for value in (before, after))
        found = self.db.history(self.resolve_chat(chat), limit, events=include_events, before=older, after=newer)
        return [message for message in found if self._admits(message, strict=False)]

    def search(
        self, query: str, *, chat: str | None = None, limit: int = 20, before: Message | str | None = None
    ) -> list[Message]:
        """Messages to this program's address whose text contains query (ignoring case), newest first: in one chat,
        or in all of them. To page on, pass before: the last (oldest) message you got, or its GUID."""
        self._check_address()
        older = before.guid if isinstance(before, Message) else before
        found = self.db.search(query, chat_guid=self.resolve_chat(chat) if chat else None, before=older)
        return list(itertools.islice((m for m in found if self._admits(m, strict=False)), max(1, limit)))

    def mentions_me(self, message: Message) -> bool:
        """Whether a message @mentions this program's address (with no address set, any of your addresses): in a
        group, answer only when someone asks for you."""
        self._check_address()
        mentioned = {address_key(address) for address in message.mentions}
        return self._address_key in mentioned if self._address_key is not None else bool(mentioned & self._mine)

    def message(self, guid: str) -> Message | None:
        """One message by GUID (a "p:N/GUID" tapback target works too), or None."""
        return self.db.message(parse_target(guid)[0])

    def poll(self, message: Message | str) -> PollResults | None:
        """A poll's current state: its options, everyone's current choice, and the question sent with it.

        Takes the poll's message, an update of it, or a vote in it (a Message or its GUID); None for anything else.
        """
        guid = message.guid if isinstance(message, Message) else parse_target(message)[0]
        self._check_address()
        found = self.db.message(guid)
        if found is None or not self._admits(found, strict=False):
            return None
        return self.db.poll(guid, mine=self._mine)  # a vote from any of your addresses is yours (self-chat echoes)

    # --- which of your addresses this program is -----------------------------------------------------------------

    def _check_address(self) -> None:
        # Learn your own addresses. With no address set, refuse while messages arrive at several of your phone
        # numbers; otherwise remember the one in use, so a second one appearing later stops things too (see _admits).
        if self._address_checked:
            return
        mine = self.db.my_addresses()
        self._mine = {key for key in map(address_key, mine) if key}
        if self.address is None:
            phones = [address for address in mine if is_phone(address)]
            if len(phones) > 1:
                what = "Messages here arrives at several of your phone numbers"
                raise AddressNotChosen(_choose_address(phones, what))
            self._pinned_phone = address_key(phones[0]) if phones else None
        self._address_checked = True

    def _others(self, participants: Iterable[str]) -> tuple[str, ...]:
        if self._address_key is None:
            return tuple(participants)
        return tuple(handle for handle in participants if address_key(handle) != self._address_key)

    def _is_echo(self, message: Message) -> bool:
        """A received copy of something this program just sent: in a chat with yourself, everything comes back."""
        if address_key(message.sender) not in self._mine:
            return False  # only your own addresses echo, so a real person's identical "ok" is never dropped
        if message.reaction:
            reaction = message.reaction
            fingerprint = f"reaction:{reaction.target_guid}:{reaction.label}:{reaction.removed}"
        elif message.poll:
            fingerprint = f"poll:{message.poll.session}"
        elif message.vote:
            fingerprint = f"vote:{message.vote.session}:{','.join(sorted(message.vote.options))}"
        else:
            fingerprint = f"text:{(message.text or '').strip()}"
        now = time.monotonic()
        return any(
            chat == message.chat_guid and seen == fingerprint and now - at < ECHO_WINDOW
            for at, chat, seen in self._recent
        )

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

    async def _changes(self, chat_guid: str | None, include_from_me: bool) -> AsyncIterator[Message]:
        await asyncio.to_thread(self._check_address)
        floor = await asyncio.to_thread(self.db.recent_floor, CHANGE_WINDOW)
        started = (time.time() - APPLE_EPOCH) * 1e9  # changes stamped later than this are news
        seen: dict[int, tuple[int, int, int]] = {}  # ROWID -> (date, date_edited, date_retracted) as last read
        while True:
            marks = await asyncio.to_thread(self.db.change_marks, floor)
            changed = []
            for rowid, date, edited, retracted in marks:
                before = seen.get(rowid)
                seen[rowid] = (date, edited, retracted)
                if before is None:
                    # First sight. A message can arrive and be edited or unsent between two reads (nothing reads
                    # while the consumer is busy), so report it if the change happened after this stream began.
                    if max(edited, retracted) > started:
                        changed.append(rowid)
                elif before[1:] != (edited, retracted):
                    changed.append(rowid)
            cutoff = (time.time() - APPLE_EPOCH - CHANGE_WINDOW) * 1e9
            for rowid in [rowid for rowid, (date, _, _) in seen.items() if date and date < cutoff]:
                del seen[rowid]  # too old to be edited or unsent any more
            floor = min(seen) - 1 if seen else max((mark[0] for mark in marks), default=floor)
            for rowid in changed:
                message = await asyncio.to_thread(self.db.message_at, rowid)
                if message is None or (chat_guid is not None and message.chat_guid != chat_guid):
                    continue
                if self._admits(message, strict=False) and (include_from_me or not message.is_from_me):
                    yield message
            await asyncio.sleep(self.poll_interval)

    async def _own_message(self, message: Message | str, chat: str | None) -> tuple[Message, str, int]:
        """The current row of one of your own messages, with its chat and part, for editing or unsending."""
        guid, chat_guid, part = await self._target(message, chat)
        target = await self._message_from_db(guid)  # fresh: its date, edit count, and whether it was unsent
        if not target.is_from_me:
            raise ValueError(f"message {guid} isn't yours; only your own messages can be edited or unsent")
        return target, chat_guid, part

    @staticmethod
    def _age(message: Message) -> float:
        return time.time() - message.date.timestamp() if message.date else 0.0

    async def _stream(
        self, since: int | None, include_from_me: bool, chat_guid: str | None, include_events: bool = False
    ) -> AsyncIterator[Message]:
        await asyncio.to_thread(self._check_address)
        cursor = _Cursor(since if since is not None else await asyncio.to_thread(self.db.max_rowid))
        while True:
            found, idle = await self._poll(cursor, include_from_me, chat_guid, include_events)
            for message in found:
                yield message
            if idle:
                await asyncio.sleep(self.poll_interval)

    async def _poll(
        self, cursor: _Cursor, include_from_me: bool, chat_guid: str | None, include_events: bool = False
    ) -> tuple[list[Message], bool]:
        """One read past the cursor: the messages this program should see, and whether to wait before reading again."""
        batch = await asyncio.to_thread(self.db.messages_after, cursor.last, events=include_events)
        found: list[Message] = []
        for message in batch:
            if message.rowid <= cursor.last:  # a message linked to two chats appears twice
                continue
            if message.chat_guid is None:
                seen = cursor.first_seen.setdefault(message.rowid, time.monotonic())
                if time.monotonic() - seen < CHAT_LINK_GRACE:
                    return found, True  # read it again once its chat link lands
            cursor.first_seen.pop(message.rowid, None)
            cursor.last = message.rowid
            if chat_guid is not None and message.chat_guid != chat_guid:
                continue
            if not self._admits(message, strict=True):
                continue
            if not message.is_from_me and self._is_echo(message):
                continue
            if include_from_me or not message.is_from_me:
                found.append(message)
        return found, not batch

    async def _react(self, chat_guid: str, guid: str, part: int, reaction: str, remove: bool) -> str:
        kind = reaction_type(reaction, remove)
        self._check_send(chat_guid)
        self._guard.record_send(chat_guid)
        result = await self._request(
            "send-reaction",
            {"chatGuid": chat_guid, "selectedMessageGuid": guid, "reactionType": kind, "partIndex": part},
        )
        self._recent.append((time.monotonic(), chat_guid, f"reaction:{guid}:{reaction_label(reaction)}:{remove}"))
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
        async with self._helper_lock:
            if not self._server.listening:
                try:
                    await self._server.start()
                except OSError as error:
                    if error.errno != errno.EADDRINUSE:
                        raise
                    raise HelperBusy(
                        f"another program on this Mac is using the helper (port {self._server.port}); only one "
                        "imbridge program can send at a time. Nothing was sent."
                    ) from None
            if not self._server.connected:
                try:
                    await self._server.wait_connected(timeout=3)  # an injected helper redials every second
                except (TimeoutError, asyncio.TimeoutError):  # the same class from Python 3.11 on
                    if not self.inject:
                        raise HelperNotConnected(
                            "no helper answered; run `imbridge start` (or use inject=True) to load it into Messages"
                        ) from None
                    await self._relaunch(timeout)
                    return
            if not self._build_checked:
                await self._check_build(timeout)

    async def _check_build(self, timeout: float) -> None:
        """Reload the helper if Messages still has another build loaded: an older helper ignores what it doesn't
        know, so a newer request would wait out its timeout instead of failing. Checked once."""
        self._build_checked = True
        wanted = await asyncio.to_thread(helper_build, self.dylib)
        if wanted is None or self._server.build == wanted:
            return
        loaded = self._server.build or "an unnamed build"
        if not self.inject:
            log.warning("Messages has another imbridge helper loaded (%s, not %s): run `imbridge start` to load "
                        "this one", loaded, wanted)
            return
        log.info("Messages has another imbridge helper loaded (%s); reloading it with %s", loaded, wanted)
        await self._relaunch(timeout)

    async def _relaunch(self, timeout: float = 45.0) -> None:
        log.info("launching Messages with the helper")
        env = {"IMBRIDGE_PORT": str(self._server.port), "IMBRIDGE_TOKEN": self._server.token or ""}
        await asyncio.to_thread(messages_app.launch_with_helper, self.dylib, env)
        await self._server.wait_connected(timeout=timeout)
