"""Polls (macOS 26 and iOS 26 and later): what a poll offers, who voted for what, and the tally.

A poll is an iMessage app balloon: balloon_bundle_id is POLLS_BUNDLE, and payload_data is an NSKeyedArchiver plist
whose URL holds the poll as base64 JSON in a data: URL.

    the poll    {"version": 1, "item": {"title": "", "creatorHandle": "+1555...", "orderedPollOptions":
                 [{"optionIdentifier": "<uuid>", "text": "Pizza", "creatorHandle": "+1555...", ...}, ...]}}
    a vote      {"version": 1, "item": {"votes": [{"voteOptionIdentifier": "<uuid>", "participantHandle": ...}]}}

In chat.db, associated_message_type 0 is the poll itself; 2 and 3 are updates carrying the full option list after
someone added a choice; 4000 is a vote, whose associated_message_guid is the poll (or latest update) it was cast on.
A vote holds the voter's whole current choice, so voting again replaces it and an empty vote takes it back. Every row
of one poll shares the payload's sessionIdentifier.

With each poll Messages also sends a plain "Sent a poll" (its POLL_FALLBACK text, for devices without polls), which
it doesn't show and imbridge skips, and then whatever the sender typed, usually the question: the poll's own title
is always empty.
"""

from __future__ import annotations

import base64
import binascii
import json
import plistlib
import urllib.parse
import uuid
from dataclasses import dataclass, field
from typing import Any

POLLS_BUNDLE = "com.apple.messages.MSMessageExtensionBalloonPlugin:0000000000:com.apple.messages.Polls"
POLL_TYPES = (0, 2, 3)  # associated_message_type: the poll, and updates after someone added a choice
VOTE_TYPE = 4000


@dataclass(frozen=True)
class PollOption:
    id: str
    text: str
    added_by: str | None = None  # the handle of whoever added it (the poll's creator, unless added later)


@dataclass(frozen=True)
class Poll:
    """What a poll message offers: its options as of that message (a later update may add more)."""

    session: str  # shared by every message of this poll, votes included
    options: tuple[PollOption, ...]
    creator: str | None = None  # the handle of whoever created the poll
    update_of: str | None = None  # for an update (someone added a choice), the poll message it updates


@dataclass(frozen=True)
class PollVote:
    """A vote: the voter's whole current choice in a poll. Voting again replaces it; empty means they took it back."""

    session: str
    poll_guid: str  # the poll message (or the update of it) the vote was cast on
    options: tuple[str, ...]  # PollOption ids


@dataclass(frozen=True)
class PollResults:
    """A poll's current state: its options, everyone's current choice, and the message sent with it."""

    guid: str  # the poll's message (its first one here, if it was updated)
    chat_guid: str | None
    session: str
    creator: str | None  # sender of the poll: a handle, or None if you created it
    question: str | None  # what the creator sent along with the poll, usually its question; None if nothing
    options: tuple[PollOption, ...]  # every option, including ones added later
    choices: dict[str | None, tuple[str, ...]] = field(default_factory=dict)  # voter (None: you) -> option ids
    latest: str = ""  # the newest message of the poll itself (after any added choices): what votes are cast on

    def voters(self, option: str) -> tuple[str | None, ...]:
        """Who currently picks an option (by id or text); None stands for you."""
        ids = {o.id for o in self.options if option in (o.id, o.text)}
        return tuple(voter for voter, picked in self.choices.items() if ids & set(picked))

    def counts(self) -> dict[str, int]:
        """Votes per option, by option text, in the poll's order."""
        return {o.text: len(self.voters(o.id)) for o in self.options}

    def to_dict(self) -> dict[str, Any]:
        return {
            "guid": self.guid,
            "chat_guid": self.chat_guid,
            "creator": self.creator,
            "question": self.question,
            "options": [
                {"id": o.id, "text": o.text, "votes": len(voters), "voters": ["me" if v is None else v for v in voters]}
                for o in self.options
                for voters in [self.voters(o.id)]
            ],
        }


def poll_payload(data: bytes | None) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """A poll row's payload_data: the archived dictionary, and the JSON inside its URL. None if it isn't a poll."""
    if not data:
        return None
    try:
        archive = _unarchive(plistlib.loads(data))
        url = archive.get("URL") if isinstance(archive, dict) else None
        if not isinstance(url, str) or not url.startswith("data:") or "," not in url:
            return None
        encoded = urllib.parse.unquote(url.split(",", 1)[1].split("?", 1)[0])
        raw = base64.b64decode(encoded.replace("-", "+").replace("_", "/") + "=" * (-len(encoded) % 4))
        content = json.loads(raw)
    except (plistlib.InvalidFileException, ValueError, binascii.Error, KeyError, IndexError, TypeError):
        return None
    if not isinstance(content, dict) or not isinstance(content.get("item"), dict):
        return None
    return archive, content


def parse_poll(associated_type: int | None, associated_guid: str | None, data: bytes | None) -> Poll | PollVote | None:
    """The Poll or PollVote a chat.db row with the Polls balloon holds."""
    decoded = poll_payload(data)
    if decoded is None:
        return None
    archive, content = decoded
    session = _text(archive.get("sessionIdentifier")) or ""
    item = content["item"]
    if (associated_type or 0) == VOTE_TYPE:
        votes = item.get("votes") if isinstance(item.get("votes"), list) else []
        picked = [vote.get("voteOptionIdentifier") for vote in votes if isinstance(vote, dict)]
        target = (associated_guid or "").split("/")[-1]  # a bare GUID; tolerate "p:0/GUID" too
        return PollVote(session, target, tuple(dict.fromkeys(p for p in picked if isinstance(p, str))))
    if (associated_type or 0) not in POLL_TYPES:
        return None
    options = tuple(
        PollOption(option["optionIdentifier"], _text(option.get("text")) or "", _text(option.get("creatorHandle")))
        for option in item.get("orderedPollOptions") or []
        if isinstance(option, dict) and isinstance(option.get("optionIdentifier"), str)
    )
    updated = (associated_guid or "").split("/")[-1] or None  # an update names the poll message it updates
    return Poll(session, options, _text(item.get("creatorHandle")), updated if associated_type else None)


def fallback_text(data: bytes | None) -> str | None:
    """The text Messages sends alongside a poll for devices without polls ("Sent a poll", localized)."""
    decoded = poll_payload(data)
    return _text(decoded[0].get("ldtext")) if decoded else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _unarchive(archive: dict[str, Any]) -> Any:
    """The root object of an NSKeyedArchiver plist, with dictionaries, arrays, strings, URLs and UUIDs resolved."""
    objects = archive["$objects"]

    def resolve(value: Any, depth: int = 0) -> Any:
        if depth > 20:
            return None
        if isinstance(value, plistlib.UID):
            value = objects[value.data]
        if isinstance(value, dict):
            if "NS.keys" in value and "NS.objects" in value:
                pairs = zip(value["NS.keys"], value["NS.objects"], strict=False)
                return {resolve(k, depth + 1): resolve(v, depth + 1) for k, v in pairs}
            if "NS.objects" in value:
                return [resolve(v, depth + 1) for v in value["NS.objects"]]
            if "NS.relative" in value:  # NSURL
                return resolve(value["NS.relative"], depth + 1)
            if "NS.uuidbytes" in value:  # NSUUID
                return str(uuid.UUID(bytes=bytes(value["NS.uuidbytes"]))).upper()
            if "NS.string" in value:  # NSMutableString
                return resolve(value["NS.string"], depth + 1)
            return None  # other objects aren't needed here
        return None if value == "$null" else value

    return resolve(archive["$top"]["root"])
