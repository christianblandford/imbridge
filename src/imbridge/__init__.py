"""iMessage for Python and AI agents: send, reply inline, tapback with any emoji, and receive, through Messages.app."""

from importlib.metadata import PackageNotFoundError, version

from .addresses import ANY_ADDRESS, AddressNotChosen, WrongAddress
from .chatdb import Attachment, ChatDB, ChatInfo, FullDiskAccessError, GroupEvent, Message, MessageNotFound
from .client import (
    EFFECTS,
    FEATURES,
    Chat,
    ChatNotFound,
    EditLimit,
    IMBridge,
    SendLaterFailed,
    Unsupported,
    WrongChat,
    question_guid,
)
from .guard import ANY_CHAT, NewContact, RateLimited, SendNotAllowed
from .links import LinkPreview
from .locations import Location
from .polls import Poll, PollOption, PollResults, PollVote
from .protocol import HelperBusy, HelperError, HelperNotConnected, HelperUnauthorized
from .reactions import CLASSIC_TAPBACKS, Reaction
from .richtext import TEXT_EFFECTS, Span

try:
    __version__ = version("imbridge")
except PackageNotFoundError:  # running from a source checkout
    __version__ = "0.0.0"

__all__ = [
    "ANY_ADDRESS",
    "ANY_CHAT",
    "AddressNotChosen",
    "WrongAddress",
    "CLASSIC_TAPBACKS",
    "EFFECTS",
    "FEATURES",
    "Attachment",
    "Chat",
    "ChatDB",
    "ChatInfo",
    "ChatNotFound",
    "EditLimit",
    "FullDiskAccessError",
    "GroupEvent",
    "HelperBusy",
    "HelperError",
    "HelperNotConnected",
    "HelperUnauthorized",
    "IMBridge",
    "LinkPreview",
    "Location",
    "Message",
    "MessageNotFound",
    "NewContact",
    "Poll",
    "PollOption",
    "PollResults",
    "PollVote",
    "RateLimited",
    "Reaction",
    "SendLaterFailed",
    "SendNotAllowed",
    "Span",
    "TEXT_EFFECTS",
    "Unsupported",
    "WrongChat",
    "question_guid",
]
