"""Signalling-only SIP for Loki.

Deliberately no media: the whole product scenario -- the doorbell rings, the camera
appears, the door opens -- needs REGISTER, an inbound INVITE, a 180 Ringing and a REST
call. Adding RTP would buy two-way audio at the cost of a native dependency that does
not build on most Home Assistant installations.

No Home Assistant imports anywhere in this package, so every part of it is testable
against a plain socket.
"""

from __future__ import annotations

from .digest import DigestChallenge, challenges_from
from .errors import (
    SipBlockedError,
    SipError,
    SipEvictionError,
    SipFramingError,
    SipPermanentError,
    SipSafetyError,
    SipTransportError,
    SipUnverifiableError,
)
from .messages import (
    Header,
    MessageBuilder,
    Ping,
    Pong,
    SipMessage,
    StreamFramer,
    parse_message,
)
from .registration import RegistrationState
from .uri import SipUri, display_name, parse_uri

__all__ = [
    "DigestChallenge",
    "Header",
    "MessageBuilder",
    "Ping",
    "Pong",
    "RegistrationState",
    "SipBlockedError",
    "SipError",
    "SipEvictionError",
    "SipFramingError",
    "SipMessage",
    "SipPermanentError",
    "SipSafetyError",
    "SipTransportError",
    "SipUnverifiableError",
    "SipUri",
    "StreamFramer",
    "challenges_from",
    "display_name",
    "parse_message",
    "parse_uri",
]
