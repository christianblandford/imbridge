"""Link previews: what a message's rich link shows, and checking a link before sending one.

A link sent with a preview is a message with balloon_bundle_id URL_BALLOON whose payload_data is an NSKeyedArchiver
plist: a RichLink {richLinkMetadata, richLinkIsPlaceholder}, the metadata being LinkPresentation's LPLinkMetadata
(title, summary, siteName, URL, originalURL, and icon and image metadata whose pictures are the message's
attachments). A placeholder is a link sent before its preview loaded.

To send one, Messages loads the page on this Mac, as it does when you paste a link. So imbridge only asks for
previews of pages on the public internet, and checks every address the preview names before it goes out: a link, a
redirect or a picture on this Mac or your local network could otherwise put what's there into a chat.
"""

from __future__ import annotations

import ipaddress
import plistlib
import socket
import urllib.parse
from dataclasses import dataclass
from typing import Any

URL_BALLOON = "com.apple.messages.URLBalloonProvider"
MAX_URL = 4096  # characters in a link sent with a preview
_NAT64 = ipaddress.ip_network("64:ff9b::/96")  # IPv6-only networks reach IPv4 hosts through these


@dataclass(frozen=True)
class LinkPreview:
    url: str | None  # where the link leads
    title: str | None
    summary: str | None = None
    site_name: str | None = None
    original_url: str | None = None  # the link as it was written, before any redirect


def parse_link(data: bytes | None) -> LinkPreview | None:
    """The preview in a URL balloon's payload_data, or None (no payload, or a placeholder with nothing loaded)."""
    if not data:
        return None
    try:
        archive = plistlib.loads(data)
        objects = archive["$objects"]
        root = _object(objects, archive["$top"]["root"])
    except (plistlib.InvalidFileException, ValueError, KeyError, IndexError, TypeError):
        return None
    metadata = _object(objects, root.get("richLinkMetadata")) if isinstance(root, dict) else None
    if not isinstance(metadata, dict):
        return None

    def text(key: str) -> str | None:
        value = _object(objects, metadata.get(key))
        if isinstance(value, dict):  # an NSMutableString
            value = _object(objects, value.get("NS.string"))
        return value.strip() or None if isinstance(value, str) and value != "$null" else None

    def url(key: str) -> str | None:
        value = _object(objects, metadata.get(key))
        relative = _object(objects, value.get("NS.relative")) if isinstance(value, dict) else None
        return relative if isinstance(relative, str) and relative != "$null" else None

    preview = LinkPreview(url("URL"), text("title"), text("summary"), text("siteName"), url("originalURL"))
    return preview if preview.url or preview.title else None


def _object(objects: list[Any], value: Any) -> Any:
    return objects[value.data] if isinstance(value, plistlib.UID) else value


def web_url(url: str) -> str:
    """url, stripped, if it's one web link a preview can be made for: http or https, a host, no user name or
    password (they'd go out in the message). ValueError otherwise."""
    url = url.strip() if isinstance(url, str) else ""
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError for a port that isn't a number
    except ValueError:
        parts = None
    if parts is None or parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError(f"a link preview needs an http:// or https:// link with a host, not {url[:100]!r}")
    if parts.username is not None or parts.password is not None:
        raise ValueError("imbridge doesn't send links with a user name or password in them")
    if len(url) > MAX_URL or any(char.isspace() or ord(char) < 32 for char in url):
        raise ValueError(f"a link preview needs a single link of up to {MAX_URL} characters, without spaces")
    return url


def is_public(host: str) -> bool | None:
    """Whether every address host has is on the public internet (False for this Mac, your local network, and other
    private or reserved ranges), or None if it has none (it doesn't resolve)."""
    try:
        found = {info[4][0] for info in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)}
    except (OSError, UnicodeError):
        return None
    return all(_is_global(address) for address in found) if found else None


def _is_global(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%")[0])  # an IPv6 address can end in its interface, fe80::1%en0
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    elif ip in _NAT64:
        ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip.is_global


def preview_urls(payload: bytes) -> list[str]:
    """Every web address in a link preview's payload: the page, where it led, and where each picture came from."""
    objects = plistlib.loads(payload)["$objects"]
    return [url for item in objects if (url := _url(objects, item))]


def _url(objects: list[Any], value: Any, depth: int = 0) -> str | None:
    """The address an archived NSURL holds ({NS.relative, NS.base}, the base another NSURL or $null), or None."""
    value = _object(objects, value)
    if not isinstance(value, dict) or "NS.relative" not in value or depth > 8:
        return None
    relative = _object(objects, value["NS.relative"])
    if not isinstance(relative, str):
        return None
    base = _url(objects, value.get("NS.base"), depth + 1)
    return urllib.parse.urljoin(base, relative) if base else relative
