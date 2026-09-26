# Changelog

## 0.1.0 (unreleased)

First release.

- Send, reply inline, tapback (the six classics or any emoji), typing indicators, read receipts, and message effects,
  through a helper injected into Messages.app.
- Receive new messages, tapbacks and inline replies from `chat.db`, including text that only exists in
  `attributedBody`.
- The `imbridge` command: `doctor`, `start`, `send`, `reply`, `react`, `chats`, `history`, `watch`.
- The helper is BlueBubbles' Private API helper (pinned commit) plus four patches: any-emoji tapbacks, a fix for the
  macOS 26+ crash when replying or reacting while someone is typing, a clang build fix, and imbridge's transport
  (own port, token, line-delimited requests, 1-second reconnects).
