"""Formatted text: bold, italic, underline, strikethrough, animated text effects, and @mentions.

    await chat.send(["Meeting ", Span("moved", bold=True), " to ", Span("3pm", effect="big")])

Messages keeps formatting as attributes on runs of a message's text, all in one part (one bubble). iOS 18 and
macOS 15 or later show it; older devices get the plain text. An effect animates its run; a mention highlights a
person's name and notifies them, and only works for people in the chat.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

# The names in Messages' Text Effects menu, and what Messages calls each one inside.
TEXT_EFFECTS = {
    "big": "big",
    "small": "small",
    "shake": "shakeHorizontal",
    "nod": "shakeVertical",
    "explode": "explode",
    "ripple": "bounce",
    "bloom": "bloom",
    "jitter": "jitter",
    "somersault": "somersault",
    "squish": "squish",
    "stretch": "stretch",
}
_INTERNAL = {*TEXT_EFFECTS.values(), "scaleRipple"}
STYLES = ("bold", "italic", "underline", "strikethrough")


def text_effect(name: str) -> str:
    """Messages' own name for a text effect, from its menu name (any case) or that own name; ValueError otherwise.

    Messages silently sends plain text for a name it doesn't know, so an unknown one is refused here instead.
    """
    if name in _INTERNAL:
        return name
    try:
        return TEXT_EFFECTS[name.strip().lower()]
    except KeyError:
        raise ValueError(f"{name!r} isn't a text effect; use one of {', '.join(TEXT_EFFECTS)}") from None


@dataclass(frozen=True)
class Span:
    """A run of formatted text, for send() and reply(): Span("done", bold=True), Span("wow", effect="explode"),
    or Span("Sam", mention="+15551234567") to @mention someone in the chat (the text is how their name shows)."""

    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strikethrough: bool = False
    effect: str | None = None  # a text effect: a TEXT_EFFECTS name
    mention: str | None = None  # the phone number or email of someone in the chat

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("a Span needs some text")
        if self.effect is not None:
            text_effect(self.effect)


Text = str | Iterable[str | Span]
"""What send() and reply() take: plain text, or a sequence of plain strings and Spans."""


def spans(text: Text) -> list[Span] | None:
    """The Spans of formatted text, or None for plain text (a str, or strings with nothing formatted)."""
    if isinstance(text, str):
        return None
    found = [item if isinstance(item, Span) else Span(item) for item in text if item]
    if not found:
        raise ValueError("there's no text to send")
    if all(span == Span(span.text) for span in found):
        return None
    return found


def plain(text: Text) -> str:
    """The text without its formatting."""
    return text if isinstance(text, str) else "".join(item.text if isinstance(item, Span) else item for item in text)


def parts(found: list[Span]) -> list[dict[str, Any]]:
    """The helper's send-multipart parts for these Spans: each a run of the same message part, part 0."""
    return [
        {
            "partIndex": 0,
            "text": span.text,
            "styles": [style for style in STYLES if getattr(span, style)],
            "effect": text_effect(span.effect) if span.effect else None,
            "mention": span.mention,
        }
        for span in found
    ]
