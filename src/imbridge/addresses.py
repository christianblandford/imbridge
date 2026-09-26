"""Which of your own iMessage addresses a message or chat is on.

An Apple ID can have several addresses (phone numbers and emails), and a Mac signed in to it receives messages sent to
all of them. Two iPhones on one Apple ID means two phone numbers arriving in the same chat.db, so a program meant to
answer on one number would otherwise also see (and answer) texts sent to the other. chat.db records the address on
each message (destination_caller_id: the one it was sent to, or sent from for your own) and on each chat
(last_addressed_handle), in varying formats: "+14805550100", "14805550100", "mailto:you@example.com".
"""

from __future__ import annotations

import re


class AnyAddress:
    def __repr__(self) -> str:
        return "ANY_ADDRESS"


ANY_ADDRESS = AnyAddress()
"""IMBridge(address=ANY_ADDRESS) deliberately works with messages to every one of your addresses."""


class AddressNotChosen(RuntimeError):
    """Messages here arrive at more than one of your phone numbers, and this program hasn't said which one it is."""


class WrongAddress(PermissionError):
    """The chat or message is on a different one of your addresses than the one this program uses."""


_PREFIXES = ("mailto:", "tel:", "e:", "p:")


def display_address(address: str | None) -> str | None:
    """An address without its URI prefix: "mailto:A@B.com" -> "a@b.com"; phone numbers are left as written."""
    if not address or not address.strip():
        return None
    address = address.strip()
    for prefix in _PREFIXES:
        if address.lower().startswith(prefix):
            address = address[len(prefix) :]
            break
    return address.lower() if "@" in address else address


_PHONE = re.compile(r"\+?[\d\s().-]{7,}")  # digits and the usual punctuation, nothing else
_EMAIL = re.compile(r"[^@\s:;/]+@[^@\s:;/]+\.[^@\s:;/.]+")


def address_key(address: str | None) -> str | None:
    """A form to compare addresses by: a lowercased email, or the last 10 digits of a phone number. Anything else
    (a group's id, "urn:biz:...") has none, so its digits never pass for a phone number."""
    address = display_address(address)
    if address is None:
        return None
    if "@" in address:
        return address
    if not _PHONE.fullmatch(address):
        return None
    digits = re.sub(r"\D", "", address)
    return digits[-10:] if len(digits) >= 7 else None


def same_address(a: str | None, b: str | None) -> bool:
    """Whether two addresses are the same person's: equal emails (ignoring case), or the same phone number. Two
    numbers that both carry a country code must match in every digit; one without is compared by its last 10."""
    a, b = display_address(a), display_address(b)
    if not a or not b:
        return False
    if "@" in a or "@" in b:
        return a == b
    if not _PHONE.fullmatch(a) or not _PHONE.fullmatch(b):
        return False
    da, db = re.sub(r"\D", "", a), re.sub(r"\D", "", b)
    if len(da) < 7 or len(db) < 7:
        return False
    if len(da) > 10 and len(db) > 10:
        return da == db
    return da[-10:] == db[-10:]


def is_phone(address: str | None) -> bool:
    key = address_key(address)
    return key is not None and "@" not in key


def contact_address(value: str) -> str | None:
    """A phone number in international form (+15551234567) or an email: someone a conversation can be started with.

    Local numbers don't count, so a missing country code can never reach a stranger's number.
    """
    value = display_address(value) or ""  # without mailto:, tel:, or IMCore's internal e: and p:
    if "@" in value:
        return value if _EMAIL.fullmatch(value) else None
    digits = re.sub(r"\D", "", value)
    return f"+{digits}" if value.startswith("+") and _PHONE.fullmatch(value) and 8 <= len(digits) <= 15 else None
