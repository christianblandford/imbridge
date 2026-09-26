"""A tiny bot: in one chat you choose, answer "ping" with a 🏓 tapback and an inline "pong".

    python examples/ping_bot.py +15551234567      # or a chat GUID from `imbridge chats`

It ignores every other chat, so it's safe to leave running while you try things.
"""

import asyncio
import sys

from imbridge import IMBridge


async def main(chat: str) -> None:
    async with IMBridge() as im:
        chat_guid = im.resolve_chat(chat)
        print(f"listening in {chat_guid}; send it 'ping'")
        async for message in im.messages():
            if message.chat_guid != chat_guid or message.reaction or not message.text:
                continue
            if message.text.strip().lower() == "ping":
                await im.react(message, "🏓")
                await im.reply(message, "pong")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1]))
