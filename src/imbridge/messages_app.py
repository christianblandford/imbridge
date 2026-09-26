"""Start and stop Messages.app with the helper injected.

Injection needs SIP disabled, library validation disabled, and the -arm64e_preview_abi boot-arg; without them dyld
silently ignores DYLD_INSERT_LIBRARIES and Messages starts without the helper.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from pathlib import Path

MESSAGES_APP = "/System/Applications/Messages.app"


def messages_pid() -> int | None:
    pids = subprocess.run(["pgrep", "-x", "Messages"], capture_output=True, text=True).stdout.split()
    return int(pids[0]) if pids else None


def _wait(condition: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.2)
    return True


def quit_messages(timeout: float = 10.0) -> None:
    if messages_pid() is None:
        return
    subprocess.run(["osascript", "-e", 'tell application "Messages" to quit'], capture_output=True)
    if _wait(lambda: messages_pid() is None, timeout):
        return
    subprocess.run(["pkill", "-x", "Messages"], capture_output=True)
    if not _wait(lambda: messages_pid() is None, 3):
        subprocess.run(["pkill", "-KILL", "-x", "Messages"], capture_output=True)
        _wait(lambda: messages_pid() is None, 3)


def launch_with_helper(
    dylib: Path, env: dict[str, str] | None = None, hidden: bool = True, timeout: float = 15.0
) -> int:
    """(Re)launch Messages through LaunchServices with the helper injected; returns its pid."""
    quit_messages()
    args = ["open", "-g", "-a", MESSAGES_APP, "--env", f"DYLD_INSERT_LIBRARIES={dylib}"]
    for key, value in (env or {}).items():
        args += ["--env", f"{key}={value}"]
    if hidden:
        args.insert(1, "-j")
    subprocess.run(args, check=True, capture_output=True)  # never print: stdout may be an MCP stream
    if not _wait(lambda: messages_pid() is not None, timeout):
        raise RuntimeError("Messages did not start")
    return messages_pid()


def helper_loaded(pid: int, dylib_name: str = "imbridge-helper.dylib") -> bool:
    """True if the process has the helper mapped."""
    files = subprocess.run(["lsof", "-p", str(pid), "-Fn"], capture_output=True, text=True).stdout
    return dylib_name in files
