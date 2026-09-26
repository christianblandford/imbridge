"""Link previews: what a message's rich link shows.

A link sent with a preview is a message with balloon_bundle_id URL_BALLOON whose payload_data is an NSKeyedArchiver
plist: a RichLink {richLinkMetadata, richLinkIsPlaceholder}, the metadata being LinkPresentation's LPLinkMetadata
(title, summary, siteName, URL, originalURL, and icon and image metadata whose pictures are the message's
attachments). A placeholder is a link sent before its preview loaded.
"""

from __future__ import annotations

import plistlib
from dataclasses import dataclass
from typing import Any

URL_BALLOON = "com.apple.messages.URLBalloonProvider"


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
