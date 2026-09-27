"""Contact names: who a phone number or email is, from the Mac's Contacts. Read-only.

Contacts keeps each account's cards (iCloud, Google, On My Mac) in a SQLite database of its own,
~/Library/Application Support/AddressBook/Sources/<id>/AddressBook-v22.abcddb, plus one at the top of that folder.
Reading them takes the same Full Disk Access as chat.db. Numbers match cards the way imbridge matches numbers
everywhere (same_address): every digit when both have a country code, else the last ten. A number on cards with
different names (a shared landline) gets no name rather than a guess.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

from . import config
from .addresses import display_address, same_address

DATABASE = "AddressBook-v22.abcddb"
REFRESH = 30.0  # seconds between looks at whether Contacts changed
COMPANY = 1  # ZDISPLAYFLAGS: the card is a company's, and its name is the organization


class Contacts:
    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root is not None else config.ADDRESS_BOOK
        self._phones: dict[str, list[tuple[str, str]]] = {}  # last ten digits -> (number as written, name)
        self._emails: dict[str, set[str]] = {}  # lowercased email -> names
        self._stamp: tuple | None = None
        self._checked = float("-inf")
        self.cards = 0  # how many cards were read

    def name(self, address: str | None) -> str | None:
        """The name on the contact card for a phone number or email, or None: no card, cards with different names,
        or Contacts can't be read."""
        address = display_address(address)
        if not address:
            return None
        self._refresh()
        if "@" in address:
            names = self._emails.get(address, set())
        else:
            digits = re.sub(r"\D", "", address)
            names = {name for number, name in self._phones.get(digits[-10:], ()) if same_address(address, number)}
        return next(iter(names)) if len(names) == 1 else None

    def _refresh(self) -> None:
        now = time.monotonic()
        if now - self._checked < REFRESH:
            return
        self._checked = now
        databases = sorted([self.root / DATABASE, *self.root.glob(f"Sources/*/{DATABASE}")])
        stamp = tuple((path, *(_mtime(path.with_name(path.name + suffix)) for suffix in ("", "-wal")))
                      for path in databases)
        if stamp != self._stamp:
            self._stamp = stamp
            self._load([path for path in databases if path.exists()])

    def _load(self, databases: list[Path]) -> None:
        phones: dict[str, list[tuple[str, str]]] = {}
        emails: dict[str, set[str]] = {}
        cards: set[tuple[Path, int]] = set()
        for path in databases:
            try:
                db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
            except sqlite3.Error:
                continue
            try:
                names = {
                    card: name
                    for card, *parts in db.execute(
                        "SELECT Z_PK, ZFIRSTNAME, ZLASTNAME, ZNICKNAME, ZORGANIZATION, ZDISPLAYFLAGS FROM ZABCDRECORD"
                    )
                    if (name := _display_name(*parts))
                }
                for card, number in db.execute("SELECT ZOWNER, ZFULLNUMBER FROM ZABCDPHONENUMBER"):
                    digits = re.sub(r"\D", "", number or "")
                    if card in names and len(digits) >= 7:
                        phones.setdefault(digits[-10:], []).append((number, names[card]))
                        cards.add((path, card))
                for card, email in db.execute("SELECT ZOWNER, ZADDRESS FROM ZABCDEMAILADDRESS"):
                    if card in names and email and "@" in email:
                        emails.setdefault(email.strip().lower(), set()).add(names[card])
                        cards.add((path, card))
            except sqlite3.Error:
                continue  # locked mid-write, or a layout this reader doesn't know: skip it until it changes
            finally:
                db.close()
        self._phones, self._emails, self.cards = phones, emails, len(cards)


def _display_name(first: str | None, last: str | None, nickname: str | None, organization: str | None,
                  flags: int | None) -> str | None:
    """The name Contacts shows for a card: a company's organization, else first and last name, else what there is."""
    parts = [part.strip() for part in (first, last) if part and part.strip()]
    if (flags or 0) & COMPANY and organization and organization.strip():
        return organization.strip()
    return " ".join(parts) or (nickname or "").strip() or (organization or "").strip() or None


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None
