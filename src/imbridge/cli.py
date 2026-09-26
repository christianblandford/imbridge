"""The imbridge command: doctor, start, allow, send, reply, react, chats, history, watch, mcp."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import __version__
from .addresses import ANY_ADDRESS, AddressNotChosen, WrongAddress, contact_address
from .chatdb import FullDiskAccessError, Message
from .client import EFFECTS, Chat, ChatNotFound, EditLimit, IMBridge, WrongChat
from .doctor import run_checks
from .guard import ANY_LINE, RateLimited, SendNotAllowed, read_allowed, write_allowed
from .polls import PollResults
from .protocol import HelperError
from .reactions import CLASSIC_TAPBACKS
from .richtext import TEXT_EFFECTS, Span

_EVENTS = {
    "added": "added {person}",
    "removed": "removed {person}",
    "left": "left the group",
    "renamed": "renamed the group to {name}",
    "photo_changed": "changed the group photo",
    "photo_removed": "removed the group photo",
}


def describe(message: Message) -> str:
    when = message.date.astimezone().strftime("%H:%M:%S") if message.date else "--:--:--"
    who = "me" if message.is_from_me else (message.sender or "?")
    where = message.chat_name or message.chat_guid or "?"
    if message.address:
        where += f" via {message.address}"
    if event := message.event:
        who = message.sender or "me"  # who made the change
        template = _EVENTS.get(event.kind, "changed the group ({code})")
        if event.kind == "renamed" and not event.name:
            template = "removed the group's name"
        body = template.format(person=event.person or "me", name=f'"{event.name}"', code="/".join(map(str, event.code)))
    elif poll := message.poll:
        verb = "added a choice to a poll" if poll.update_of else "sent a poll"
        body = f"{verb}: {' / '.join(option.text for option in poll.options)}"
    elif vote := message.vote:
        body = f"{'voted' if vote.options else 'took back their vote'} in poll {vote.poll_guid}"
    elif reaction := message.reaction:
        body = f"{'removed ' if reaction.removed else ''}{reaction.label} on {reaction.target_guid}"
    else:
        body = message.text or ""
        if message.attachments:
            count = len(message.attachments)
            body += f" [{count} attachment{'s' if count > 1 else ''}]"
        if message.reply_to:
            body = f"(reply to {message.reply_to}) {body}"
    return f"[{when}] {who} in {where}: {body}  <{message.guid}>"


def _print_poll(results: PollResults, as_json: bool) -> None:
    if as_json:
        print(json.dumps(results.to_dict(), ensure_ascii=False))
        return
    print(results.question or "(a poll with no question)")
    for option in results.to_dict()["options"]:
        voters = f"  ({', '.join(option['voters'])})" if option["voters"] else ""
        print(f"  {option['votes']:>3}  {option['text']}{voters}")


def _label(chat: Chat) -> str:
    return chat.name or ", ".join(chat.participants) or chat.guid


def _bridge(args: argparse.Namespace, *, inject: bool = True) -> IMBridge:
    address = getattr(args, "address", None)
    return IMBridge(address=ANY_ADDRESS if address and address.lower() == "any" else address, inject=inject)


def _mcp(args: argparse.Namespace) -> int:
    try:
        from .mcp_server import serve
    except ModuleNotFoundError as e:
        if (e.name or "").split(".")[0] != "mcp":
            raise
        print("imbridge: the MCP server needs the mcp package: pip install 'imbridge[mcp]'", file=sys.stderr)
        return 1
    serve(_bridge(args))
    return 0


def _print_message(message: Message, as_json: bool) -> None:
    print(json.dumps(message.to_dict(), ensure_ascii=False) if as_json else describe(message), flush=True)


def _doctor() -> int:
    checks = run_checks()
    for check in checks:
        mark = {True: "✓", False: "✗", None: "·"}[check.ok]
        print(f"{mark} {check.name}: {check.detail}")
        if check.fix and check.ok is not True:
            print(f"    {'fix' if check.ok is False else 'next'}: {check.fix}")
    return 1 if any(check.ok is False for check in checks) else 0


def _allow(args: argparse.Namespace) -> int:
    # Widening who imbridge may message takes a person at a terminal, so no script or AI agent can do it on its own.
    if not sys.stdin.isatty():
        print(
            "imbridge: `imbridge allow` must be run by a person in a terminal (it asks you to confirm), so that no "
            "script or AI agent can widen who imbridge may message.",
            file=sys.stderr,
        )
        return 1
    allowed = read_allowed()
    if args.any:
        print("This lets imbridge send to ANY chat: every contact and every group. Only the rate limits still apply.")
        if input('Type "any chat" to confirm: ').strip().lower() != "any chat":
            print("Nothing changed.")
            return 1
        allowed.add(ANY_LINE)
        write_allowed(allowed)
        print("imbridge may now send to any chat. Undo it with `imbridge disallow --any`.")
        return 0
    if not args.chat:
        print("imbridge: name a chat (GUID, phone number or email), or use --any", file=sys.stderr)
        return 2
    try:
        chat = IMBridge(inject=False).chat(args.chat)
    except ChatNotFound:
        person = contact_address(args.chat)
        if person is None:
            raise
        answer = input(f"You have no conversation with {person} yet. Let imbridge start one and message them? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing changed.")
            return 1
        allowed.add(person)
        write_allowed(allowed)
        print(f"imbridge may now message {person}.")
        return 0
    members = f" ({', '.join(chat.participants)})" if chat.is_group and chat.participants else ""
    answer = input(f"Let imbridge send to {_label(chat)}{members}, chat {chat.guid}? [y/N] ")
    if answer.strip().lower() not in ("y", "yes"):
        print("Nothing changed.")
        return 1
    allowed.add(chat.guid)
    write_allowed(allowed)
    print(f"imbridge may now send to {_label(chat)}.")
    return 0


def _disallow(args: argparse.Namespace) -> int:
    allowed = read_allowed()
    target = ANY_LINE if args.any else (IMBridge(inject=False).resolve_chat(args.chat) if args.chat else None)
    if target is None:
        print("imbridge: name a chat, or use --any", file=sys.stderr)
        return 2
    if target not in allowed:
        print("That chat wasn't allowed; nothing changed.")
        return 0
    allowed.discard(target)
    write_allowed(allowed)
    print("Done: imbridge may no longer send there." if target != ANY_LINE else "Done: 'any chat' is off.")
    return 0


def _allowed() -> int:
    allowed = read_allowed()
    if not allowed:
        print("imbridge may not send anywhere yet (read-only). Allow a chat with `imbridge allow <chat>`.")
        return 0
    if ANY_LINE in allowed:
        print("* any chat (imbridge allow --any)")
    im = IMBridge(inject=False)
    for entry in sorted(allowed - {ANY_LINE}):
        try:
            print(f"{entry}  {_label(im.chat(entry))}")
        except ChatNotFound:
            print(f"{entry}  (no conversation yet)")
    return 0


async def _start(args: argparse.Namespace) -> int:
    async with _bridge(args) as im:
        account = await im.account()
    status = account.get("login_status_message") or "unknown"
    print(f"helper ready: Messages is signed in as {account.get('apple_id')} (iMessage: {status})")
    return 0


async def _send(args: argparse.Namespace) -> int:
    async with _bridge(args) as im:
        text = getattr(args, "text", "")
        if getattr(args, "text_effect", None):
            text = [Span(text, effect=args.text_effect)]
        if args.command == "send":
            guid = await im.send(args.chat, text, reply_to=args.reply_to, effect=args.effect)
        elif args.command == "send-file":
            guid = await im.send_file(args.chat, args.path, reply_to=args.reply_to)
        elif args.command == "send-poll":
            guid = await im.send_poll(args.chat, args.options, question=args.question)
        elif args.command == "vote":
            guid = await (im.unvote if args.remove else im.vote)(args.poll, *args.options)
            guid = guid or "(no change: that was already your choice)"
        elif args.command == "reply":
            guid = await im.reply(args.message, text)
        else:
            guid = await im.react(args.message, args.reaction, remove=args.remove)
    print(guid)
    return 0


async def _watch(args: argparse.Namespace) -> int:
    im = _bridge(args, inject=False)
    stream = im.chat(args.chat).messages if args.chat else im.all_messages
    async for message in stream(include_from_me=args.from_me, include_events=args.events):
        _print_message(message, args.json)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="imbridge", description="iMessage from Python and the command line: send, reply, react with any emoji."
    )
    parser.add_argument("--version", action="version", version=f"imbridge {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    mine = argparse.ArgumentParser(add_help=False)
    mine.add_argument(
        "--address",
        help="which of your own addresses this is (phone number or email), or 'any'; default $IMBRIDGE_ADDRESS",
    )

    commands.add_parser("doctor", help="check that this Mac is set up for imbridge")
    commands.add_parser(
        "start", parents=[mine], help="load the helper into Messages (restarting it, hidden) and wait until it answers"
    )

    allow = commands.add_parser("allow", help="let imbridge send to a chat (asks you to confirm)")
    allow.add_argument("chat", nargs="?", help="phone number, email, group name, or chat GUID")
    allow.add_argument("--any", action="store_true", help="every chat (asks you to type a confirmation)")
    disallow = commands.add_parser("disallow", help="stop imbridge sending to a chat")
    disallow.add_argument("chat", nargs="?")
    disallow.add_argument("--any", action="store_true", help="turn off 'any chat'")
    commands.add_parser("allowed", help="list the chats imbridge may send to")

    send = commands.add_parser("send", parents=[mine], help="send a message to an allowed chat")
    send.add_argument("chat", help="phone number, email, group name, or chat GUID")
    send.add_argument("text")
    send.add_argument("--reply-to", metavar="GUID", help="send it as an inline reply to this message")
    send.add_argument("--effect", choices=sorted(EFFECTS), help="bubble or screen effect")
    send.add_argument("--text-effect", choices=list(TEXT_EFFECTS), help="animate the text itself")

    send_file = commands.add_parser("send-file", parents=[mine], help="send a photo, GIF, video or document")
    send_file.add_argument("chat", help="phone number, email, group name, or chat GUID")
    send_file.add_argument("path")
    send_file.add_argument("--reply-to", metavar="GUID", help="send it as an inline reply to this message")

    send_poll = commands.add_parser("send-poll", parents=[mine], help="send a poll to an allowed chat")
    send_poll.add_argument("chat", help="phone number, email, group name, or chat GUID")
    send_poll.add_argument("options", nargs="+", metavar="OPTION", help="two or more")
    send_poll.add_argument("--question", help="sent as a message right after the poll")

    vote = commands.add_parser("vote", parents=[mine], help="vote in a poll, keeping your other choices")
    vote.add_argument("poll", metavar="GUID", help="the poll's message, or a vote in it")
    vote.add_argument("options", nargs="*", metavar="OPTION", help="by text or id")
    vote.add_argument("--remove", action="store_true", help="take back your vote for these (or all) options")

    reply = commands.add_parser("reply", parents=[mine], help="reply inline to a message in an allowed chat")
    reply.add_argument("message", metavar="GUID")
    reply.add_argument("text")
    reply.add_argument("--text-effect", choices=list(TEXT_EFFECTS), help="animate the text itself")

    react = commands.add_parser(
        "react", parents=[mine], help="tapback a message in an allowed chat: a classic reaction or any emoji"
    )
    react.add_argument("message", metavar="GUID")
    react.add_argument("reaction", help=f"{', '.join(CLASSIC_TAPBACKS)}, or any emoji")
    react.add_argument("--remove", action="store_true", help="take the tapback away")

    chats = commands.add_parser("chats", help="list recent chats")
    chats.add_argument("-n", type=int, default=20, help="how many (default 20)")
    chats.add_argument("--json", action="store_true", help="one JSON object per line")

    poll = commands.add_parser("poll", parents=[mine], help="show a poll's options, votes and question")
    poll.add_argument("message", metavar="GUID", help="the poll's message, or a vote in it")
    poll.add_argument("--json", action="store_true", help="as a JSON object")

    history = commands.add_parser("history", parents=[mine], help="show a chat's latest messages")
    history.add_argument("chat", help="phone number, email, group name, or chat GUID")
    history.add_argument("-n", type=int, default=20, help="how many (default 20)")
    history.add_argument("--json", action="store_true", help="one JSON object per line")

    watch = commands.add_parser("watch", parents=[mine], help="print new messages, tapbacks and replies as they arrive")
    watch.add_argument("--chat", help="only this chat")
    watch.add_argument("--json", action="store_true", help="one JSON object per line")
    watch.add_argument("--from-me", action="store_true", help="include messages you send")
    watch.add_argument("--events", action="store_true", help="include changes to groups (people added, renames...)")

    commands.add_parser("mcp", parents=[mine], help="run the MCP server (stdio) for Claude, Cursor and other clients")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "doctor":
            return _doctor()
        if args.command == "allow":
            return _allow(args)
        if args.command == "disallow":
            return _disallow(args)
        if args.command == "allowed":
            return _allowed()
        if args.command == "start":
            return asyncio.run(_start(args))
        if args.command in ("send", "send-file", "send-poll", "vote", "reply", "react"):
            return asyncio.run(_send(args))
        if args.command == "watch":
            return asyncio.run(_watch(args))
        if args.command == "mcp":
            return _mcp(args)
        im = _bridge(args, inject=False)
        if args.command == "chats":
            for chat in im.chats(args.n):
                if args.json:
                    print(json.dumps({**chat.to_dict(), "can_send": chat.can_send}, ensure_ascii=False))
                else:
                    when = chat.last_message_at.astimezone().strftime("%Y-%m-%d %H:%M") if chat.last_message_at else "-"
                    marker = "  [can send]" if chat.can_send else ""
                    print(f"{chat.guid}  {_label(chat)}  ({when}){marker}")
        elif args.command == "poll":
            results = im.poll(args.message)
            if results is None:
                print(f"imbridge: {args.message} isn't a poll or a vote in one", file=sys.stderr)
                return 1
            _print_poll(results, args.json)
        else:  # history
            for message in im.history(args.chat, args.n):
                _print_message(message, args.json)
        return 0
    except KeyboardInterrupt:
        return 130
    except (
        FullDiskAccessError,
        ChatNotFound,
        WrongChat,
        EditLimit,
        FileNotFoundError,
        ValueError,
        SendNotAllowed,
        AddressNotChosen,
        WrongAddress,
        RateLimited,
        HelperError,
        TimeoutError,
        asyncio.TimeoutError,
    ) as e:
        print(f"imbridge: {e or type(e).__name__}", file=sys.stderr)
        if isinstance(e, (TimeoutError, asyncio.TimeoutError)):
            print("imbridge: the helper didn't answer; run `imbridge doctor`", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
