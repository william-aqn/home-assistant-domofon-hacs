"""SIP error taxonomy.

The distinction that matters is between "retry later" and "stop": a transport blip
should reconnect with backoff, while anything that could displace the resident's phone
must stop the client and stay stopped until a person intervenes.
"""

from __future__ import annotations


class SipError(Exception):
    """Base error for the SIP layer."""


class SipTransportError(SipError):
    """The connection failed or dropped. Recoverable: reconnect with backoff."""


class SipFramingError(SipError):
    """The byte stream cannot be trusted any more.

    Never recovered in place: resynchronising by scanning forward for the next
    "SIP/2.0" is how half a message gets processed as a whole one. The connection is
    torn down and rebuilt instead.
    """


class SipPermanentError(SipError):
    """Retrying will not help. The client stops until a person acts."""


class SipBlockedError(SipPermanentError):
    """Registering would displace somebody else's binding, so we did not register."""


class SipEvictionError(SipPermanentError):
    """Our registration displaced an existing binding. Withdrawn; SIP disabled."""


class SipUnverifiableError(SipPermanentError):
    """The registrar does not report bindings, so the eviction guard is blind.

    Registering anyway would mean gambling with the resident's doorbell, so we refuse.
    """


class SipSafetyError(SipError):
    """A message was requested that this integration must never emit.

    Raised by the builder, not by policy code, so the dangerous message cannot be
    constructed at all rather than merely being avoided by convention.
    """
