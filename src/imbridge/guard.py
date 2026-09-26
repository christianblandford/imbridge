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

ANY_LINE = "*"  # in the allowed-chats file: every chat


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
        self.max_per_chat = max_per_chat
        self.max_total = max_total
        self.window = window
        self.clock = clock

    def resolve_allowed(self) -> set[str]:
        """The chat GUIDs allowed in code; raises for an entry that matches no chat, so a typo fails at startup."""
        if self._resolved is None:
            chats = () if isinstance(self._allow, AnyChat) else self._allow
            self._resolved = {self._resolve(chat) for chat in chats}
        return self._resolved

    def allows(self, chat_guid: str) -> bool:
        if isinstance(self._allow, AnyChat):
            return True
        listed = read_allowed()  # read every time, so `imbridge disallow` takes effect in running programs
        if ANY_LINE in listed or chat_guid in listed:
            return True
        return chat_guid in self.resolve_allowed()

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
