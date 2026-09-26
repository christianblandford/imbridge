import pytest

from imbridge import config
from imbridge.guard import (
    ANY_CHAT,
    ANY_LINE,
    NewContact,
    RateLimited,
    SendGuard,
    SendNotAllowed,
    read_allowed,
    write_allowed,
)

ALEX, CREW = "any;-;+15551234567", "any;+;chat123"


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


def test_nothing_is_allowed_by_default():
    guard = SendGuard()
    assert not guard.allows(ALEX)
    with pytest.raises(SendNotAllowed, match="imbridge allow"):
        guard.check_allowed(ALEX)


def test_the_allowed_chats_file():
    write_allowed({ALEX})
    assert read_allowed() == {ALEX}
    assert SendGuard().allows(ALEX)
    assert not SendGuard().allows(CREW)
    assert (config.APP_SUPPORT / "allowed-chats").stat().st_mode & 0o777 == 0o600


def test_any_chat_line():
    write_allowed({ANY_LINE})
    assert SendGuard().allows(CREW)


def test_disallowing_applies_to_a_running_guard():
    write_allowed({ALEX})
    guard = SendGuard()
    assert guard.allows(ALEX)
    write_allowed(set())
    assert not guard.allows(ALEX)


def test_allowed_in_code_resolves_phone_numbers():
    guard = SendGuard(["(555) 123-4567"], resolve=lambda chat: ALEX if "555" in chat else chat)
    assert guard.allows(ALEX)
    assert not guard.allows(CREW)


def no_conversations(entry):
    raise LookupError(entry)


def test_people_allowed_before_you_have_a_conversation():
    guard = SendGuard([NewContact("+1 (555) 000-9999"), NewContact("new@example.com")], resolve=no_conversations)
    assert guard.allows_handle("+15550009999") and guard.allows_handle("NEW@example.com")
    assert guard.allows("any;-;+15550009999")  # the conversation, once it has started
    assert not guard.allows_handle("+15550001234")
    for typo in ["Crue", "+15550009998"]:  # anything else that matches no chat is a typo, numbers included
        with pytest.raises(LookupError):
            SendGuard([typo], resolve=no_conversations).resolve_allowed()


def test_new_contacts_need_a_full_number_or_an_email():
    assert NewContact(" +1 (555) 000-9999 ") == "+15550009999"
    for vague in ["555 000 9999", "Crew", "any;-;+15550009999"]:
        with pytest.raises(ValueError):
            NewContact(vague)


def test_an_allowed_group_is_never_taken_for_a_person():
    write_allowed({"any;+;chat935842394823948234"})  # mostly digits, ending like a phone number
    assert not SendGuard().allows_handle("+14823948234")
    assert not SendGuard().allows("any;-;+14823948234")


def test_any_chat_in_code():
    assert SendGuard(ANY_CHAT).allows(CREW)


def test_per_chat_limit():
    clock = Clock()
    guard = SendGuard(max_per_chat=3, clock=clock)
    for _ in range(3):
        guard.record_send(ALEX)
    with pytest.raises(RateLimited, match="in case something is looping"):
        guard.record_send(ALEX)
    guard.record_send(CREW)  # other chats aren't affected
    clock.now += 61
    guard.record_send(ALEX)  # a minute later it's fine again


def test_total_limit():
    guard = SendGuard(max_per_chat=10, max_total=4, clock=Clock())
    for chat in (ALEX, CREW, ALEX, CREW):
        guard.record_send(chat)
    with pytest.raises(RateLimited):
        guard.record_send("any;-;+15550000000")


def test_limits_are_shared_by_every_process():
    clock = Clock()
    SendGuard(max_per_chat=2, clock=clock).record_send(ALEX)
    SendGuard(max_per_chat=2, clock=clock).record_send(ALEX)
    with pytest.raises(RateLimited):
        SendGuard(max_per_chat=2, clock=clock).record_send(ALEX)


def test_people_are_matched_by_their_whole_number():
    guard = SendGuard([NewContact("+447911123456")], resolve=no_conversations)
    assert guard.allows_handle("+44 7911 123456")
    assert not guard.allows_handle("+17911123456")  # same last 10 digits, a different person
    assert not guard.allows("any;-;+17911123456")
