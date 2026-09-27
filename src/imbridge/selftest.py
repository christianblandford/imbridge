"""`imbridge doctor --live`: try each feature in your note-to-self chat, and say what works on this Mac.

It sends about fifteen messages to yourself, each marked "🧪 imbridge self-test" with this run's token, and checks
every one against chat.db: a message, formatting, an inline reply, tapbacks, an edit, a file, a sticker, a location
pin, a link preview, a voice message, a poll and a vote, the typing indicator, an unsend, delivery, and search.
Features this macOS doesn't have are skipped, and so is Send Later: imbridge doesn't schedule messages to yourself.

The note-to-self chat is a one-to-one chat where both you and the other side are addresses of the account signed in
to Messages, so a chat with another number that merely passed through this Mac is never mistaken for it.
"""

from __future__ import annotations

import asyncio
import struct
import tempfile
import time
import uuid
import zlib
from collections.abc import Awaitable, Callable
from pathlib import Path

from .addresses import address_key
from .chatdb import Message
from .client import IMBridge
from .doctor import Check
from .guard import one_to_one_handle
from .richtext import Span

LANDED = 20.0  # seconds a test message has to show up in chat.db


async def find_self_chat(im: IMBridge) -> str | None:
    """Your note-to-self chat: the most recently active one-to-one chat from one of the signed-in account's addresses
    to another of them. None if there isn't one yet (send yourself a message in Messages first)."""
    aliases = (await im.account()).get("aliases") or []
    mine = {address_key(alias.get("Alias")) for alias in aliases if isinstance(alias, dict)} - {None}
    for chat in im.chats(300):
        handle = one_to_one_handle(chat.guid)
        on = address_key(chat.address) if chat.address else None
        if handle and address_key(handle) in mine and (on is None or on in mine):
            return chat.guid
    return None


async def run_live_checks(im: IMBridge, chat: str, report: Callable[[Check], None]) -> list[Check]:
    """Try each feature in chat (your note-to-self chat), reporting each result as it comes."""
    token = uuid.uuid4().hex[:6]
    label = f"🧪 imbridge self-test {token}"
    results: list[Check] = []
    sent: dict[str, str] = {}

    async def check(name: str, action: Callable[[], Awaitable[str | tuple[bool | None, str]]],
                    feature: str | None = None, skip: str | None = None) -> None:
        if feature is not None and not im.supports(feature):
            skip = "this macOS doesn't have it"
        if skip:
            result = Check(name, None, f"skipped: {skip}")
        else:
            try:
                outcome = await action()
                ok, detail = outcome if isinstance(outcome, tuple) else (True, outcome)
                result = Check(name, ok, detail)
            except Exception as error:  # a check that fails says why, and the others still run
                result = Check(name, False, f"{type(error).__name__}: {error}")
        results.append(result)
        report(result)

    async def landed(guid: str, test: Callable[[Message], object], what: str) -> Message:
        deadline = time.monotonic() + LANDED
        while True:
            found = im.message(guid)
            if found is not None and test(found):
                return found
            if time.monotonic() > deadline:
                raise TimeoutError(f"{what} didn't show up in chat.db within {LANDED:.0f}s")
            await asyncio.sleep(0.3)

    async def message() -> str:
        sent["first"] = await im.send(chat, f"{label}: a message")
        await landed(sent["first"], lambda m: m.text and token in m.text, "the message")
        return "sent, and in chat.db"

    async def formatting() -> str:
        guid = await im.send(chat, [Span("bold", bold=True), ", ", Span("italic", italic=True), f" ({label})"])
        await landed(guid, lambda m: m.text, "the formatted message")
        return "sent"

    async def reply() -> str:
        sent["reply"] = await im.reply(sent["first"], f"{label}: an inline reply")
        await landed(sent["reply"], lambda m: m.reply_to == sent["first"], "the reply")
        return "threaded under the first message"

    async def tapback() -> str:
        guid = await im.react(sent["first"], "love")
        await landed(guid, lambda m: m.reaction and m.reaction.label == "love", "the tapback")
        return "❤️ on the first message"

    async def emoji_tapback() -> str:
        guid = await im.react(sent["reply"], "🧪")  # on another message: one tapback per person per message
        await landed(guid, lambda m: m.reaction and m.reaction.label == "🧪", "the tapback")
        return "🧪 on the reply"

    async def edit() -> str:
        await im.edit(sent["first"], f"{label}: a message, edited")
        await landed(sent["first"], lambda m: m.edited_at, "the edit")
        return "the first message now says it was edited"

    async def send_file(folder: Path) -> str:
        guid = await im.send_file(chat, folder / "dot.png")
        await landed(guid, lambda m: m.attachments, "the file")
        return "a PNG arrived as an attachment"

    async def sticker(folder: Path) -> str:
        guid = await im.send_sticker(chat, folder / "dot.png", label="an orange dot")
        await landed(guid, lambda m: any(a.is_sticker for a in m.attachments), "the sticker")
        return "arrived marked as a sticker"

    async def location() -> str:
        guid = await im.send_location(chat, 37.334886, -122.008988, name="Apple Park")
        found = await landed(guid, lambda m: m.location, "the pin")
        return f"{found.location.name} at {found.location.latitude:.4f}, {found.location.longitude:.4f}"

    async def link() -> str | tuple[bool | None, str]:
        guid = await im.send_link(chat, "https://www.apple.com")
        found = await landed(guid, lambda m: m.text, "the link")
        if found.link is None:
            return None, "sent as a plain link: the preview didn't load (is this Mac online?)"
        return f"sent with its preview ({found.link.title})"

    async def voice() -> str:
        guid = await im.send_voice(chat, text=f"This is the imbridge self-test, run {' '.join(token)}.")
        await landed(guid, lambda m: m.is_voice, "the voice message")
        heard = await im.transcript(guid, timeout=10)
        return f"sent, and Messages transcribed it: {heard!r}" if heard else "sent (not transcribed yet)"

    async def poll() -> str:
        guid = await im.send_poll(chat, ["Yes", "No"], question=f"{label}: do polls work?")
        await landed(guid, lambda m: m.poll, "the poll")
        await im.vote(guid, "Yes")
        deadline = time.monotonic() + LANDED
        while not ((results_ := im.poll(guid)) and results_.choices.get(None)):
            if time.monotonic() > deadline:
                raise TimeoutError("the vote didn't show up in chat.db")
            await asyncio.sleep(0.3)
        return "sent, and voted Yes"

    async def typing() -> str:
        await im.typing(chat, True)
        await asyncio.sleep(1)
        await im.typing(chat, False)
        return "shown and hidden (Messages doesn't draw it in a chat with yourself)"

    async def unsend() -> str:
        guid = await im.send(chat, f"{label}: this one gets unsent")
        await landed(guid, lambda m: m.text, "the message")
        await im.unsend(guid)
        await landed(guid, lambda m: m.unsent_at, "the unsend")
        return "sent, then taken back"

    async def delivery() -> str | tuple[bool | None, str]:
        status = await im.wait_for_delivery(sent["first"], timeout=15)
        if status in ("delivered", "read"):
            return f"the first message is {status}"
        return (False if status == "failed" else None), f"the first message is {status or 'missing'}"

    async def search() -> str:
        found = im.search(token, chat=chat, limit=50)
        if not found:
            raise LookupError(f"searching for {token} found nothing")
        return f"found {len(found)} of this run's messages"

    with tempfile.TemporaryDirectory() as folder:
        work = Path(folder)
        (work / "dot.png").write_bytes(_dot())
        await check("Send a message", message)
        if "first" not in sent:
            return results  # the rest build on it
        await check("Formatting", formatting, "formatting")
        await check("Inline reply", reply)
        await check("Tapback", tapback)
        no_reply = None if "reply" in sent else "there's no reply to put it on"
        await check("Emoji tapback", emoji_tapback, "emoji_tapbacks", no_reply)
        await check("Edit", edit, "edits")
        await check("Send a file", lambda: send_file(work))
        await check("Sticker", lambda: sticker(work), "stickers")
        await check("Location pin", location)
        await check("Link preview", link)
        await check("Voice message", voice)
        await check("Poll and vote", poll, "polls")
        await check("Typing indicator", typing)
        await check("Unsend", unsend, "unsend")
        await check("Send Later", search, skip="imbridge doesn't schedule messages to yourself")  # (never runs)
        await check("Delivery", delivery)
        await check("Search", search)
    return results


def _dot(size: int = 96) -> bytes:
    """A PNG of an orange dot on a transparent background: something to send as a file and as a sticker."""
    center, radius = (size - 1) / 2, size * 0.42
    rows = []
    for y in range(size):
        row = bytearray(b"\x00")  # filter: none
        for x in range(size):
            inside = (x - center) ** 2 + (y - center) ** 2 <= radius**2
            row += bytes((255, 149, 0, 255)) if inside else bytes(4)
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(
        b"IEND", b""
    )
