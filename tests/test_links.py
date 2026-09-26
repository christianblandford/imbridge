"""Link previews: reading the RichLink archive Messages stores with a link, and sending one."""

import base64
import plistlib
import socket
import sqlite3
from pathlib import Path

import pytest
from helpers import ALEX, at
from test_sending import actions, run

from imbridge import SendNotAllowed, config
from imbridge.chatdb import ChatDB
from imbridge.links import URL_BALLOON, LinkPreview, is_public, parse_link, preview_urls, web_url

PAGE = "https://en.wikipedia.org/wiki/Stonehenge"


def rich_link(*, title="Stonehenge", summary="A prehistoric monument", site="Wikipedia", placeholder=False, image=None):
    """payload_data as Messages stores it: RichLink {richLinkMetadata: LPLinkMetadata, richLinkIsPlaceholder}. image is
    where its picture came from, relative to the page."""
    objects = ["$null"]

    def add(value) -> plistlib.UID:
        objects.append(value)
        return plistlib.UID(len(objects) - 1)

    def url(value, base=None):
        return add({"NS.base": base or plistlib.UID(0), "NS.relative": add(value)})

    fields = {"URL": url(PAGE), "originalURL": url("https://w.wiki/Stonehenge")}
    for key, value in (("title", title), ("summary", summary), ("siteName", site)):
        if value is not None:
            fields[key] = add(value)
    if image:
        fields["imageMetadata"] = add({"URL": url(image, base=url(PAGE)), "version": 1})
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


def test_links_in_messages(chat_db):
    db = sqlite3.connect(chat_db)
    db.execute("ALTER TABLE message ADD COLUMN balloon_bundle_id TEXT")
    db.execute("ALTER TABLE message ADD COLUMN payload_data BLOB")
    db.execute("ALTER TABLE attachment ADD COLUMN hide_attachment INTEGER DEFAULT 0")
    rowid = db.execute(
        "INSERT INTO message (guid, text, handle_id, is_from_me, date, service, cache_has_attachments,"
        " balloon_bundle_id, payload_data) VALUES ('LINK', ?, 1, 0, ?, 'iMessage', 1, ?, ?)",
        (PAGE, at(20), URL_BALLOON, rich_link()),
    ).lastrowid
    db.execute("INSERT INTO chat_message_join VALUES (1, ?, ?)", (rowid, at(20)))
    # the preview's pictures, which only the balloon shows (and, as if one were sent along, an ordinary photo)
    for guid, hidden in (("ICON", 1), ("IMAGE", 1), ("PHOTO", 0)):
        attachment = db.execute(
            "INSERT INTO attachment (guid, filename, transfer_name, hide_attachment) VALUES (?, ?, ?, ?)",
            (guid, f"~/Library/Messages/Attachments/{guid}.pluginPayloadAttachment",
             f"{guid}.pluginPayloadAttachment", hidden),
        ).lastrowid
        db.execute("INSERT INTO message_attachment_join VALUES (?, ?)", (rowid, attachment))
    db.commit()
    db.close()
    found = ChatDB(chat_db).message("LINK")
    assert found.text == PAGE and found.link.title == "Stonehenge"
    assert [attachment.guid for attachment in found.attachments] == ["PHOTO"]


def test_not_previews():
    assert parse_link(None) is None
    assert parse_link(b"garbage") is None


def test_links_to_preview():
    assert web_url("  https://example.com/a?b=c#d \n") == "https://example.com/a?b=c#d"
    assert web_url("HTTP://Example.com:8080") == "HTTP://Example.com:8080"
    for bad in ("example.com", "ftp://example.com/file", "javascript:alert(1)", "https://", "https://:80/",
                "https://user:secret@example.com/", "https://someone@example.com/", "https://example.com/a b",
                "https://example.com/\x00", "https://example.com:port/", "https://example.com/" + "a" * 4096, None):
        with pytest.raises(ValueError):
            web_url(bad)


ADDRESSES = {
    "example.com": ["93.184.215.14", "2606:2800:21f:cb07:6820:80da:af6b:8b2c"],
    "localhost": ["127.0.0.1", "::1"],
    "router.lan": ["192.168.1.1"],
    "printer.local": ["fe80::1%en0"],
    "machine.tailnet.ts.net": ["100.101.102.103"],  # shared address space, as Tailscale uses
    "office.example": ["fd12:3456::1"],
    "mapped.example": ["::ffff:10.0.0.1"],
    "nat64.example": ["64:ff9b::a00:1"],  # 10.0.0.1, reached from an IPv6-only network
    "nat64-public.example": ["64:ff9b::5db8:d70e"],  # 93.184.215.14
    "split.example": ["93.184.215.14", "10.0.0.8"],
}


def fake_dns(host, *args, **kwargs):
    if host not in ADDRESSES:
        raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided, or not known")
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0)) for address in ADDRESSES[host]]


def test_public_hosts(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", fake_dns)
    public = {host: is_public(host) for host in [*ADDRESSES, "nowhere.example"]}
    assert public == {
        "example.com": True, "localhost": False, "router.lan": False, "printer.local": False,
        "machine.tailnet.ts.net": False, "office.example": False, "mapped.example": False, "nat64.example": False,
        "nat64-public.example": True, "split.example": False, "nowhere.example": None,
    }


def test_where_a_preview_came_from():
    urls = preview_urls(rich_link(image="/static/stones.jpg"))
    assert urls == [PAGE, "https://w.wiki/Stonehenge", PAGE, "https://en.wikipedia.org/static/stones.jpg"]


def previewing(payload: bytes, pictures: int = 2):
    """Messages' answer to fetch-link: the payload, with its pictures saved where it was asked to put them."""
    def answer(request):
        folder = Path(request["data"]["directory"])
        saved = []
        for number in range(pictures):
            saved.append(folder / f"{number}.pluginPayloadAttachment")
            saved[-1].write_bytes(b"picture")
        return {"found": True, "payload": base64.b64encode(payload).decode(), "attachments": [str(p) for p in saved]}
    return answer


def resolving(monkeypatch, **public):
    monkeypatch.setattr("imbridge.client.is_public", lambda host: public.get(host.replace(".", "_"), True))


def test_sending_a_link(chat_db, monkeypatch):
    resolving(monkeypatch)
    payload = rich_link(image="/static/stones.jpg")
    guid = "0c8d6f6e-8ea5-4d51-9c3e-3a0e1b0a6a70"
    sent, requests = run(chat_db, lambda im: im.chat(ALEX).send_link(f" {PAGE} ", guid=guid),
                         {"fetch-link": previewing(payload)}, allow=[ALEX])
    assert actions(requests) == ["fetch-link", "send-link"] and sent == "SENT-2"
    fetch, send = requests[0]["data"], requests[1]["data"]
    assert fetch["url"] == PAGE and Path(fetch["directory"]).parent == config.OUTGOING
    assert (send["chatGuid"], send["url"], send["guid"]) == (ALEX, PAGE, guid.upper())
    assert base64.b64decode(send["payload"]) == payload
    assert [Path(p).name for p in send["attachments"]] == ["0.pluginPayloadAttachment", "1.pluginPayloadAttachment"]
    assert all(Path(p).exists() for p in send["attachments"])  # they're the pictures' transfers: kept


def test_links_without_a_preview_go_as_plain_links(chat_db, monkeypatch):
    resolving(monkeypatch)
    _, requests = run(chat_db, lambda im: im.send_link(ALEX, PAGE),
                      {"fetch-link": {"found": False, "reason": "timed out"}}, allow=[ALEX])
    assert actions(requests) == ["fetch-link", "send-message"] and requests[1]["data"]["message"] == PAGE
    assert not any(config.OUTGOING.iterdir())
    resolving(monkeypatch, nowhere_example=None)  # doesn't resolve here, so Messages couldn't load it either
    _, requests = run(chat_db, lambda im: im.send_link(ALEX, "https://nowhere.example/"), allow=[ALEX])
    assert actions(requests) == ["send-message"]


def test_links_to_private_addresses_are_refused(chat_db, monkeypatch):
    resolving(monkeypatch, router_lan=False, intranet_example=False)
    log = []
    with pytest.raises(ValueError, match="router.lan isn't on the public internet"):
        run(chat_db, lambda im: im.send_link(ALEX, "http://router.lan/admin"), None, log, allow=[ALEX])
    assert log == []  # Messages never loads it
    # a public page that redirects, or takes a picture from, somewhere private: loaded, but never sent
    payload = rich_link(image="http://intranet.example/secret.png")
    with pytest.raises(ValueError, match="comes partly from intranet.example"):
        run(chat_db, lambda im: im.send_link(ALEX, PAGE), {"fetch-link": previewing(payload)}, log, allow=[ALEX])
    assert actions(log) == ["fetch-link"]
    assert not any(config.OUTGOING.iterdir())


def test_links_need_an_allowed_chat(chat_db, monkeypatch):
    resolving(monkeypatch)
    log = []
    with pytest.raises(SendNotAllowed):
        run(chat_db, lambda im: im.send_link(ALEX, PAGE), None, log)
    with pytest.raises(ValueError):
        run(chat_db, lambda im: im.send_link(ALEX, "file:///etc/passwd"), None, log, allow=[ALEX])
    assert log == []
