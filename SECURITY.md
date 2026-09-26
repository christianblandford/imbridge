# Security

imbridge lets a program send iMessages as you, so security reports matter.

## Reporting a vulnerability

Report it privately through GitHub: on the repository's **Security** tab, choose **Report a vulnerability**. Please
don't open a public issue.

## What imbridge protects, and what it can't

- The helper inside Messages only acts on requests that carry the token imbridge generated for your account and passed
  to Messages when it launched it. The token is stored in `~/Library/Application Support/imbridge/token`, readable
  only by you. Other users of the Mac can't drive Messages through the helper, and neither can apps that can't read
  your files.
- Software running as you can read that token, just as it can already read your files. imbridge doesn't defend
  against malware running under your account.
- Using imbridge requires disabling System Integrity Protection, which weakens the whole Mac. The README explains
  the trade-off.
