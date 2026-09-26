# Changelog

## 0.1.0 (unreleased)

First release.

- Send, reply inline, tapback (the six classics or any emoji), typing indicators, read receipts, and message effects,
  through a helper injected into Messages.app.
- Receive new messages, tapbacks and inline replies from `chat.db`, including text that only exists in
  `attributedBody`.
- Chat handles: `im.chat(...)` gives a conversation whose `messages()` only yields that chat, and whose `reply()` and
  `react()` refuse other chats' messages (`WrongChat`). The stream over every chat is `im.all_messages()`.
- Sending is opt-in per chat: imbridge is read-only until a chat is allowed with `imbridge allow` (which needs a
  person at a terminal) or `IMBridge(allow=[...])`. Sends are rate-limited to 10 a minute per chat and 30 in total,
  across every imbridge process on the Mac.
- The `imbridge` command: `doctor`, `start`, `allow`, `disallow`, `allowed`, `send`, `reply`, `react`, `chats`,
  `history`, `watch`.
- The helper is BlueBubbles' Private API helper (pinned commit) plus four patches: any-emoji tapbacks, a fix for the
  macOS 26+ crash when replying or reacting while someone is typing, a clang build fix, and imbridge's transport
  (its own port, a token, line-delimited requests, 1-second reconnects).
