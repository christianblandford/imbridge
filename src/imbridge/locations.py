"""Location pins: reading the ones people send, and making one to send.

A pin is a small vCard attachment named "<place>.loc.vcf" (type text/x-vlocation) whose URL is an Apple Maps link:

    BEGIN:VCARD
    VERSION:3.0
    N:;Apple Park;;;
    FN:Apple Park
    item1.ADR;type=pref:;;One Apple Park Way;Cupertino;CA;95014;United States
    item2.URL;type=pref:https://maps.apple.com/?ll=37.334886\\,-122.008988&q=Apple%20Park
    item2.X-ABLabel:map url
    END:VCARD

vCard escapes commas and semicolons in values with a backslash ("37.33\\,-122.00").
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

PIN_TYPE = "text/x-vlocation"
PIN_SUFFIX = ".loc.vcf"


@dataclass(frozen=True)
class Location:
    """A place someone sent as a pin: where it is, and what they called it."""

    latitude: float
    longitude: float
    name: str | None = None  # the place's name ("Apple Park"), or None for a bare dropped pin
    address: str | None = None  # its street address, when the pin carries one
    url: str | None = None  # the Apple Maps link for it


def is_pin(mime_type: str | None, name: str | None) -> bool:
    return mime_type == PIN_TYPE or (name or "").lower().endswith(PIN_SUFFIX)


def read_pin(path: str | Path | None) -> Location | None:
    """The Location in a pin's vCard, or None if the file isn't there (Messages offloads old attachments) or holds no
    coordinates."""
    if not path:
        return None
    try:
        return parse_pin(Path(path).read_text(errors="replace"))
    except OSError:
        return None


def parse_pin(card: str) -> Location | None:
    fields: dict[str, str] = {}
    for line in _unfold(card).splitlines():
        key, _, value = line.partition(":")
        name = key.split(";")[0].split(".")[-1].upper()  # "item2.URL;type=pref" -> "URL"
        if name in ("FN", "ADR", "URL") and name not in fields:
            if name != "URL" or "maps.apple.com" in value:
                fields[name] = value
    url = _unescape(fields.get("URL", ""))
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    point = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*", (query.get("ll") or [""])[0])
    if not point:
        return None
    latitude, longitude = float(point[1]), float(point[2])
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    address = ", ".join(part for part in _unescape(fields.get("ADR", "")).split(";") if part.strip()) or None
    name = _unescape(fields.get("FN", "")).strip() or None
    if name and name.lower() in ("current location", "dropped pin"):
        name = None  # what Messages calls a pin with no place
    return Location(latitude, longitude, name, address, url or None)


def make_pin(latitude: float, longitude: float, name: str | None = None) -> str:
    """A pin's vCard for these coordinates, as Messages writes one."""
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("latitude goes from -90 to 90 and longitude from -180 to 180")
    title = (name or "").strip()
    point = f"{latitude:.6f}\\,{longitude:.6f}"
    query = f"ll={point}&q={urllib.parse.quote(title) if title else point}"
    shown = _escape(title or "Dropped Pin")
    return (
        "BEGIN:VCARD\r\nVERSION:3.0\r\n"
        f"N:;{shown};;;\r\nFN:{shown}\r\n"
        f"item1.URL;type=pref:https://maps.apple.com/?{query}\r\n"
        "item1.X-ABLabel:map url\r\nEND:VCARD\r\n"
    )


def _unfold(card: str) -> str:
    return re.sub(r"\r?\n[ \t]", "", card)  # vCard continues a long line on the next, indented by one space


def _unescape(value: str) -> str:
    return re.sub(r"\\([,;\\nN])", lambda m: "\n" if m[1] in "nN" else m[1], value)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")
