"""Link previews: reading the RichLink archive Messages stores with a link."""

import plistlib

from imbridge.links import LinkPreview, parse_link


def rich_link(*, title="Stonehenge", summary="A prehistoric monument", site="Wikipedia", placeholder=False):
    """payload_data as Messages stores it: RichLink {richLinkMetadata: LPLinkMetadata, richLinkIsPlaceholder}."""
    objects = ["$null"]

    def add(value) -> plistlib.UID:
        objects.append(value)
        return plistlib.UID(len(objects) - 1)

    def url(value):
        return add({"NS.base": plistlib.UID(0), "NS.relative": add(value)})

    fields = {"URL": url("https://en.wikipedia.org/wiki/Stonehenge"), "originalURL": url("https://w.wiki/Stonehenge")}
    for key, value in (("title", title), ("summary", summary), ("siteName", site)):
        if value is not None:
            fields[key] = add(value)
    metadata = add({**fields, "version": 1})
    root = add({"richLinkMetadata": metadata, "richLinkIsPlaceholder": placeholder})
    return plistlib.dumps({"$archiver": "NSKeyedArchiver", "$version": 100000, "$top": {"root": root},
                           "$objects": objects}, fmt=plistlib.FMT_BINARY)


def test_reading_a_link_preview():
    assert parse_link(rich_link()) == LinkPreview(
        "https://en.wikipedia.org/wiki/Stonehenge", "Stonehenge", "A prehistoric monument", "Wikipedia",
        "https://w.wiki/Stonehenge",
    )
    bare = parse_link(rich_link(summary=None, site=None))
    assert (bare.title, bare.summary, bare.site_name) == ("Stonehenge", None, None)


def test_not_previews():
    assert parse_link(None) is None
    assert parse_link(b"garbage") is None
