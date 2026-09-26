# Changelog

## 0.4.1 (unreleased)

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
