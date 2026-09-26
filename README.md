# imbridge

**iMessage for Python and AI agents on macOS.** Send messages, reply inline, tapback with *any* emoji, and receive
everything as it arrives, straight through Messages.app. No server to run, no Electron app, no cloud relay.

```python
import asyncio
from imbridge import IMBridge

async def main():
    async with IMBridge() as im:
        async for message in im.messages():  # new messages, tapbacks and inline replies
            if message.text and not message.reaction:
                await im.react(message, "👀")  # any emoji, not just the classic six
                await im.reply(message, "on it")  # an inline (threaded) reply

asyncio.run(main())
```

## Why imbridge

- **Nothing to host.** Your agent imports a library. There's no server process, Electron app, web UI or open port
  beyond a localhost socket that only your process can use.
- **Any-emoji tapbacks.** The six classics plus any emoji, the way iOS 18 and macOS 15 do it. Other tools can
  *read* emoji tapbacks; imbridge sends them.
- **The real iMessage features.** Inline replies, tapbacks, effects, typing indicators and read receipts, done by
  Messages itself rather than by clicking through its UI.
- **Current.** Tested on macOS 27. It includes a fix for a macOS 26+ crash that hits other Messages helpers when
  they reply or react while someone is typing.
- **Built for agents.** An async Python API, a JSON-lines `watch` stream, and one-shot CLI commands.

## How it works

```
                 localhost socket + token
your Python  ───────────────────────────────▶  helper inside Messages.app  ──▶  iMessage
    ▲                                              (calls IMCore directly)
    │  read-only SQLite
    └──────────────  ~/Library/Messages/chat.db  ◀──  Messages
```

- **Sending** goes through a small helper library loaded into Messages. It calls IMCore, Apple's private Messages
  framework, so a reply or tapback is the same operation as tapping it in the app. The helper is
  [BlueBubbles'](https://github.com/BlueBubblesApp/bluebubbles-helper) Private API helper with
  [a few patches](helper/patches), built from source.
- **Receiving** reads `chat.db`, Messages' own database, read-only. Tapbacks, inline replies, attachments and group
  chats come through as structured fields.

## Requirements

- An **Apple Silicon** Mac running macOS 11 or later (tested on macOS 27.0), signed in to iMessage in Messages.
- **Python 3.10+**.
- **System Integrity Protection disabled**, plus two related settings (below). macOS only lets a helper load into
  Messages this way. BlueBubbles' Private API and imsg's advanced features have the same requirement.

> [!WARNING]
> Disabling SIP lowers the security of the whole Mac, not just Messages. If you can, run imbridge on a Mac dedicated
> to it (an old Mac mini is ideal). While SIP is off, iPhone and iPad apps from the App Store won't open.

## Setup

**1. Let macOS load the helper** (once):

1. Shut down, then hold the power button until "Loading startup options" appears. Choose **Options**, then
   **Utilities > Terminal**, and run `csrutil disable`. Restart.
2. In Terminal:
   ```bash
   sudo nvram boot-args="-arm64e_preview_abi"
   sudo defaults write /Library/Preferences/com.apple.security.libraryvalidation.plist DisableLibraryValidation -bool true
   ```
3. Restart again.

**2. Give Full Disk Access** to the app that runs your Python (Terminal, iTerm, VS Code, or your agent's app) under
System Settings > Privacy & Security > Full Disk Access. imbridge needs it to read `chat.db`.

**3. Install and check:**

```bash
pip install imbridge
imbridge doctor   # checks every requirement, including that the helper matches your macOS version
imbridge start    # loads the helper into Messages (restarting Messages, hidden) and waits until it answers
```

## Python API

```python
async with IMBridge() as im:
    guid = await im.send("+15551234567", "hello")         # a chat GUID, or the number/email of an existing chat
    await im.send("any;+;chat123456", "hi all", effect="confetti")
    await im.reply(guid, "replying inline")               # a Message or a message GUID
    await im.react(guid, "love")                          # love, like, dislike, laugh, emphasize, question
    await im.react(guid, "🔥")                            # ...or any emoji
    await im.react(guid, "🔥", remove=True)
    await im.typing("+15551234567")                       # typing indicator on (on=False turns it off)
    await im.mark_read("+15551234567")

    im.chats(limit=20)                                    # recent chats, newest first
    im.history("+15551234567", limit=50)                  # a chat's latest messages, oldest first
    async for message in im.messages(include_from_me=False):
        ...
```

Every `Message` has these fields:

| field | |
|---|---|
| `guid`, `rowid` | the message's ID, and its row in chat.db |
| `chat_guid`, `is_group`, `chat_name` | where it was sent |
| `sender`, `is_from_me` | the sender's phone number or email; `None` when you sent it |
| `text`, `date`, `service` | the text (decoded from `attributedBody` when needed), a UTC datetime, and `iMessage`/`SMS`/`RCS` |
| `reply_to` | the GUID of the message this is an inline reply to |
| `reaction` | set when the row is a tapback: `kind` (`love`…`question`, `emoji`, `sticker`), `emoji`, `removed`, `target_guid`, `target_part` |
| `attachments` | each with a `path` on disk, `mime_type` and `name` |

`IMBridge(inject=True)` loads the helper into Messages whenever none answers. Pass `inject=False` if something else
manages Messages. Receiving (`messages`, `history`, `chats`) only reads `chat.db`, so it works without the helper.

Effects: `slam`, `loud`, `gentle`, `invisible_ink`, `echo`, `spotlight`, `balloons`, `confetti`, `love`, `lasers`,
`fireworks`, `celebration`.

## Command line

```bash
imbridge send +15551234567 "hello" [--reply-to GUID] [--effect confetti]
imbridge reply GUID "inline reply"
imbridge react GUID 🔥 [--remove]
imbridge chats [-n 20] [--json]
imbridge history +15551234567 [-n 20] [--json]
imbridge watch [--json] [--from-me]    # streams new messages, tapbacks and replies; --json gives one object per line
```

`send`, `reply` and `react` print the new message's GUID.

## Using it with an AI agent

Use imbridge as a library inside your agent's loop, as in the example at the top. You can also have an agent read
`imbridge watch --json` and answer with `imbridge reply` and `imbridge react`. [examples/](examples) has a runnable
bot that answers only in a chat you name.

## Troubleshooting

- Run **`imbridge doctor`** first. Before anything is injected, it checks SIP, the boot-arg, library validation, Full
  Disk Access, and that every private API the helper calls still exists on your macOS. That last check matters
  after macOS updates: a helper that calls something Apple removed would crash Messages on launch.
- **Helper logs:** `log stream --predicate 'subsystem == "imbridge"'`
- **Messages disappears when the helper loads.** It restarts hidden; open it from the Dock as usual.
- **Testing in a chat with yourself.** Everything you send also comes back as received. Messages then replaces
  your own tapback rows with the echoed ones, and iMessage keeps only one tapback per person per message.

## Security

- The helper can do anything Messages can, including send as you. It only talks to a process on the same Mac:
  imbridge listens on `127.0.0.1`/`::1`, and each request must carry a token that imbridge generates
  (`~/Library/Application Support/imbridge/token`, readable only by you) and passes to Messages when it launches it.
- The bigger trade-off is SIP (see the warning above).

## How the helper is built

[`helper/UPSTREAM`](helper/UPSTREAM) pins a commit of BlueBubbles' helper, and [`helper/patches`](helper/patches)
applies imbridge's changes in order:

1. Any-emoji tapbacks, per-part text styles, and a typing-indicator crash fix
2. Skip chat items without `-index` (the macOS 26+ crash when replying or reacting while someone is typing)
3. A build fix for current clang
4. imbridge's transport: its own port, the token, line-delimited requests, and fast reconnects

`helper/build.sh` builds it with only the Xcode Command Line Tools. Released wheels include the built helper.

## Development

```bash
git clone https://github.com/christianblandford/imbridge && cd imbridge
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"   # also builds the helper
.venv/bin/pytest && .venv/bin/ruff check .
```

Releases are cut by pushing a version tag; see [RELEASING.md](RELEASING.md).

## Credits and license

imbridge is Apache-2.0 licensed. The helper is built from
[BlueBubbles' Private API helper](https://github.com/BlueBubblesApp/bluebubbles-helper) (Apache-2.0), and the
any-emoji tapback patch came out of the BlueBubbles setup it replaces. See [NOTICE](NOTICE) for the bundled
third-party code.

imbridge is not affiliated with or endorsed by Apple. iMessage and Messages are trademarks of Apple Inc.
