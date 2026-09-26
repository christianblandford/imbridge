"""iMessage for Python and AI agents: send, reply inline, tapback with any emoji, and receive, through Messages.app."""

from importlib.metadata import PackageNotFoundError, version

from .addresses import ANY_ADDRESS, AddressNotChosen, WrongAddress
from .chatdb import Attachment, ChatDB, ChatInfo, FullDiskAccessError, Message
from .client import EFFECTS, Chat, ChatNotFound, EditLimit, IMBridge, WrongChat
from .guard import ANY_CHAT, RateLimited, SendNotAllowed
from .protocol import HelperError, HelperNotConnected, HelperUnauthorized
from .reactions import CLASSIC_TAPBACKS, Reaction

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
    "Attachment",
    "Chat",
    "ChatDB",
    "ChatInfo",
    "ChatNotFound",
    "EditLimit",
    "FullDiskAccessError",
    "HelperError",
    "HelperNotConnected",
    "HelperUnauthorized",
    "IMBridge",
    "Message",
    "RateLimited",
    "Reaction",
    "SendNotAllowed",
    "WrongChat",
]
