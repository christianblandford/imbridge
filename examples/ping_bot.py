"""A tiny bot: in the one chat you name, answer "ping" with a 🏓 tapback and an inline "pong".

    imbridge allow +15551234567            # once, in your own terminal
    python examples/ping_bot.py +15551234567

It only ever reads and answers in that chat.
"""

import asyncio
import sys

from imbridge import IMBridge


async def main(target: str) -> None:
    async with IMBridge() as im:
        chat = im.chat(target)
        if not chat.can_send:
            sys.exit(f"imbridge may not send to {chat.guid} yet; run `imbridge allow {target}` first")
        print(f"listening in {chat!r}; send it 'ping'")
        async for message in chat.messages():
            if message.text and not message.reaction and message.text.strip().lower() == "ping":
                await chat.react(message, "🏓")
                await chat.reply(message, "pong")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1]))
