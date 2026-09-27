"""Incoming typing indicators: the helper's notices turned into is_typing, wait_while_typing and typing_changes."""

import asyncio

from helpers import ALEX, CREW, FakeMessages, free_port

from imbridge import IMBridge, TypingChange
from imbridge import client as client_module


def with_helper(chat_db, scenario, **kwargs):
    """Run scenario(im, fake) with imbridge connected to a stand-in helper that can push notices."""
    async def main():
        port = free_port()
        fake = FakeMessages(port)
        task = asyncio.create_task(fake.run())
        im = IMBridge(chat_db=chat_db, token="t", inject=False, port=port, poll_interval=0.01, **kwargs)
        try:
            await im.start()
            return await scenario(im, fake)
        finally:
            await im.close()
            task.cancel()

    return asyncio.run(main())


def notice(chat, typing):
    return {"event": "started-typing" if typing else "stopped-typing", "guid": chat}


async def settle():
    await asyncio.sleep(0.05)


def test_typing_state(chat_db):
    async def scenario(im, fake):
        states = [im.is_typing(ALEX)]
        await fake.push(notice(ALEX, True))
        await settle()
        states += [im.is_typing(ALEX), im.chat(ALEX).is_typing(), im.is_typing(CREW)]
        await fake.push(notice(ALEX, False))
        await settle()
        return [*states, im.is_typing(ALEX)]

    assert with_helper(chat_db, scenario) == [False, True, True, False, False]


def test_changes_are_reported_once(chat_db):
    async def scenario(im, fake):
        changes = []

        async def watch():
            async for change in im.typing_changes():
                changes.append(change)

        watcher = asyncio.create_task(watch())
        await settle()
        # a redrawn conversation list says "not typing" about every chat, over and over
        for event in [notice(CREW, False), notice(ALEX, True), notice(ALEX, True), notice(ALEX, False),
                      notice(ALEX, False), notice(CREW, False)]:
            await fake.push(event)
        await settle()
        watcher.cancel()
        return changes

    changes = with_helper(chat_db, scenario)
    assert [(change.chat_guid, change.typing) for change in changes] == [(ALEX, True), (ALEX, False)]
    assert all(isinstance(change, TypingChange) for change in changes)


def test_waiting_while_someone_types(chat_db):
    async def scenario(im, fake):
        assert await im.wait_while_typing(ALEX, timeout=0.1)  # nobody typing: no wait
        await fake.push(notice(ALEX, True))
        await settle()
        still = await im.wait_while_typing(ALEX, timeout=0.2)

        async def stop_soon():
            await asyncio.sleep(0.2)
            await fake.push(notice(ALEX, False))

        asyncio.create_task(stop_soon())
        started = asyncio.get_running_loop().time()
        done = await im.chat(ALEX).wait_while_typing(timeout=5)
        return still, done, asyncio.get_running_loop().time() - started

    still, done, waited = with_helper(chat_db, scenario)
    assert (still, done) == (False, True) and waited < 2


def test_a_forgotten_bubble_goes_away(chat_db, monkeypatch):
    monkeypatch.setattr(client_module, "TYPING_TIMEOUT", 0.2)  # Messages drops a bubble after a minute of silence

    async def scenario(im, fake):
        await fake.push(notice(ALEX, True))
        await settle()
        before = im.is_typing(ALEX)
        await asyncio.sleep(0.3)
        return before, im.is_typing(ALEX)

    assert with_helper(chat_db, scenario) == (True, False)
