"""A tiny bot: in the one chat you name, answer "ping" with a 🏓 tapback and an inline "pong".

    python examples/ping_bot.py +15551234567      # or an email, a group's name, or a chat GUID

The chat is allowed right here in the code, and the bot only ever reads and answers in it.
"""

import asyncio
import sys

from imbridge import IMBridge


async def main(target: str) -> None:
    async with IMBridge(allow=[target]) as im:
        chat = im.chat(target)
        print(f"listening in {chat!r}; send it 'ping'")
        async for message in chat.messages():
            if message.text and not message.reaction and message.text.strip().lower() == "ping":
                await chat.react(message, "🏓")
                await chat.reply(message, "pong")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1]))
