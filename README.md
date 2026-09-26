# imbridge

**iMessage for Python and AI agents on macOS.** Send messages, reply inline, tapback with *any* emoji, and receive
everything as it arrives, straight through Messages.app. No server to run, no Electron app, no cloud relay.

```python
import asyncio
from imbridge import IMBridge

async def main():
    async with IMBridge() as im:
        chat = im.resolve_chat("+15551234567")  # the one conversation this bot answers in
        async for message in im.messages():  # new messages, tapbacks and inline replies
            if message.chat_guid == chat and message.text and not message.reaction:
                await im.react(message, "👀")  # any emoji, not just the classic six
                await im.reply(message, "on it")  # an inline (threaded) reply

asyncio.run(main())
```

## Why imbridge

- **Nothing to host.** Your agent imports a library. There's no server process, Electron app or web UI. Besides your
  code, the only moving part is a small helper inside Messages, which talks to your process over a token-protected
  localhost socket.
- **Any-emoji tapbacks.** The classic six plus any emoji, as iOS 18 and macOS 15 allow. BlueBubbles' released helper
  and imsg can only send the classic six.
- **Real iMessage features.** Inline replies, tapbacks, message effects, typing indicators and read receipts, done by
  Messages itself rather than by scripting its UI.
- **Current.** Tested on macOS 27. It fixes a macOS 26+ crash in BlueBubbles' helper when it replies or reacts while
  someone is typing.
- **Built for agents.** An async Python API, a JSON-lines `watch` stream, and one-shot CLI commands.

## How it compares

As of September 2026:

| | imbridge | BlueBubbles Server | imsg |
|---|---|---|---|
| What you run | a Python library | an Electron app with a REST/WebSocket server | a Swift CLI with a JSON-RPC mode |
| Send and receive text | ✓ | ✓ | ✓, without disabling SIP |
| Inline replies and targeted tapbacks | ✓ | ✓ (Private API) | ✓ (its helper) |
| Send any-emoji tapbacks | ✓ | not in a release | ✗ (it can read them) |
| Needs SIP disabled | yes | for Private API features | for its helper's features |
| Latest release | new | 1.9.9, May 2025 | 0.15.x, September 2026 |

If you only need plain text in and out and don't want to touch SIP, [imsg](https://github.com/openclaw/imsg) does
that well. imbridge is for when your agent needs the whole conversation from Python: replies, reactions and effects.

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
  [a few patches](https://github.com/christianblandford/imbridge/tree/main/helper/patches), built from source.
- **Receiving** reads `chat.db`, Messages' own database, read-only. Tapbacks, inline replies, attachments and group
  chats come through as structured fields.

## Requirements

- An **Apple Silicon** Mac signed in to iMessage in Messages. Built for macOS 11.5 and later; tested on macOS 27.0.
- **Python 3.10** or later.
- **System Integrity Protection disabled**, plus two related settings (below). macOS allows no other way to load a
  helper into Messages; BlueBubbles' Private API and imsg's helper have the same requirement.

> **Warning:** disabling SIP lowers the security of the whole Mac, not just Messages. If you can, run imbridge on a
> Mac dedicated to it (an old Mac mini is ideal). While SIP is off, iPhone and iPad apps from the App Store won't open.

## Setup

**1. Let macOS load the helper** (once):

1. Shut down, then hold the power button until "Loading startup options" appears. Choose **Options**, then
   **Continue**, and pick your user if asked. Then choose **Utilities > Terminal**, run `csrutil disable`, and restart.
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

The first `imbridge start` may ask to let your terminal control Messages, because imbridge quits and relaunches it to
load the helper. Allow it. To run the latest code instead of a release, use
`pip install git+https://github.com/christianblandford/imbridge`. That builds the helper, so it needs the Xcode
Command Line Tools (`xcode-select --install`).

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
    im.message(guid)                                      # one message, or None
    async for message in im.messages(include_from_me=False):
        ...
```

`send`, `reply` and `react` return the new message's GUID. iMessage keeps one tapback per person per message, so a new
tapback replaces your previous one.

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

**Options.** `IMBridge(inject=True)` loads the helper into Messages whenever none answers; pass `inject=False` if
something else manages Messages. `poll_interval` (default 0.5 seconds) sets how often `messages()` checks for new
rows. Receiving (`messages`, `history`, `chats`, `message`) only reads `chat.db`, so it works without the helper.

**Errors.** `ChatNotFound` means no existing conversation matches. `HelperError` means the helper refused or failed;
its subclasses are `HelperNotConnected` and `HelperUnauthorized`. `FullDiskAccessError` means `chat.db` can't be read.

**Effects:** `slam`, `loud`, `gentle`, `invisible_ink`, `echo`, `spotlight`, `balloons`, `confetti`, `love`, `lasers`,
`fireworks`, `celebration`, `shooting_star`.

**Environment variables:** `IMBRIDGE_PORT` sets the port the helper dials (default 45700 for the first user account on
the Mac, one higher for each further account). `IMBRIDGE_HELPER` loads a helper dylib other than the bundled one.

## Command line

```bash
imbridge doctor                        # check this Mac's setup
imbridge start                         # load the helper into Messages
imbridge send +15551234567 "hello" [--reply-to GUID] [--effect confetti]
imbridge reply GUID "inline reply"
imbridge react GUID 🔥 [--remove]
imbridge chats [-n 20] [--json]
imbridge history +15551234567 [-n 20] [--json]
imbridge watch [--json] [--from-me]    # stream new messages, tapbacks and replies; --json prints one object per line
```

`send`, `reply` and `react` print the new message's GUID.

## Using it with an AI agent

- **As a library**, inside your agent's loop, like the example at the top.
  [examples/ping_bot.py](https://github.com/christianblandford/imbridge/blob/main/examples/ping_bot.py) is a runnable
  bot that answers only in the chat you name.
- **From the shell**, for any agent that can run commands: read new messages from `imbridge watch --json` and answer
  with `imbridge reply GUID "..."` or `imbridge react GUID 👍`.

## Limitations

- Apple Silicon only; the helper is built for arm64e.
- Only existing conversations. Start a chat in Messages first; starting new ones isn't supported yet.
- The helper can also send attachments, edit, unsend and manage groups, but the Python API doesn't wrap those yet.
- Messages restarts, hidden, whenever imbridge loads the helper, which closes its windows.
- Receiving checks `chat.db` every half second (see `poll_interval`), so new messages can take up to that long to
  arrive.
- SMS goes through your iPhone, so Text Message Forwarding must be on.
- The helper uses private Apple APIs, which a macOS update can change. `imbridge doctor` checks them before loading
  the helper.

## Troubleshooting

- Run **`imbridge doctor`** first. Before anything is loaded, it checks SIP, the boot-arg, library validation, Full
  Disk Access, and that every private API the helper calls still exists on your macOS. That last check matters after
  macOS updates: a helper that calls something Apple removed would crash Messages on launch.
- **Helper logs:** `log stream --predicate 'subsystem == "imbridge"'`
- **Messages disappears when the helper loads.** It restarts hidden; open it from the Dock as usual.
- **The helper won't stay loaded.** Quit BlueBubbles Server if it's installed: it relaunches Messages with its own
  helper.
- **Testing in a chat with yourself.** Everything you send also comes back as received, and Messages replaces your own
  tapback rows with the ones that come back.

## Security

- The helper can do anything Messages can, including send as you. It only takes requests from your Mac: imbridge
  listens on `127.0.0.1`/`::1`, and each request must carry a token that imbridge generates (stored in
  `~/Library/Application Support/imbridge/token`, readable only by you) and passes to Messages when it launches it.
- The bigger trade-off is SIP; see the warning above. To report a vulnerability, see
  [SECURITY.md](https://github.com/christianblandford/imbridge/blob/main/SECURITY.md).

## How the helper is built

[`helper/UPSTREAM`](https://github.com/christianblandford/imbridge/blob/main/helper/UPSTREAM) pins a commit of
BlueBubbles' helper, and [`helper/patches`](https://github.com/christianblandford/imbridge/tree/main/helper/patches)
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

Releases are cut by pushing a version tag; see
[RELEASING.md](https://github.com/christianblandford/imbridge/blob/main/RELEASING.md).

## Credits and license

imbridge is licensed under Apache-2.0. Its helper is built from
[BlueBubbles' Private API helper](https://github.com/BlueBubblesApp/bluebubbles-helper), also Apache-2.0; imbridge's
any-emoji tapbacks started as a patch to it. [NOTICE](https://github.com/christianblandford/imbridge/blob/main/NOTICE)
lists the bundled third-party code.

imbridge is not affiliated with or endorsed by Apple. iMessage and Messages are trademarks of Apple Inc.
