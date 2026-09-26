# imbridge

**iMessage for Python and AI agents on macOS.** Send messages, reply inline, tapback with *any* emoji, and receive
everything as it arrives, straight through Messages.app. No server to run, no Electron app, no cloud relay, and no
way for a bug to message your contacts.

```python
import asyncio
from imbridge import IMBridge

ALEX = "+15551234567"  # the one conversation this bot works in

async def main():
    async with IMBridge(allow=[ALEX]) as im:  # imbridge sends nowhere else
        chat = im.chat(ALEX)
        async for message in chat.messages():  # only this chat's new messages, tapbacks and replies
            if message.text and not message.reaction:
                await chat.react(message, "👀")  # any emoji, not just the classic six
                await chat.reply(message, "on it")  # an inline (threaded) reply

asyncio.run(main())
```

## Why imbridge

- **Nothing to host.** Your agent imports a library. There's no server process, Electron app or web UI. Besides your
  code, the only moving part is a small helper inside Messages, which talks to your process over a token-protected
  localhost socket.
- **It can't spam your contacts.** imbridge sends nowhere until you allow a chat, either in your code or with
  `imbridge allow`, which only a person at a terminal can run. A chat handle refuses messages from other chats, rate
  limits stop runaway loops, and with two numbers on one Apple ID it only answers on the one you choose.
- **Any-emoji tapbacks.** The classic six plus any emoji, as iOS 18 and macOS 15 allow. BlueBubbles' released helper
  and imsg can only send the classic six.
- **Real iMessage features.** Inline replies, tapbacks, message effects, typing indicators and read receipts, done by
  Messages itself rather than by scripting its UI.
- **Current.** Tested on macOS 27. It fixes a macOS 26+ crash in BlueBubbles' helper when it replies or reacts while
  someone is typing.

## How it compares

As of September 2026:

| | imbridge | BlueBubbles Server | imsg |
|---|---|---|---|
| What you run | a Python library | an Electron app with a REST/WebSocket server | a Swift CLI with a JSON-RPC mode |
| Send and receive text | ✓ | ✓ | ✓, without disabling SIP |
| Inline replies and targeted tapbacks | ✓ | ✓ (Private API) | ✓ (its helper) |
| Send any-emoji tapbacks | ✓ | not in a release | ✗ (it can read them) |
| Sends only to chats you allowed | ✓ | ✗ | ✗ |
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

**4. Decide where imbridge may send.** Until you do, it only reads. If your Apple ID has more than one phone number
(two iPhones, say), also tell each program which number it is with `IMBridge(address="+15550002222")` or
`IMBRIDGE_ADDRESS`; see [Which of your numbers it answers on](#which-of-your-numbers-it-answers-on). List the chats in
your code with `IMBridge(allow=["+15551234567", "Family"])`, or allow them once for every program on this Mac,
including the CLI:

```bash
imbridge chats                   # find the chat: a phone number, an email, a group's name, or its GUID
imbridge allow +15551234567      # asks you to confirm
imbridge allowed                 # what's allowed; `imbridge disallow <chat>` takes one back
```

## Who imbridge may message

imbridge is built so that a bug in your code, or an agent getting creative, can't message people you didn't choose:

- **The allowlist.** Messages, replies, tapbacks, typing indicators and read receipts only go to allowed chats;
  anything else raises `SendNotAllowed` before Messages is touched. A chat can be allowed two ways, and both are
  deliberate:
  - **In your code:** `IMBridge(allow=["+15551234567", "Family"])` takes phone numbers, emails, group names and chat
    GUIDs. `start()` checks that each one matches exactly one chat, so a typo fails immediately. A running program
    can't add chats; there's no `allow()` to call from a loop.
  - **For every program on the Mac:** `imbridge allow <chat>` asks for confirmation and refuses to run without a
    terminal, so an agent driving the CLI can't add chats itself.

  Allowing every chat takes a deliberate `allow=ANY_CHAT` in code, or `imbridge allow --any`.
- **Chat handles.** `chat.messages()` yields only that chat's messages, and `chat.reply()` and `chat.react()` raise
  `WrongChat` for a message from any other chat.
- **Rate limits.** At most 10 messages and tapbacks a minute to one chat and 30 in total, counted across every imbridge
  process on the Mac, or it raises `RateLimited`. You can change them with `IMBridge(max_per_chat=..., max_total=...)`.
- **Your own messages** are left out of every stream unless you ask for them (`include_from_me=True`), so a bot doesn't
  answer itself.

### Which of your numbers it answers on

A Mac signed in to your Apple ID receives messages sent to every address on it. With two iPhones on one Apple ID,
texts to your personal number and texts to your bot's number arrive in the same place, and even in the same chat when
one person texts both. So tell each program which address it is:

```python
async with IMBridge(address="+15550002222", allow=["+15551234567"]) as im:   # or IMBRIDGE_ADDRESS=+15550002222
    ...
```

- **It only sees messages sent to that address.** Streams and history leave everything else out.
- **It only sends from that address.** Messages replies from whatever address a conversation is on, so imbridge
  refuses (`WrongAddress`) to send in a chat that's on another address. It also refuses to reply or react to a message
  that was sent to another address.
- **Without an address,** imbridge refuses to read or send (`AddressNotChosen`) while this Mac has recent messages at
  more than one of your phone numbers. If a second number turns up while a program is running, the stream stops with
  the same error instead of passing that message on. Use `address=ANY_ADDRESS` to deliberately take every number.

`imbridge doctor` lists the numbers in use, every `Message` and chat has an `address`, and the CLI takes `--address`.

## Python API

```python
async with IMBridge(allow=["+15551234567"]) as im:
    chat = im.chat("+15551234567")                        # or an email, a group's name, or a chat GUID
    guid = await chat.send("hello", effect="confetti")    # returns the new message's GUID
    await chat.reply(guid, "replying inline")             # a Message or a message GUID
    await chat.react(guid, "love")                        # love, like, dislike, laugh, emphasize, question
    await chat.react(guid, "🔥")                          # ...or any emoji
    await chat.react(guid, "🔥", remove=True)
    await chat.typing()                                   # typing indicator on; chat.typing(False) turns it off
    await chat.mark_read()
    chat.history(50)                                      # latest messages, oldest first
    async for message in chat.messages(include_from_me=False):
        ...

    im.chats(20)                                          # recent chats, newest first; each has .can_send
    im.message(guid)                                      # one message, or None
    async for message in im.all_messages():               # every chat: be deliberate about who you answer
        ...
```

A group's name only works when no other chat shares it; otherwise imbridge refuses and lists the candidates' GUIDs
(`imbridge chats` shows which is which).

A chat has `guid`, `name`, `is_group`, `participants`, `last_message_at`, `address` (which of your addresses it's
on) and `can_send`. `IMBridge` also has `send`,
`reply`, `react`, `typing` and `mark_read` that take a chat or message GUID directly; the same allowlist and limits
apply. iMessage keeps one tapback per person per message, so a new tapback replaces your previous one.

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
| `address` | which of your addresses it was sent to (or, for your own messages, sent from) |

**Options.** `IMBridge(allow=..., address=..., max_per_chat=10, max_total=30)` are covered above. `inject=True` loads the helper
into Messages whenever none answers; pass `inject=False` if something else manages Messages. `poll_interval` (default
0.5 seconds) sets how often streams check for new messages. Reading (streams, `history`, `chats`, `message`) only
touches `chat.db`, so it works without the helper.

**Errors.** `SendNotAllowed` means the chat isn't allowed. `RateLimited` means a limit was hit. `AddressNotChosen`
means this Mac gets messages at several of your phone numbers and the program hasn't said which it is. `WrongAddress`
means a chat or message is on another of your addresses. `WrongChat` means a
chat was handed another chat's message. `ChatNotFound` means no existing conversation matches. `HelperError` means the
helper refused or failed; its subclasses are `HelperNotConnected` and `HelperUnauthorized`. `FullDiskAccessError`
means `chat.db` can't be read.

**Effects:** `slam`, `loud`, `gentle`, `invisible_ink`, `echo`, `spotlight`, `balloons`, `confetti`, `love`, `lasers`,
`fireworks`, `celebration`, `shooting_star`.

**Environment variables:** `IMBRIDGE_ADDRESS` is the default `address` (`any` for `ANY_ADDRESS`). `IMBRIDGE_PORT` sets the port the helper dials (default 45700 for the first user account on
the Mac, one higher for each further account). `IMBRIDGE_HELPER` loads a helper dylib other than the bundled one.

## Command line

```bash
imbridge doctor                        # check this Mac's setup
imbridge start                         # load the helper into Messages
imbridge allow CHAT | --any            # let imbridge send to a chat (asks you to confirm)
imbridge disallow CHAT | --any
imbridge allowed
imbridge send +15551234567 "hello" [--reply-to GUID] [--effect confetti]
imbridge reply GUID "inline reply"
imbridge react GUID 🔥 [--remove]
imbridge chats [-n 20] [--json]
imbridge history +15551234567 [-n 20] [--json]
imbridge watch [--chat CHAT] [--json] [--from-me]    # stream new messages; --json prints one object per line
imbridge <command> --address +15550002222           # start, send, reply, react, history and watch take --address
```

`send`, `reply` and `react` print the new message's GUID. They follow the same allowlist and rate limits as the
library.

## Using it with an AI agent

- **As a library**, inside your agent's loop, like the example at the top.
  [examples/ping_bot.py](https://github.com/christianblandford/imbridge/blob/main/examples/ping_bot.py) is a runnable
  bot for one chat.
- **From the shell**, for any agent that can run commands: it reads new messages from
  `imbridge watch --chat CHAT --json` and answers with `imbridge reply GUID "..."` or `imbridge react GUID 👍`. Allow
  the chats it may use beforehand; it can't allow more on its own.

## Limitations

- Apple Silicon only; the helper is built for arm64e.
- Only existing conversations. Start a chat in Messages first; starting new ones isn't supported yet.
- The helper can also send attachments, edit, unsend and manage groups, but the Python API doesn't wrap those yet.
- Messages restarts, hidden, whenever imbridge loads the helper, which closes its windows.
- Streams check `chat.db` every half second (see `poll_interval`), so new messages can take up to that long to arrive.
- SMS goes through your iPhone, so Text Message Forwarding must be on.
- The helper uses private Apple APIs, which a macOS update can change. `imbridge doctor` checks them before loading
  the helper.

## Troubleshooting

- Run **`imbridge doctor`** first. Before anything is loaded, it checks SIP, the boot-arg, library validation, Full
  Disk Access, and that every private API the helper calls still exists on your macOS. That last check matters after
  macOS updates: a helper that calls something Apple removed would crash Messages on launch.
- **`SendNotAllowed`:** allow the chat in your code (`IMBridge(allow=[...])`), or with `imbridge allow` in your own
  terminal.
- **Helper logs:** `log stream --predicate 'subsystem == "imbridge"'`
- **Messages disappears when the helper loads.** It restarts hidden; open it from the Dock as usual.
- **The helper won't stay loaded.** Quit BlueBubbles Server if it's installed: it relaunches Messages with its own
  helper.
- **`AddressNotChosen` or `WrongAddress`:** see
  [Which of your numbers it answers on](#which-of-your-numbers-it-answers-on). `imbridge doctor` lists the numbers
  in use.
- **Testing in a chat with yourself.** Everything you send also comes back as received, and Messages replaces your own
  tapback rows with the ones that come back.

## Security

- The helper can do anything Messages can, including send as you. It only takes requests from your Mac: imbridge
  listens on `127.0.0.1`/`::1`, and each request must carry a token that imbridge generates (stored in
  `~/Library/Application Support/imbridge/token`, readable only by you) and passes to Messages when it launches it.
- Everything the other side can see is limited to allowed chats and rate-limited, as described above.
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
