# Changelog

## 0.6.0 (2026-09-27)

- Incoming typing: `im.is_typing(chat)`, `await im.wait_while_typing(chat, timeout=30)` (so a bot doesn't answer
  half a thought) and `im.typing_changes()` follow the other person's typing bubble from the helper's notices, once
  per change (Messages repeats "not typing" whenever it redraws its list), letting a bubble go after a minute of
  silence as Messages does. `imbridge watch --typing` and the MCP tool `typing_status` show it. Best effort on macOS
  26 and later: there the helper only sees the bubbles Messages draws in its conversation list, and live tests on
  macOS 27 with Messages hidden saw none.
- Contact names: `Message.sender_name` and `Chat.names` come from your Contacts (read-only, under the same Full Disk
  Access as chat.db), matched with the same strict rules as everywhere else in imbridge; a number on cards with
  different names gets none. `im.contact_name(address)` looks one up, and `im.chats(query=...)` (`imbridge chats
  --query`, the MCP `list_chats` `query`) finds chats by name, number, email or contact name. The MCP server adds
  `from_name` to messages and `names` to chats; the CLI shows names. `IMBridge(contacts=False)` turns it off, and
  `imbridge doctor` says how many contacts it can read.
- Search: `im.search(query, chat=None, limit=20, before=None)` and `chat.search(query)` find messages containing
  some text (ignoring case), newest first, in one chat or all, including text Messages keeps only in the attributed
  body. `imbridge search` and the MCP tool `search_messages` do the same. `before` pages on.
- Paging: `history()` takes `before` (a message or GUID: the messages just before it) and `after` (the ones just
  after it); so do `imbridge history` (`--before`, `--after`) and the MCP tool `read_messages`, and `list_chats`
  takes an `offset`. An unknown GUID raises `MessageNotFound`.
- Link previews: `chat.send_link(url, guid=None)`, `imbridge send-link` and the MCP tool `send_link` send a link
  with its preview card (title, summary, pictures), the way Messages sends a pasted link
  (helper/patches/0012-link-previews.patch). Messages loads the page with LinkPresentation and archives it with
  LinkPresentation's own Messages payload; the pictures go as hidden transfers, as Messages sends them. Previews are
  for the public internet only: a link whose host is on this Mac or a private network raises `ValueError` before
  Messages loads anything, and a preview that names such a host (a redirect, a picture) is never sent. A page with no
  preview, or one that takes over 15 seconds, goes as a plain link. Verified live: the message matches what Messages
  sends, and the recipient saw the preview.
- Link previews, read: a link sent with a preview arrives with `Message.link` (a `LinkPreview`: url, title, summary,
  site name, original url), read from the archived RichLink Messages stores with it.
- `send_poll()` takes a `guid` you choose, like `send()` (helper/patches/0013-poll-guids.patch). The question sent
  after the poll gets a GUID made from it, `question_guid(guid)`, so resending the pair with the same `guid` delivers
  neither twice. Verified live: a retry with the same `guid` left one poll and one question.
- Upgrading reloads the helper: Messages kept running the helper it was started with, and an older one ignores
  requests it doesn't know, so a new feature timed out until Messages restarted. The helper now reports its build
  when it connects (helper/patches/0014-helper-build.patch; `helper/build.sh` names it from the patches), and
  imbridge reloads Messages once when that isn't the build it ships (with `inject=False` it logs a warning instead).
  `imbridge start` now restarts Messages only when it has to.
- `im.supports(feature)` says whether this Mac's macOS has a feature (`FEATURES`: polls need macOS 26, Send Later
  15, and so on). Polls and Send Later raise `Unsupported` on an older macOS before anything is sent, instead of going
  ahead on a Mac without the feature (Messages before macOS 26 shows only a poll's "Sent a poll" text). The MCP
  server leaves out the tools this Mac can't use.
- `Message.attachments` leaves out attachments Messages hides: the pictures behind a balloon, like a link preview's or
  an iMessage app's, which aren't anything the sender attached.

## 0.5.0 (2026-09-26)

- Retrying safely: `send()`, `reply()` and `send_file()` (and a new conversation's first message) take a `guid` you
  choose, a UUID, and the message is created with it (helper/patches/0011-chosen-guids.patch). Record it before
  sending: after a timeout or crash, `im.message(guid)` says whether it went out, and a retry with the same `guid`
  isn't delivered twice. Verified live: the chosen GUID lands in chat.db, and a retry reached the recipient once.
- Location pins: a pin someone sends arrives with `Message.location` (a `Location`: latitude, longitude, name,
  address, Maps url), read from its vCard. `chat.send_location(latitude, longitude, name=None)`, `imbridge
  send-location` and the MCP tool `send_location` send a pin for the coordinates given, as Messages sends a place from
  Maps; nothing ever shares where the Mac is.
- Stickers, sent: `chat.send_sticker(path, on=None, label=None)` (and `imbridge send-sticker`) sends an image as a
  sticker, marked the way Messages marks the stickers you make, on its own or stuck onto one of the chat's messages
  (helper/patches/0009-stickers.patch).
- Sticker tapbacks, sent: `chat.react_with_sticker(message, path)` (and `imbridge react-sticker`) tapbacks a message
  with a sticker, sent by IMCore's own IMTapbackSender as an IMStickerTapback (helper/patches/0010-sticker-tapbacks.patch).
- Stickers, read: `Attachment.is_sticker` marks sticker images (sent on their own, stuck onto a message, or used as a
  tapback), and a sticker used as a tapback (associated types 2007/3007) is a `Reaction` of kind `sticker_tapback`
  instead of `unknown`. A sticker stuck onto a bubble stays kind `sticker`.

## 0.4.1 (2026-09-26)

- **Fix:** `cancel_scheduled()` could report a Send Later message cancelled, and its row disappear, while the message
  still went out at its time: a cancel sent while the message is still on its way to Apple's servers
  (`schedule_state` 1) is lost. It now waits until the servers hold the message (state 2), which takes well under a
  second, and raises `SendLaterFailed` rather than cancel too early. Verified live: cancelled on its way, the message
  was delivered; cancelled the moment it was held, it stayed cancelled.

## 0.4.0 (2026-09-26)

- Send Later: `chat.send_later(text, at)` schedules a message with Messages' own Send Later, to the minute and up to
  14 days ahead; it goes out even if nothing is running then. `chat.scheduled()` lists what's waiting (each with
  `Message.scheduled_for`) and `chat.cancel_scheduled(message)` takes one back. Scheduled messages go out through
  IMCore's chat registry, as Messages' own sends do: `-[IMChat sendMessage:]` relabels a chat as your own address
  after a scheduled message, so the next message sent into it, scheduled or not, lands in your own conversation
  (sometimes creating a broken "e:address" conversation). imbridge refuses chats with yourself (where your own devices
  get a scheduled message at once) and reads each message back to check Messages held it in the right chat, raising
  `SendLaterFailed` otherwise. Also `imbridge send-later`/`scheduled`/`cancel` and the MCP tools `send_later`,
  `list_scheduled` and `cancel_scheduled` (helper/patches/0007-send-later.patch, 0008-registry-dispatch.patch).
- Focus status: `im.focus_status(person)` (and `chat.focus_status()`, `imbridge focus`, the MCP tool `focus_status`)
  says whether someone has notifications silenced, or `None` when they don't share it. The helper's lookup had been
  failing on macOS 26.4 and later, where the method it called was renamed (helper/patches/0006-focus-status.patch).
- Polls, read: a poll arrives as a message with `poll` (its options, creator and session) and each vote as one with
  `vote` (the voter's whole current choice). `im.poll(message)` and `chat.poll(message)` give a `PollResults`: current
  options including added choices, each voter's latest choice, `counts()`, `voters(option)`, and the question the
  creator sent with it. `imbridge poll GUID` and the MCP server's `read_poll` show the same.
- The plain "Sent a poll" Messages sends with every poll, for devices without polls, is left out of streams and history
  as Messages leaves it out of the conversation.
- Polls, sent: `chat.send_poll(options, question=...)` sends a poll and then its question as a message, as Messages
  does. `chat.vote(poll, *options)` and `chat.unvote(poll, *options)` vote the way tapping does in Messages: each vote
  carries your whole current choice under the poll's own session, so other choices stay (sending only the new one, or
  a fresh session, is what makes other tools' votes replace choices or go uncounted). Votes that change nothing aren't
  sent. `imbridge send-poll`, `imbridge vote`, and the MCP tools `send_poll` and `vote` do the same, all through the
  allowlist, address and rate-limit checks.
- The helper gains `send-poll` and `send-poll-vote` (helper/patches/0005-polls.patch).
- Formatting and mentions, sent: `send()` and `reply()` take a list of strings and `Span`s for bold, italic,
  underline, strikethrough, animated text effects (`TEXT_EFFECTS`, by the names in Messages' Text Effects menu) and
  @mentions. Unknown effects, which Messages would silently send as plain text, and mentions of people outside the
  chat are refused before anything is sent. `imbridge send`/`reply --text-effect` and the MCP tools' `text_effect`
  animate a whole message.

## 0.3.1 (2026-09-26)

- **Fix (safety):** a phone number no longer resolves to a conversation whose identifier merely contains its digits.
  `send("+15551234567", ...)`, `allow=[...]` and `imbridge allow` matched any one-to-one chat whose identifier
  ended with those 10 digits, including email addresses such as carrier MMS or bounce addresses
  (`...-5551234567=mms.att.net@...`), and the allowlist and address checks then approved that chat. Phone numbers now
  match only chats that are that phone number, every digit compared when both carry a country code.
- `imbridge doctor` reports a broken conversation addressed to an internal form of your address (`e:you@...`), which
  IMCore can create when it misfiles a message and then use as your note-to-self chat.

## 0.3.0 (2026-09-26)

- Edits and unsends, read: `Message.edited_at`, `Message.edit_count` and `Message.unsent_at`, and `im.message(guid)`
  returns the current text. On macOS 26+ an unsend leaves `date_retracted` at 0 and is marked only in
  `message_summary_info`; both are read.
- `im.changes()` and `chat.changes()`: messages as they're edited or unsent, including ones that arrive and change
  between two reads.
- Edits and unsends, sent: `chat.edit(message, text)` and `chat.unsend(message)` (and `im.edit`/`im.unsend`) for your
  own messages, through the same allowlist, address and rate-limit checks as sends. Past iMessage's limits they raise
  `EditLimit` with `kind` `edit_window` (15 minutes), `edit_count` (5 edits) or `unsend_window` (2 minutes). The MCP
  server gains `edit_message` and `unsend_message`.
- `Chat.participants` leaves out the address the program runs as, and keeps your other addresses.
- Files: `chat.send_file(path, reply_to=...)`, `im.send_file(chat, path)` and `imbridge send-file` send photos, GIFs,
  videos and documents, optionally as inline replies. Messages is sandboxed, so the file is first copied into
  `~/Library/Messages/Attachments/imbridge/`, and that copy becomes the attachment.
- New conversations: `im.send()` to an allowed phone number (with its country code) or email you have no conversation
  with starts one, over iMessage if they have it and SMS otherwise. Allow someone new with
  `allow=[NewContact("+15557654321")]` or `imbridge allow`, which asks you to confirm. Other `allow` entries must still
  match a chat, so a typo fails at start, and local numbers are refused, so a missing country code can't reach a
  stranger.
- Messages can't be told which address a new conversation goes out from: iMessage uses its "Start new conversations
  from" setting, and SMS the iPhone forwarding texts to this Mac. With `address=` set, imbridge refuses
  (`WrongAddress`) to start one that would go out from another address; with none set, it refuses
  (`AddressNotChosen`) to start one from a second phone number of yours.
- Group changes: with `include_events=True`, streams, `new_messages()` and `history()` also return the rows recording
  people added, removed or leaving and renames, as messages with `event` set (a `GroupEvent`: `kind`, `person`, `name`,
  `code`). `imbridge watch --events` prints them. Events missing an address take their chat's.
- `Message.mentions`: the phone numbers and emails a message @mentions, and `im.mentions_me(message)` to check for the
  program's own address.

## 0.2.0 (2026-09-26)

- An MCP server: `imbridge mcp` (install `imbridge[mcp]`) gives Claude Code, Claude Desktop, Cursor and other MCP
  clients `list_chats`, `read_messages`, `check_messages` (with `wait_seconds` to wait for a reply), `send_message`,
  `reply`, `react`, `show_typing` and `whoami`. Sends go through the same allowlist, address rules and rate limits,
  and refusals are worded so a model asks the user instead of working around them. Its first send loads the helper
  into Messages, only after the checks pass.
- `IMBridge.new_messages(since=..., wait=...)`: what arrived after a chat.db ROWID, waiting up to `wait` seconds, for
  request/response code.
- In a chat with yourself, the received copies of what imbridge just sent are dropped from streams, so an agent never
  answers itself.
- With `address=` set, `chats()` lists only the chats on that address.
- Concurrent sends can't relaunch Messages twice, and nothing imbridge runs prints to stdout.

## 0.1.0 (2026-09-26)

First release.

- Send, reply inline, tapback (the six classics or any emoji), typing indicators, read receipts, and message effects,
  through a helper injected into Messages.app.
- Receive new messages, tapbacks and inline replies from `chat.db`, including text that only exists in
  `attributedBody`.
- Chat handles: `im.chat(...)` gives a conversation whose `messages()` only yields that chat, and whose `reply()` and
  `react()` refuse other chats' messages (`WrongChat`). The stream over every chat is `im.all_messages()`.
- Sending is opt-in per chat: imbridge is read-only until a chat is allowed, inline in code with
  `IMBridge(allow=[...])` (phone numbers, emails, group names or GUIDs, checked at `start()`) or with `imbridge allow`
  (which needs a person at a terminal). Sends are rate-limited to 10 a minute per chat and 30 in total, across every
  imbridge process on the Mac.
- Chats can be named by a group's display name wherever a chat is expected; a name several chats share is refused.
- `IMBridge(address=...)` (or `IMBRIDGE_ADDRESS`, or `--address`) says which of your own addresses a program is, for
  Apple IDs with several numbers: it only sees messages sent to that address and won't send in chats on another
  (`WrongAddress`). With no address set and messages arriving at several of your phone numbers, imbridge refuses
  to read or send (`AddressNotChosen`), including when a second number first appears mid-stream.
- The `imbridge` command: `doctor`, `start`, `allow`, `disallow`, `allowed`, `send`, `reply`, `react`, `chats`,
  `history`, `watch`.
- The helper is BlueBubbles' Private API helper (pinned commit) plus four patches: any-emoji tapbacks, a fix for the
  macOS 26+ crash when replying or reacting while someone is typing, a clang build fix, and imbridge's transport
  (its own port, a token, line-delimited requests, 1-second reconnects).
