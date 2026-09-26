"""Keeps imbridge from messaging anyone you didn't mean it to.

Two rules apply to everything imbridge does that the other side can see (messages, inline replies, tapbacks, typing
indicators, read receipts):

- Allowlist. imbridge only acts in chats you've allowed: with `imbridge allow`, which a person has to confirm in a
  terminal, or with IMBridge(allow=[...]) in your code. With neither, imbridge is read-only.
- Rate limits. Messages and tapbacks are capped at 10 a minute per chat and 30 a minute in total, counted across every
  imbridge process on the Mac, so a runaway loop stops instead of flooding a conversation.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from . import config
from .addresses import contact_address, same_address

ANY_LINE = "*"  # in the allowed-chats file: every chat


def one_to_one_handle(chat_guid: str) -> str | None:
    """The other person's phone number or email in a one-to-one chat GUID ("any;-;+15551234567"), else None."""
    service, kind, handle = (chat_guid.split(";", 2) + ["", ""])[:3]
    return handle if kind == "-" and handle else None


class NewContact(str):
    """In IMBridge(allow=[...]), someone you have no conversation with yet, whom imbridge may start one with:
    NewContact("+15557654321"). Every other entry has to match an existing chat, so a typo in one fails at start
    instead of reaching a stranger."""

    def __new__(cls, address: str) -> NewContact:
        person = contact_address(address)
        if person is None:
            raise ValueError(f"{address!r} isn't a phone number with its country code (+15551234567) or an email")
        return super().__new__(cls, person)


class AnyChat:
    def __repr__(self) -> str:
        return "ANY_CHAT"


ANY_CHAT = AnyChat()
"""IMBridge(allow=ANY_CHAT) lets imbridge send to every chat. Rate limits still apply."""


class SendNotAllowed(PermissionError):
    """The chat isn't on imbridge's allowlist."""


class RateLimited(RuntimeError):
    """Too many messages in the last minute, to one chat or in total."""


def allowed_file() -> Path:
    return config.APP_SUPPORT / "allowed-chats"


def read_allowed() -> set[str]:
    """Chat GUIDs allowed with `imbridge allow` ("*" means every chat)."""
    try:
        lines = allowed_file().read_text().splitlines()
    except FileNotFoundError:
        return set()
    return {line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")}


def write_allowed(chats: Iterable[str]) -> None:
    path = allowed_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "# Chats imbridge may send to. Change them with `imbridge allow` and `imbridge disallow`.\n"
    path.write_text(header + "".join(f"{chat}\n" for chat in sorted(set(chats))))
    path.chmod(0o600)


class SendGuard:
    def __init__(
        self,
        allow: Iterable[str] | AnyChat = (),
        *,
        resolve: Callable[[str], str] = lambda chat: chat,
        max_per_chat: int = 10,
        max_total: int = 30,
        window: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._allow = [allow] if isinstance(allow, str) else allow  # one chat given on its own
        self._resolve = resolve  # turns a phone number, email or group name from `allow` into its chat GUID
        self._resolved: set[str] | None = None
        self._handles: set[str] = set()  # people allowed in code that you haven't messaged yet
        self.max_per_chat = max_per_chat
        self.max_total = max_total
        self.window = window
        self.clock = clock

    def resolve_allowed(self) -> set[str]:
        """The chat GUIDs allowed in code. An entry that matches no chat raises, so a typo fails at startup, unless
        it's a NewContact: someone to start a conversation with."""
        if self._resolved is None:
            resolved: set[str] = set()
            for entry in () if isinstance(self._allow, AnyChat) else self._allow:
                try:
                    resolved.add(self._resolve(entry))
                except LookupError:
                    if not isinstance(entry, NewContact):
                        raise
                    self._handles.add(entry)
            self._resolved = resolved
        return self._resolved

    def allows(self, chat_guid: str) -> bool:
        if isinstance(self._allow, AnyChat):
            return True
        listed = read_allowed()  # read every time, so `imbridge disallow` takes effect in running programs
        if ANY_LINE in listed or chat_guid in listed or chat_guid in self.resolve_allowed():
            return True
        handle = one_to_one_handle(chat_guid)  # a one-to-one chat started with someone allowed by number or email
        return handle is not None and self._allows_person(handle, listed)

    def allows_handle(self, handle: str) -> bool:
        """Whether imbridge may start a conversation with this phone number or email."""
        if isinstance(self._allow, AnyChat):
            return True
        listed = read_allowed()
        return ANY_LINE in listed or self._allows_person(handle, listed)

    def _allows_person(self, handle: str, listed: set[str]) -> bool:
        chats = listed | self.resolve_allowed()  # which also collects self._handles
        people = list(self._handles)
        for entry in chats:
            person = one_to_one_handle(entry) if ";" in entry else entry  # never a group: its GUID isn't a person
            if person:
                people.append(person)
        return any(same_address(handle, person) for person in people)

    def check_allowed_handle(self, handle: str) -> None:
        if not self.allows_handle(handle):
            raise SendNotAllowed(
                f"imbridge isn't allowed to message {handle}. Allow them in your code with IMBridge(allow=[...]), "
                f"or by running `imbridge allow {handle}` yourself (it asks you to confirm)."
            )

    def check_allowed(self, chat_guid: str) -> None:
        if not self.allows(chat_guid):
            raise SendNotAllowed(
                f"imbridge isn't allowed to send to {chat_guid}. Allow it in your code with IMBridge(allow=[...]), "
                f"or by running `imbridge allow {chat_guid}` yourself (it asks you to confirm)."
            )

    def record_send(self, chat_guid: str) -> None:
        """Count a message or tapback to chat_guid, or raise RateLimited if it would go over a limit."""
        path = config.APP_SUPPORT / "recent-sends.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o600), "r+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)  # every imbridge process on the Mac shares these counts
            try:
                sends = json.loads(f.read() or "[]")
            except json.JSONDecodeError:
                sends = []
            now = self.clock()
            sends = [(at, chat) for at, chat in sends if now - at < self.window]
            to_chat = sum(1 for _, chat in sends if chat == chat_guid)
            if to_chat >= self.max_per_chat:
                raise RateLimited(
                    f"imbridge already sent {to_chat} messages to {chat_guid} in the last minute (the limit is "
                    f"{self.max_per_chat}); it stops here in case something is looping"
                )
            if len(sends) >= self.max_total:
                raise RateLimited(
                    f"imbridge already sent {len(sends)} messages in the last minute (the limit is {self.max_total}); "
                    "it stops here in case something is looping"
                )
            sends.append((now, chat_guid))
            f.seek(0)
            f.truncate()
            f.write(json.dumps(sends))
