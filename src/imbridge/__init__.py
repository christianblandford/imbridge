"""iMessage for Python and AI agents: send, reply inline, tapback with any emoji, and receive, through Messages.app."""

from importlib.metadata import PackageNotFoundError, version

from .chatdb import Attachment, Chat, ChatDB, FullDiskAccessError, Message
from .client import EFFECTS, ChatNotFound, IMBridge
from .protocol import HelperError, HelperNotConnected, HelperUnauthorized
from .reactions import CLASSIC_TAPBACKS, Reaction

try:
    __version__ = version("imbridge")
except PackageNotFoundError:  # running from a source checkout
    __version__ = "0.0.0"

__all__ = [
    "CLASSIC_TAPBACKS",
    "EFFECTS",
    "Attachment",
    "Chat",
    "ChatDB",
    "ChatNotFound",
    "FullDiskAccessError",
    "HelperError",
    "HelperNotConnected",
    "HelperUnauthorized",
    "IMBridge",
    "Message",
    "Reaction",
]
