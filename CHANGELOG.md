# Changelog

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
