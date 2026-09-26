"""Where imbridge keeps its state, and the settings it shares with the helper inside Messages."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

APP_SUPPORT = Path.home() / "Library" / "Application Support" / "imbridge"
CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
BASE_PORT = 45700  # the helper's default too; BlueBubbles uses 45670, so the two never collide


def helper_port() -> int:
    """Port the helper dials: IMBRIDGE_PORT, else 45700 for the first user account (uid 501), +1 per extra user."""
    if env := os.environ.get("IMBRIDGE_PORT"):
        return int(env)
    return min(max(BASE_PORT + os.getuid() - 501, 1024), 65535)


def helper_dylib() -> Path:
    """The helper to inject into Messages: IMBRIDGE_HELPER, else the one bundled with this package."""
    if env := os.environ.get("IMBRIDGE_HELPER"):
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent / "helper" / "imbridge-helper.dylib"


def helper_token() -> str:
    """The shared secret the helper requires on every request. Created on first use, readable only by you."""
    path = APP_SUPPORT / "token"
    try:
        if token := path.read_text().strip():
            return token
    except FileNotFoundError:
        pass
    APP_SUPPORT.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(24)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    return token
