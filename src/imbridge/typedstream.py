"""Plain text out of a message's attributedBody.

Since macOS 13, Messages often leaves message.text empty and keeps the text only in attributedBody: an
NSAttributedString archived in NeXT's typedstream format. The string sits right after the NSString class name as
'+', a length, then UTF-8 bytes. The length is one byte, or 0x81 followed by 2 bytes, or 0x82 followed by 4 bytes
(little-endian).
"""

from __future__ import annotations


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
    i = plus + 1
    if blob[i] == 0x81:
        length, i = int.from_bytes(blob[i + 1 : i + 3], "little"), i + 3
    elif blob[i] == 0x82:
        length, i = int.from_bytes(blob[i + 1 : i + 5], "little"), i + 5
    else:
        length, i = blob[i], i + 1
    return blob[i : i + length].decode("utf-8", errors="replace")
