"""Constants for the SIP layer. No Home Assistant imports."""

from __future__ import annotations

from typing import Final

CRLF: Final = b"\r\n"

# Identify honestly. Impersonating the official client here would buy nothing and
# would make a support conversation with the operator impossible.
USER_AGENT: Final = "Loki-HomeAssistant/1.0"

# We answer these; we originate almost none of them. ACK and BYE appear because a
# caller may send them to us, and OPTIONS because registrars ping with it.
ALLOW: Final = "INVITE, ACK, CANCEL, BYE, OPTIONS"

SIP_PORT: Final = 5060

# Framing limits. A stream claiming more than this is not a SIP peer.
MAX_HEADER_BYTES: Final = 16384
MAX_BODY_BYTES: Final = 65536

# RFC 3261 timers.
T1: Final = 0.5
TIMER_F: Final = 64 * T1  # transaction timeout for a non-INVITE request

# RFC 3261 §7.3.3: accepting every compact form is a MUST for a receiver. We never
# emit one -- readability of our own traffic is worth more than the bytes saved.
COMPACT_HEADERS: Final[dict[str, str]] = {
    "i": "call-id",
    "f": "from",
    "t": "to",
    "v": "via",
    "m": "contact",
    "c": "content-type",
    "l": "content-length",
    "s": "subject",
    "k": "supported",
    "e": "content-encoding",
    "o": "event",
    "r": "refer-to",
    "b": "referred-by",
    "u": "allow-events",
    "a": "accept-contact",
    "j": "reject-contact",
    "d": "request-disposition",
    "x": "session-expires",
    "y": "identity",
    "n": "identity-info",
}

# RFC 3261 §8.1.1.7: every branch of a compliant implementation starts with this.
BRANCH_MAGIC: Final = "z9hG4bK"
