from imbridge.typedstream import attributed_body_mentions, attributed_body_text

HEADER = (
    b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00"
    b"\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+"
)
TRAILER = b"\x86\x84\x02iI\x01\x05\x92\x84\x84\x84\x0cNSDictionary\x00\x94\x84\x01i\x01\x92\x86\x86"


def archive(text: str) -> bytes:
    data = text.encode()
    if len(data) < 0x80:
        length = bytes([len(data)])
    elif len(data) < 0x10000:
        length = b"\x81" + len(data).to_bytes(2, "little")
    else:
        length = b"\x82" + len(data).to_bytes(4, "little")
    return HEADER + length + data + TRAILER


def test_short_text():
    assert attributed_body_text(archive("hello")) == "hello"


def test_multibyte_text_uses_byte_lengths():
    assert attributed_body_text(archive("on it 👀 — ünïcode")) == "on it 👀 — ünïcode"


def test_two_byte_length():
    text = "x" * 300
    assert attributed_body_text(archive(text)) == text


def test_four_byte_length():
    text = "y" * 70_000
    assert attributed_body_text(archive(text)) == text


def test_nothing_to_decode():
    assert attributed_body_text(None) is None
    assert attributed_body_text(b"") is None
    assert attributed_body_text(b"no string class in here") is None


def mention(address: str) -> bytes:
    # as Messages writes it: the key, then the handle as a string object referring to NSString and '+'
    return b"\x92\x84\x96\x96\x1c__kIMMentionConfirmedMention\x86\x92\x84\x96\x96" + archive_string(address) + b"\x86"


def archive_string(value: str) -> bytes:
    return bytes([len(value.encode())]) + value.encode()


def test_mentions():
    body = archive("hey Sam and Jo") + mention("+15550002222") + mention("jo@example.com") + mention("+15550002222")
    assert attributed_body_mentions(body) == ("+15550002222", "jo@example.com")  # in order, each once
    assert attributed_body_text(body) == "hey Sam and Jo"
    assert attributed_body_mentions(archive("no one")) == ()
    assert attributed_body_mentions(None) == ()

