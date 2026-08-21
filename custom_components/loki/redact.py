"""The single sanctioned path for secrets into a log line.

Debug logs get pasted into bug reports. Everything that could authenticate someone,
identify the resident, or unlock a door is masked here rather than at each call site,
so there is exactly one place to audit.
"""

from __future__ import annotations

from typing import Any

# Values that are outright secret: never show any part of them.
_SECRET_KEYS = frozenset(
    {
        "token",
        "refresh",
        "refresh_token",
        "access_token",
        "hash",
        "password",
        "sms_code",
        "code",
        "pin",
        "authorization",
        "google_device_token",
    }
)

# Values that identify a person: show just enough to tell two apart.
_PARTIAL_KEYS = frozenset({"phone", "username", "user"})

_KEEP = 4


def redact_value(key: str, value: Any) -> Any:
    """Mask one value according to how sensitive its key is."""
    lowered = key.lower()

    if lowered in _SECRET_KEYS:
        return "***" if value else value

    if lowered in _PARTIAL_KEYS and isinstance(value, str) and len(value) > _KEEP:
        # Enough to distinguish two accounts, not enough to dial.
        return f"{value[:2]}***{value[-_KEEP:]}"

    return value


def redact(payload: Any) -> Any:
    """Return a copy of a request/response body safe to log.

    Recurses into nested dicts and lists -- the ``sip`` object arrives nested, and its
    password must not survive.
    """
    if isinstance(payload, dict):
        return {key: redact(redact_value(key, value)) for key, value in payload.items()}
    if isinstance(payload, list):
        return [redact(item) for item in payload]
    return payload


def redact_url(url: str | None) -> str | None:
    """Strip inline credentials from a stream URL.

    Device URLs embed ``user:pass@`` and end up in diagnostics; the host and channel are
    the useful part for troubleshooting, the credentials are not.
    """
    if not url or "@" not in url:
        return url

    scheme, separator, rest = url.partition("://")
    if not separator:
        return f"***@{url.rsplit('@', 1)[-1]}"
    return f"{scheme}://***@{rest.rsplit('@', 1)[-1]}"


def describe_response(payload: Any) -> str:
    """Summarise a response for a log line without leaking its contents.

    Says which keys came back and whether each has a value -- enough to tell "the
    server answered with a token" from "the server answered with an empty token",
    which is the distinction that actually matters when a login fails.
    """
    if isinstance(payload, dict):
        parts = (
            f"{key}={'set' if value else 'empty'}" for key, value in payload.items()
        )
        return "{" + ", ".join(parts) + "}"
    if isinstance(payload, list):
        return f"[{len(payload)} items]"
    if payload is None:
        return "empty body"
    return type(payload).__name__
