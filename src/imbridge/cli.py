"""The imbridge command: doctor, start, send, reply, react, chats, history, watch."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import __version__
from .chatdb import FullDiskAccessError, Message
from .client import EFFECTS, ChatNotFound, IMBridge
from .doctor import run_checks
from .protocol import HelperError
from .reactions import CLASSIC_TAPBACKS


def describe(message: Message) -> str:
    when = message.date.astimezone().strftime("%H:%M:%S") if message.date else "--:--:--"
    who = "me" if message.is_from_me else (message.sender or "?")
    where = message.chat_name or message.chat_guid or "?"
    if reaction := message.reaction:
        body = f"{'removed ' if reaction.removed else ''}{reaction.label} on {reaction.target_guid}"
    else:
        body = message.text or ""
        if message.attachments:
            count = len(message.attachments)
            body += f" [{count} attachment{'s' if count > 1 else ''}]"
        if message.reply_to:
            body = f"(reply to {message.reply_to}) {body}"
    return f"[{when}] {who} in {where}: {body}  <{message.guid}>"


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


async def _start() -> int:
    async with IMBridge() as im:
        account = await im.account()
    status = account.get("login_status_message") or "unknown"
    print(f"helper ready: Messages is signed in as {account.get('apple_id')} (iMessage: {status})")
    return 0


async def _send(args: argparse.Namespace) -> int:
    async with IMBridge() as im:
        if args.command == "send":
            guid = await im.send(args.chat, args.text, reply_to=args.reply_to, effect=args.effect)
        elif args.command == "reply":
            guid = await im.reply(args.message, args.text)
        else:
            guid = await im.react(args.message, args.reaction, remove=args.remove)
    print(guid)
    return 0


async def _watch(args: argparse.Namespace) -> int:
    im = IMBridge(inject=False)
    async for message in im.messages(include_from_me=args.from_me):
        _print_message(message, args.json)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="imbridge", description="iMessage from Python and the command line: send, reply, react with any emoji."
    )
    parser.add_argument("--version", action="version", version=f"imbridge {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    commands.add_parser("doctor", help="check that this Mac is set up for imbridge")
    commands.add_parser("start", help="load the helper into Messages (restarting it, hidden) and wait until it answers")

    send = commands.add_parser("send", help="send a message")
    send.add_argument("chat", help="chat GUID, or the phone number / email of an existing conversation")
    send.add_argument("text")
    send.add_argument("--reply-to", metavar="GUID", help="send it as an inline reply to this message")
    send.add_argument("--effect", choices=sorted(EFFECTS), help="bubble or screen effect")

    reply = commands.add_parser("reply", help="reply inline to a message")
    reply.add_argument("message", metavar="GUID")
    reply.add_argument("text")

    react = commands.add_parser("react", help="tapback a message: a classic reaction or any emoji")
    react.add_argument("message", metavar="GUID")
    react.add_argument("reaction", help=f"{', '.join(CLASSIC_TAPBACKS)}, or any emoji")
    react.add_argument("--remove", action="store_true", help="take the tapback away")

    chats = commands.add_parser("chats", help="list recent chats")
    chats.add_argument("-n", type=int, default=20, help="how many (default 20)")
    chats.add_argument("--json", action="store_true", help="one JSON object per line")

    history = commands.add_parser("history", help="show a chat's latest messages")
    history.add_argument("chat", help="chat GUID, or the phone number / email of an existing conversation")
    history.add_argument("-n", type=int, default=20, help="how many (default 20)")
    history.add_argument("--json", action="store_true", help="one JSON object per line")

    watch = commands.add_parser("watch", help="print new messages, tapbacks and replies as they arrive")
    watch.add_argument("--json", action="store_true", help="one JSON object per line")
    watch.add_argument("--from-me", action="store_true", help="include messages you send")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "doctor":
            return _doctor()
        if args.command == "start":
            return asyncio.run(_start())
        if args.command in ("send", "reply", "react"):
            return asyncio.run(_send(args))
        if args.command == "watch":
            return asyncio.run(_watch(args))
        im = IMBridge(inject=False)
        if args.command == "chats":
            for chat in im.chats(args.n):
                if args.json:
                    print(json.dumps(chat.to_dict(), ensure_ascii=False))
                else:
                    label = chat.name or ", ".join(chat.participants) or chat.identifier
                    when = chat.last_message_at.astimezone().strftime("%Y-%m-%d %H:%M") if chat.last_message_at else "-"
                    print(f"{chat.guid}  {label}  ({when})")
        else:  # history
            for message in im.history(args.chat, args.n):
                _print_message(message, args.json)
        return 0
    except KeyboardInterrupt:
        return 130
    except (FullDiskAccessError, ChatNotFound, HelperError, TimeoutError, asyncio.TimeoutError) as e:
        print(f"imbridge: {e or type(e).__name__}", file=sys.stderr)
        if isinstance(e, (TimeoutError, asyncio.TimeoutError)):
            print("imbridge: the helper didn't answer; run `imbridge doctor`", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
