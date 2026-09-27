"""imbridge's MCP server: iMessage tools for Claude, Cursor and any other MCP client.

    imbridge mcp [--address +15550002222]

It sends only to chats the user allowed with `imbridge allow` (which needs a person at a terminal, so an agent can't
add chats), under the same address rules and rate limits as the library. Reading tools only touch chat.db; the first
send loads the helper into Messages.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__
from .addresses import AddressNotChosen, WrongAddress
from .chatdb import FullDiskAccessError, Message
from .client import EFFECTS, Chat, EditLimit, IMBridge
from .guard import RateLimited, SendNotAllowed
from .protocol import HelperError
from .reactions import CLASSIC_TAPBACKS
from .richtext import TEXT_EFFECTS, Span

INSTRUCTIONS = """\
These tools read and send iMessages on the user's Mac, through Messages.app.

- check_messages returns new messages since your last check; pass wait_seconds to wait for a reply.
- A tapback (react) is a light acknowledgement: react to a message when you start on it, then answer with reply,
  which threads your answer under that message. Any emoji works as a tapback.
- You can only send in chats the user has allowed; list_chats shows which (can_send). Only the user can allow a chat,
  from their own terminal. If a send is refused, tell the user why instead of looking for another way to send.
- Messages go to real people and can't be taken back, so be sure before you send.
{polls}{send_later}- To share a link, send it on its own with send_link: the recipient sees a card with the
  page's title and picture, as when a person pastes a link. A link inside send_message text stays plain text.
"""

# Lines of INSTRUCTIONS for tools that only some macOS versions have.
FEATURE_INSTRUCTIONS = {
    "polls": (
        "- A poll arrives as a message with `poll` (its options), and a vote as one with `vote`; read_poll shows the\n"
        "  current tally and the question sent with the poll.\n"
    ),
    "send_later": (
        "- send_later schedules a message with Messages' Send Later; list_scheduled and cancel_scheduled manage\n"
        "  what's waiting.\n"
    ),
}

READS = ToolAnnotations(read_only_hint=True, open_world_hint=False)
SENDS = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
CHANGES = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True)
MAX_WAIT = 55  # seconds; many MCP clients give up on a tool call after a minute


def _message(message: Message) -> dict[str, Any]:
    item: dict[str, Any] = {
        "guid": message.guid,
        "chat": message.chat_guid,
        "from": "me" if message.is_from_me else message.sender,
        "text": message.text,
        "date": message.date.astimezone().isoformat() if message.date else None,
    }
    if message.chat_name:
        item["chat_name"] = message.chat_name
    if message.unsent_at:
        item["unsent"] = True
    elif message.edited_at:
        item["edited"] = True
    if message.reply_to:
        item["reply_to"] = message.reply_to
    if message.mentions:
        item["mentions"] = list(message.mentions)
    if poll := message.poll:
        item["poll"] = {"options": [option.text for option in poll.options]}
        if poll.update_of:
            item["poll"]["adds_a_choice_to"] = poll.update_of
    if vote := message.vote:
        item["vote"] = {"in_poll": vote.poll_guid, "took_back": not vote.options}
    if link := message.link:
        item["link"] = {"url": link.url, "title": link.title, "summary": link.summary, "site": link.site_name}
    if place := message.location:
        item["location"] = {"latitude": place.latitude, "longitude": place.longitude, "name": place.name,
                            "address": place.address}
    if message.scheduled_for:
        item["scheduled_for"] = message.scheduled_for.astimezone().isoformat()
    if reaction := message.reaction:
        item["tapback"] = {"reaction": reaction.label, "removed": reaction.removed, "on": reaction.target_guid}
    if message.attachments:
        item["attachments"] = [
            {"name": a.name, "type": a.mime_type, "path": a.path, **({"sticker": True} if a.is_sticker else {})}
            for a in message.attachments
        ]
    return item


def _chat(chat: Chat) -> dict[str, Any]:
    return {
        "chat": chat.guid,
        "name": chat.name,
        "participants": list(chat.participants),
        "is_group": chat.is_group,
        "last_message_at": chat.last_message_at.astimezone().isoformat() if chat.last_message_at else None,
        "can_send": chat.can_send,
    }


def _when(value: str) -> datetime:
    """An ISO 8601 date and time; without an offset it's local time."""
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{value!r} isn't a date and time like 2026-09-27T09:00") from None


def _refusal(error: Exception) -> ToolError:
    """imbridge's errors, worded for a model: what happened, and what it should (not) do about it."""
    if isinstance(error, SendNotAllowed):
        advice = "Only the user can allow a chat, by running `imbridge allow <chat>` in their own terminal. Ask them."
    elif isinstance(error, EditLimit):
        advice = "iMessage won't allow it any more."
    elif isinstance(error, RateLimited):
        advice = "Wait a minute before sending more."
    elif isinstance(error, (AddressNotChosen, WrongAddress)):
        advice = "Only the user can fix this (with --address, or in Messages' settings). Tell them."
    elif isinstance(error, FullDiskAccessError):
        advice = "The app running this MCP server needs Full Disk Access. Tell the user."
    elif isinstance(error, HelperError):
        advice = "Messages' helper isn't answering; the user can run `imbridge doctor`."
    else:
        advice = ""
    return ToolError(f"{error} {advice}".strip())


def build_server(im: IMBridge) -> MCPServer:
    """The MCP server's tools, less those this Mac's macOS doesn't have (like polls before macOS 26)."""
    instructions = INSTRUCTIONS.format(
        **{feature: text if im.supports(feature) else "" for feature, text in FEATURE_INSTRUCTIONS.items()}
    )
    server = MCPServer("imbridge", instructions=instructions, version=__version__)

    def tool(feature: str | None = None, **options: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        if feature is not None and not im.supports(feature):
            return lambda function: function  # not offered on this macOS
        return server.tool(**options)

    cursors: dict[str | None, int] = {}  # per chat (None: every chat), the chat.db ROWID checked up to
    started: list[int] = []  # the ROWID when this server started: where every first check begins

    def start_rowid() -> int:
        if not started:
            started.append(im.db.max_rowid())
        return started[0]

    try:
        start_rowid()
    except FullDiskAccessError:
        pass  # check_messages reports it when called

    @server.tool(annotations=READS)
    def list_chats(limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        """Recent iMessage chats, newest first. `chat` identifies each one; `can_send` says if you may send there.
        For older chats, raise `offset` by `limit`."""
        try:
            offset = max(0, offset)
            return [_chat(chat) for chat in im.chats(offset + max(1, min(limit, 100)))[offset:]]
        except Exception as error:
            raise _refusal(error) from error

    @server.tool(annotations=READS)
    def read_messages(
        chat: str, limit: int = 20, before: str | None = None, after: str | None = None
    ) -> list[dict[str, Any]]:
        """A chat's latest messages, oldest first. `chat` is a chat id from list_chats, a phone number or email of an
        existing conversation, or a group's name. To read further back, pass `before`: the guid of the oldest message
        you have (an empty list means the start of the chat). `after` reads on from a message instead."""
        try:
            limit = max(1, min(limit, 200))
            return [_message(message) for message in im.history(chat, limit, before=before, after=after)]
        except Exception as error:
            raise _refusal(error) from error

    @server.tool(annotations=READS)
    def search_messages(
        query: str, chat: str | None = None, limit: int = 20, before: str | None = None
    ) -> list[dict[str, Any]]:
        """Messages containing `query` (ignoring case), newest first: in one chat, or in all of them. For more
        results, pass `before`: the guid of the last (oldest) one you got. To see what was said around a result, call
        read_messages with its chat and `before` or `after` set to its guid."""
        try:
            limit = max(1, min(limit, 100))
            return [_message(message) for message in im.search(query, chat=chat, limit=limit, before=before)]
        except Exception as error:
            raise _refusal(error) from error

    @server.tool(annotations=READS)
    async def check_messages(chat: str | None = None, wait_seconds: int = 0) -> list[dict[str, Any]]:
        """New incoming messages since your last check (texts, inline replies and tapbacks), oldest first.

        Pass `chat` to check one conversation, or leave it out for all. With wait_seconds (up to 55), waits for the
        next message instead of returning an empty list: use it to wait for a reply. The first check returns what
        arrived since this server started.
        """
        try:
            key = im.resolve_chat(chat) if chat else None
            if key not in cursors:
                cursors[key] = await asyncio.to_thread(start_rowid)
            found, cursors[key] = await im.new_messages(
                since=cursors[key], chat=key, wait=max(0, min(wait_seconds, MAX_WAIT))
            )
            return [_message(message) for message in found]
        except Exception as error:
            raise _refusal(error) from error

    @tool("polls", annotations=READS)
    def read_poll(message_guid: str) -> dict[str, Any]:
        """A poll's question, options, and votes per option with who cast them ("me" is the user). Takes the poll's
        message guid, or a vote's."""
        try:
            results = im.poll(message_guid)
        except Exception as error:
            raise _refusal(error) from error
        if results is None:
            raise ToolError(f"{message_guid} isn't a poll or a vote in one.")
        return results.to_dict()

    @server.tool(
        annotations=SENDS,
        description="Send a new message to an allowed chat, or start a conversation with a phone number (with its "
        "country code, like +15551234567) or email the user has allowed. Returns its guid. `effect` is an optional "
        f"bubble or screen effect: {', '.join(EFFECTS)}. `text_effect` animates the text itself: "
        f"{', '.join(TEXT_EFFECTS)}.",
    )
    async def send_message(
        chat: str, text: str, effect: str | None = None, text_effect: str | None = None
    ) -> dict[str, Any]:
        try:
            content = [Span(text, effect=text_effect)] if text_effect else text
            return {"guid": await im.send(chat, content, effect=effect)}
        except Exception as error:
            raise _refusal(error) from error

    @server.tool(annotations=SENDS)
    async def reply(message_guid: str, text: str, text_effect: str | None = None) -> dict[str, Any]:
        """Reply inline to a message (threaded under it, like swiping to reply). Returns the reply's guid.
        `text_effect` animates the text, as in send_message."""
        try:
            return {"guid": await im.reply(message_guid, [Span(text, effect=text_effect)] if text_effect else text)}
        except Exception as error:
            raise _refusal(error) from error

    @server.tool(
        annotations=SENDS,
        description=f"Tapback a message with any single emoji, or a classic: {', '.join(CLASSIC_TAPBACKS)}. "
        "A new tapback replaces your previous one on that message; remove=true takes it away.",
    )
    async def react(message_guid: str, reaction: str, remove: bool = False) -> dict[str, Any]:
        try:
            return {"guid": await im.react(message_guid, reaction, remove=remove)}
        except Exception as error:
            raise _refusal(error) from error

    @tool("polls", annotations=SENDS)
    async def send_poll(chat: str, options: list[str], question: str | None = None) -> dict[str, Any]:
        """Send a poll to an allowed chat: two or more options, and optionally a question, which goes out as a
        message right after the poll (Messages doesn't show poll titles). Returns the poll's guid."""
        try:
            return {"guid": await im.send_poll(chat, options, question=question)}
        except Exception as error:
            raise _refusal(error) from error

    @tool("polls", annotations=SENDS)
    async def vote(message_guid: str, option: str, remove: bool = False) -> dict[str, Any]:
        """Vote for an option in a poll (by its text), keeping the user's other choices; remove=true takes that vote
        back. message_guid is the poll's guid, or a vote's in it."""
        try:
            sent = await (im.unvote if remove else im.vote)(message_guid, option)
            return {"guid": sent, "changed": sent is not None}
        except Exception as error:
            raise _refusal(error) from error

    @server.tool(annotations=SENDS)
    async def send_location(chat: str, latitude: float, longitude: float, name: str | None = None) -> dict[str, Any]:
        """Send a location pin to an allowed chat: these coordinates, and optionally the place's name. Returns its
        guid."""
        try:
            return {"guid": await im.send_location(chat, latitude, longitude, name=name)}
        except Exception as error:
            raise _refusal(error) from error

    @server.tool(annotations=SENDS)
    async def send_link(chat: str, url: str) -> dict[str, Any]:
        """Send a link on its own with its preview: a card with the page's title, summary and picture, as when a
        person pastes a link into Messages. Only for pages on the public internet; a page that gives no preview goes as
        a plain link. Returns its guid."""
        try:
            return {"guid": await im.send_link(chat, url)}
        except Exception as error:
            raise _refusal(error) from error

    @tool("send_later", annotations=SENDS)
    async def send_later(chat: str, text: str, at: str) -> dict[str, Any]:
        """Schedule a message with Messages' Send Later: it goes out at the start of that minute even if nothing is
        running then. `at` is an ISO 8601 date and time, like 2026-09-27T09:00 (local time) or with an offset;
        a minute to 14 days ahead. Returns its guid."""
        try:
            return {"guid": await im.send_later(chat, text, _when(at))}
        except Exception as error:
            raise _refusal(error) from error

    @tool("send_later", annotations=READS)
    def list_scheduled(chat: str | None = None) -> list[dict[str, Any]]:
        """Messages waiting in Send Later (in one chat, or all), soonest first, each with scheduled_for."""
        try:
            return [_message(message) for message in im.scheduled(chat)]
        except Exception as error:
            raise _refusal(error) from error

    @tool("send_later", annotations=CHANGES)
    async def cancel_scheduled(message_guid: str) -> dict[str, Any]:
        """Take back a message waiting in Send Later, before it goes out."""
        try:
            await im.cancel_scheduled(message_guid)
            return {"cancelled": message_guid}
        except Exception as error:
            raise _refusal(error) from error

    @tool("edits", annotations=CHANGES)
    async def edit_message(message_guid: str, text: str) -> dict[str, Any]:
        """Change the text of a message you sent; readers see it marked Edited. iMessage allows 5 edits within
        15 minutes of sending."""
        try:
            await im.edit(message_guid, text)
            return {"edited": message_guid}
        except Exception as error:
            raise _refusal(error) from error

    @tool("unsend", annotations=CHANGES)
    async def unsend_message(message_guid: str) -> dict[str, Any]:
        """Take back a message you sent, for everyone in the chat. iMessage allows it within 2 minutes of sending."""
        try:
            await im.unsend(message_guid)
            return {"unsent": message_guid}
        except Exception as error:
            raise _refusal(error) from error

    @server.tool(annotations=SENDS)
    async def show_typing(chat: str, typing: bool = True) -> dict[str, Any]:
        """Show (or hide) the typing indicator in an allowed chat, e.g. while you work on a longer answer."""
        try:
            await im.typing(chat, typing)
            return {"typing": typing}
        except Exception as error:
            raise _refusal(error) from error

    @tool("focus_status", annotations=READS)
    async def focus_status(person: str) -> dict[str, Any]:
        """Whether someone has notifications silenced by a Focus (so a reply may take a while). `person` is a phone
        number or email, or a one-to-one chat id. `silenced` is null when they don't share their Focus status."""
        try:
            return {"person": person, "silenced": await im.focus_status(person)}
        except Exception as error:
            raise _refusal(error) from error

    @server.tool(annotations=READS)
    def whoami() -> dict[str, Any]:
        """Which of the user's addresses this server answers on, and the chats it may send to."""
        try:
            allowed = [_chat(chat) for chat in im.chats(200) if chat.can_send]
            address = im.address if isinstance(im.address, str) else ("any" if im.address else None)
            return {"address": address, "allowed_chats": allowed}
        except Exception as error:
            raise _refusal(error) from error

    return server


def serve(im: IMBridge) -> None:
    """Run the MCP server over stdio until the client disconnects."""
    build_server(im).run("stdio")

