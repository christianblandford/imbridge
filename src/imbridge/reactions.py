"""Tapbacks: the six classics by name, and any emoji.

In chat.db a tapback is its own message row: associated_message_type says which reaction (2000-2005 add a classic,
3000-3005 remove one, 2006/3006 add/remove an emoji kept in associated_message_emoji, 1000 is a sticker) and
associated_message_guid names the target, as "p:<part>/<guid>" or "bp:<guid>".
"""

from __future__ import annotations

from dataclasses import dataclass

CLASSIC_TAPBACKS = ("love", "like", "dislike", "laugh", "emphasize", "question")
_CODES = {name: 2000 + i for i, name in enumerate(CLASSIC_TAPBACKS)}
_NAMES = {code: name for name, code in _CODES.items()}
EMOJI_TAPBACK = 2006
STICKER = 1000


@dataclass(frozen=True)
class Reaction:
    kind: str  # one of CLASSIC_TAPBACKS, "emoji", "sticker" or "unknown"
    emoji: str | None  # the emoji, when kind == "emoji"
    removed: bool  # True when this row takes an earlier tapback away
    target_guid: str  # the message reacted to
    target_part: int  # which part of that message (0 unless it has several)

    @property
    def label(self) -> str:
        return self.emoji if self.kind == "emoji" and self.emoji else self.kind


def reaction_type(reaction: str, remove: bool = False) -> str:
    """The helper's reactionType for a classic tapback name or any emoji: "love", "-love", "emoji:👀", "-emoji:👀"."""
    reaction = reaction.strip()
    if not reaction:
        raise ValueError("a reaction needs a classic tapback name or an emoji")
    base = reaction.lower() if reaction.lower() in _CODES else f"emoji:{reaction}"
    return f"-{base}" if remove else base


def parse_target(associated_guid: str) -> tuple[str, int]:
    """'p:1/GUID' -> ('GUID', 1); 'bp:GUID' -> ('GUID', 0); a bare GUID -> (GUID, 0)."""
    if associated_guid.startswith("p:"):
        part, _, guid = associated_guid[2:].partition("/")
        return guid, int(part) if part.isdigit() else 0
    if associated_guid.startswith("bp:"):
        return associated_guid[3:], 0
    return associated_guid, 0


def parse_reaction(
    associated_type: int | None, associated_guid: str | None, emoji: str | None = None
) -> Reaction | None:
    """The Reaction a chat.db row represents, or None if it isn't a tapback or sticker."""
    if not associated_guid or not associated_type:
        return None
    target, part = parse_target(associated_guid)
    if associated_type == STICKER:
        return Reaction("sticker", None, False, target, part)
    if not 2000 <= associated_type < 4000:
        return None
    removed = associated_type >= 3000
    code = associated_type - 1000 if removed else associated_type
    if code in _NAMES:
        return Reaction(_NAMES[code], None, removed, target, part)
    if code == EMOJI_TAPBACK:
        return Reaction("emoji", emoji, removed, target, part)
    return Reaction("unknown", emoji, removed, target, part)
