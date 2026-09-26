# Changelog

## 0.3.0 (unreleased)

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
