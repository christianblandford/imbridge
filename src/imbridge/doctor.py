"""`imbridge doctor`: check everything sending (the helper) and receiving (chat.db) depend on."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config, messages_app
from .addresses import is_phone
from .chatdb import ChatDB, FullDiskAccessError
from .guard import ANY_LINE, read_allowed

PRIVATE_FRAMEWORKS = ("IMCore", "IMSharedUtilities", "IMDPersistence", "IDS", "FMF")
BOOT_ARG = "-arm64e_preview_abi"


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool | None  # None: nothing to fix yet, or couldn't tell
    detail: str
    fix: str = ""


def _run(*cmd: str) -> str:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (done.stdout + done.stderr).strip()


def missing_symbols(dylib: Path) -> list[str] | None:
    """Private symbols the helper imports that this macOS doesn't export (None when dyld_info isn't available).

    dyld aborts on a missing symbol, so injecting such a helper would crash Messages every time it launched.
    """
    if not shutil.which("dyld_info"):
        return None
    wanted: dict[str, list[str]] = {}
    for line in _run("dyld_info", "-imports", str(dylib)).splitlines():
        if (match := re.search(r"\s(\S+)\s+\(from (\w+)\)$", line)) and match.group(2) in PRIVATE_FRAMEWORKS:
            wanted.setdefault(match.group(2), []).append(match.group(1))
    missing = []
    for framework, symbols in wanted.items():
        exports = _run("dyld_info", "-exports", f"/System/Library/PrivateFrameworks/{framework}.framework/{framework}")
        exported = {line.split()[-1] for line in exports.splitlines() if line.strip().startswith("0x")}
        missing += [f"{symbol} ({framework})" for symbol in symbols if symbol not in exported]
    return missing


def run_checks(dylib: Path | None = None) -> list[Check]:
    dylib = dylib or config.helper_dylib()
    checks: list[Check] = []

    version, arch = platform.mac_ver()[0], platform.machine()
    checks.append(
        Check("Apple Silicon Mac", arch == "arm64", f"macOS {version} on {arch}",
              "imbridge's helper is built for Apple Silicon (arm64e) only.")
    )

    sip = _run("csrutil", "status")
    checks.append(
        Check("SIP disabled", "disabled" in sip.lower(), sip or "csrutil gave no answer",
              "Shut down, hold the power button, Options > Utilities > Terminal: `csrutil disable`, then restart.")
    )

    active = BOOT_ARG in _run("sysctl", "-n", "kern.bootargs")
    pending = BOOT_ARG in _run("nvram", "boot-args")
    checks.append(
        Check("arm64e boot-arg", active, "active" if active else ("set, restart to apply" if pending else "not set"),
              f'sudo nvram boot-args="{BOOT_ARG}", then restart.')
    )

    prefs = "/Library/Preferences/com.apple.security.libraryvalidation.plist"
    validation = _run("defaults", "read", prefs, "DisableLibraryValidation")
    checks.append(
        Check("Library validation disabled", validation == "1",
              f"DisableLibraryValidation = {validation if validation in ('0', '1') else 'unset'}",
              f"sudo defaults write {prefs} DisableLibraryValidation -bool true")
    )

    try:
        db = ChatDB()
        count = db.max_rowid()
        checks.append(Check("Full Disk Access (reading chat.db)", True, f"{count:,} messages readable"))
        phones = [address for address in db.my_addresses() if is_phone(address)]
        chosen = os.environ.get("IMBRIDGE_ADDRESS")
        if chosen:
            checks.append(Check("Your phone numbers", True, f"programs use IMBRIDGE_ADDRESS={chosen}"))
        elif len(phones) > 1:
            checks.append(
                Check("Your phone numbers", None, f"messages arrive at several: {', '.join(phones)}",
                      "tell each program which one it is: IMBridge(address=...) or IMBRIDGE_ADDRESS")
            )
        else:
            number = phones[0] if phones else "none yet"
            checks.append(Check("Your phone numbers", True, f"messages arrive at {number}"))
    except FullDiskAccessError:
        checks.append(
            Check("Full Disk Access (reading chat.db)", False, "chat.db can't be opened",
                  "System Settings > Privacy & Security > Full Disk Access: add the app you run Python from.")
        )

    if not dylib.exists():
        checks.append(Check("Helper present", False, f"{dylib} is missing",
                            "Reinstall imbridge, or build it with helper/build.sh."))
    elif "arm64e" not in _run("file", str(dylib)):
        checks.append(
            Check("Helper present", False, "the helper has no arm64e slice", "Rebuild it with helper/build.sh.")
        )
    else:
        missing = missing_symbols(dylib)
        if missing is None:
            checks.append(Check("Helper matches this macOS", None, "couldn't check (this macOS has no dyld_info)"))
        else:
            checks.append(
                Check("Helper matches this macOS", not missing,
                      "every private API it calls exists" if not missing else "missing: " + ", ".join(missing[:5]),
                      "This macOS removed something the helper calls; injecting it would crash Messages. "
                      "Look for an imbridge update.")
            )

    pid = messages_app.messages_pid()
    if pid is None:
        checks.append(Check("Helper loaded in Messages", None, "Messages isn't running", "imbridge start"))
    else:
        loaded = messages_app.helper_loaded(pid, dylib.name)
        checks.append(
            Check("Helper loaded in Messages", True if loaded else None,
                  f"Messages (pid {pid}) {'has' if loaded else 'is running without'} the helper",
                  "" if loaded else "imbridge start")
        )

    allowed = read_allowed()
    if ANY_LINE in allowed:
        checks.append(Check("Chats imbridge may send to", None, "any chat (rate limits still apply)",
                            "imbridge disallow --any"))
    elif allowed:
        checks.append(Check("Chats imbridge may send to", True, f"{len(allowed)} (see `imbridge allowed`)"))
    else:
        checks.append(Check("Chats imbridge may send to", None, "none yet, so imbridge is read-only",
                            "imbridge allow <chat>"))
    return checks
