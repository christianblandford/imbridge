"""Location pins: reading the vCards people send, and sending one."""

import sqlite3
from pathlib import Path

import pytest
from helpers import ALEX, at
from test_sending import run

from imbridge import Location, SendNotAllowed
from imbridge.chatdb import ChatDB
from imbridge.locations import make_pin, parse_pin

PLACE = """BEGIN:VCARD\r
VERSION:3.0\r
PRODID:-//Apple Inc.//iPhone OS 26.0//EN\r
N:;Apple Park;;;\r
FN:Apple Park\r
item1.ADR;type=WORK;type=pref:;;One Apple Park Way;Cupertino;CA;95014;United States\r
item1.X-ABADR:us\r
item2.URL;type=pref:https://maps.apple.com/?address=One%20Apple%20Park%20Way&auid=1&ll=37.334886\\,-122.00\r
 8988&lsp=9902&q=Apple%20Park\r
item2.X-ABLabel:map url\r
END:VCARD\r
"""
DROPPED = """BEGIN:VCARD
VERSION:3.0
N:;Current Location;;;
FN:Current Location
item1.URL;type=pref:http://maps.apple.com/?ll=-33.856784\\,151.215297&q=-33.856784\\,151.215297
item1.X-ABLabel:map url
END:VCARD
"""


def test_reading_pins():
    place = parse_pin(PLACE)  # the URL is folded onto two lines, and its comma escaped, as vCard does
    assert (place.latitude, place.longitude, place.name) == (37.334886, -122.008988, "Apple Park")
    assert place.address == "One Apple Park Way, Cupertino, CA, 95014, United States"
    assert place.url.startswith("https://maps.apple.com/?")
    dropped = parse_pin(DROPPED)
    assert (dropped.latitude, dropped.longitude, dropped.name) == (-33.856784, 151.215297, None)  # no place: no name
    assert parse_pin("BEGIN:VCARD\nFN:Someone\nitem1.URL:https://example.com/?ll=1\\,2\nEND:VCARD") is None
    assert parse_pin(DROPPED.replace("-33.856784", "-133.856784")) is None  # not a place on Earth


def test_making_pins():
    card = make_pin(40.689247, -74.044502, "Statue of Liberty; NYC, US")
    assert parse_pin(card) == Location(
        40.689247, -74.044502, "Statue of Liberty; NYC, US", None, parse_pin(card).url
    )
    assert parse_pin(make_pin(1.5, 2.5)).name is None  # a dropped pin
    with pytest.raises(ValueError):
        make_pin(91, 0)


def test_pins_in_messages(chat_db, tmp_path):
    pin = tmp_path / "Apple Park.loc.vcf"
    pin.write_text(PLACE)
    db = sqlite3.connect(chat_db)
    rowid = db.execute(
        "INSERT INTO message (guid, text, handle_id, is_from_me, date, service, cache_has_attachments)"
        " VALUES ('PIN', '￼', 1, 0, ?, 'iMessage', 1)", (at(20),),
    ).lastrowid
    db.execute("INSERT INTO chat_message_join VALUES (1, ?, ?)", (rowid, at(20)))
    attachment = db.execute(
        "INSERT INTO attachment (guid, filename, mime_type, transfer_name) VALUES ('A2', ?, 'text/x-vlocation', ?)",
        (str(pin), pin.name),
    ).lastrowid
    db.execute("INSERT INTO message_attachment_join VALUES (?, ?)", (rowid, attachment))
    db.commit()
    db.close()
    found = ChatDB(chat_db).message("PIN")
    assert found.location.name == "Apple Park" and found.location.latitude == 37.334886
    pin.unlink()  # Messages offloads old attachments: no file, no location, but the attachment is still there
    offloaded = ChatDB(chat_db).message("PIN")
    assert offloaded.location is None and offloaded.attachments


def test_sending_a_pin(chat_db):
    _, (request,) = run(chat_db, lambda im: im.chat(ALEX).send_location(37.8199, -122.4783, name="Golden Gate/Bridge"),
                        allow=[ALEX])
    data = request["data"]
    assert request["action"] == "send-attachment" and request["file_existed"]
    assert data["filePath"].endswith("/Golden GateBridge.loc.vcf")  # a safe file name
    sent = parse_pin(Path(data["filePath"]).read_text())
    assert (sent.latitude, sent.longitude, sent.name) == (37.8199, -122.4783, "Golden Gate/Bridge")
    log = []
    with pytest.raises(ValueError):
        run(chat_db, lambda im: im.send_location(ALEX, 0, 200), None, log, allow=[ALEX])
    with pytest.raises(SendNotAllowed):
        run(chat_db, lambda im: im.send_location(ALEX, 1, 2), None, log)
    assert log == []
