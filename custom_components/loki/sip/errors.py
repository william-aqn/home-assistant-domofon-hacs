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


class SipRejectedError(SipPermanentError):
    """The registrar refused to register this account at all.

    Stale credentials, an address-of-record it does not know, an account not allowed
    to use SIP -- and a challenge we could not answer, which looks the same from here.
    Permanent in the sense that the very next attempt would fail identically, so it
    is not retried on the transport curve.

    It is *not* permanent in the sense the states above are. Nothing on the account
    changes because of it: a REGISTER whose credentials are refused creates no
    binding, displaces nothing, and leaves the resident's phone exactly where it was.
    The only cost of looking again is a failed-authentication event at the registrar,
    which is why the recheck is slow rather than absent -- absent is what it was, and
    it cost the doorbell every second between the credentials being renewed and
    somebody noticing a repair card.

    Handled before ``SipPermanentError`` in the supervisor's except chain. It is a
    subclass, so the broader clause would otherwise swallow it and stop for good.
    """


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
