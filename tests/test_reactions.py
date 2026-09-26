import pytest

from imbridge.reactions import Reaction, parse_reaction, parse_target, reaction_type


@pytest.mark.parametrize(
    ("given", "remove", "expected"),
    [
        ("love", False, "love"),
        ("LAUGH", False, "laugh"),
        ("question", True, "-question"),
        ("👀", False, "emoji:👀"),
        ("🎟️", True, "-emoji:🎟️"),
    ],
)
def test_reaction_type(given, remove, expected):
    assert reaction_type(given, remove) == expected


def test_reaction_type_needs_something():
    with pytest.raises(ValueError):
        reaction_type("  ")


@pytest.mark.parametrize(
    ("given", "expected"),
    [("p:0/ABC", ("ABC", 0)), ("p:2/ABC", ("ABC", 2)), ("bp:ABC", ("ABC", 0)), ("ABC", ("ABC", 0))],
)
def test_parse_target(given, expected):
    assert parse_target(given) == expected


def test_classic_tapbacks():
    assert parse_reaction(2000, "p:0/G") == Reaction("love", None, False, "G", 0)
    assert parse_reaction(3003, "p:1/G") == Reaction("laugh", None, True, "G", 1)


def test_emoji_tapbacks():
    added = parse_reaction(2006, "p:0/G", "👀")
    assert added == Reaction("emoji", "👀", False, "G", 0)
    assert added.label == "👀"
    assert parse_reaction(3006, "p:0/G", "👀").removed


def test_stickers_and_unknown_codes():
    assert parse_reaction(1000, "bp:G").kind == "sticker"
    assert parse_reaction(2099, "p:0/G").kind == "unknown"


def test_not_a_reaction():
    assert parse_reaction(0, None) is None
    assert parse_reaction(None, "p:0/G") is None
    assert parse_reaction(2, "p:0/G") is None


def test_sticker_tapbacks():
    added = parse_reaction(2007, "p:0/GUID")
    assert (added.kind, added.removed, added.target_guid) == ("sticker_tapback", False, "GUID")
    assert added.label == "sticker_tapback"
    assert parse_reaction(3007, "p:0/GUID").removed
    assert parse_reaction(1000, "p:0/GUID").kind == "sticker"  # stuck onto the bubble, not a tapback
