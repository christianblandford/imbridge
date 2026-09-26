"""Plain text and @mentions out of a message's attributedBody.

Since macOS 13, Messages often leaves message.text empty and keeps the text only in attributedBody: an
NSAttributedString archived in NeXT's typedstream format. The string sits right after the NSString class name as
'+', a length, then UTF-8 bytes. The length is one byte, or 0x81 followed by 2 bytes, or 0x82 followed by 4 bytes
(little-endian).

Each @mention is an attribute on the mentioned name: the key __kIMMentionConfirmedMention, then the person's phone
number or email as another string object (0x84, references to the NSString class and the '+' type, then the string).
"""

from __future__ import annotations

import re

_MENTION = b"__kIMMentionConfirmedMention"
_MENTION_VALUE = re.compile(rb"\x86\x92\x84[\x92-\xff]{2}")  # the key's end, then a new string object's references


def attributed_body_text(blob: bytes | None) -> str | None:
    if not blob:
        return None
    for marker in (b"NSString", b"NSMutableString"):
        start = blob.find(marker)
        if start >= 0:
            break
    else:
        return None
    plus = blob.find(b"+", start + len(marker))
    if plus < 0 or plus + 1 >= len(blob):
        return None
    return _string_at(blob, plus + 1)


def attributed_body_mentions(blob: bytes | None) -> tuple[str, ...]:
    """The phone numbers and emails @mentioned in a message, in order, each once."""
    found: list[str] = []
    start = 0
    while blob and (at := blob.find(_MENTION, start)) >= 0:
        start = at + len(_MENTION)
        value = _MENTION_VALUE.match(blob, start)
        address = _string_at(blob, value.end()) if value else None
        if address and address not in found:
            found.append(address)
    return tuple(found)


def _string_at(blob: bytes, i: int) -> str | None:
    """The length-prefixed UTF-8 string starting at blob[i]."""
    if i >= len(blob):
        return None
    if blob[i] == 0x81:
        length, i = int.from_bytes(blob[i + 1 : i + 3], "little"), i + 3
    elif blob[i] == 0x82:
        length, i = int.from_bytes(blob[i + 1 : i + 5], "little"), i + 5
    else:
        length, i = blob[i], i + 1
    return blob[i : i + length].decode("utf-8", errors="replace")
