"""SIP URI parsing and the list scanners every other module shares.

One scanner, used for header rows, parameter lists and URI parameters alike. Writing a
second one is how ``qop="auth,auth-int"`` ends up shredded in one place and intact in
another.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def split_on(value: str, separator: str) -> list[str]:
    """Split on a separator appearing outside quotes and outside angle brackets."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    in_quotes = False
    escaped = False

    for char in value:
        if in_quotes:
            current.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_quotes = False
            continue
        if char == '"':
            in_quotes = True
        elif char == "<":
            depth += 1
        elif char == ">":
            depth = max(0, depth - 1)
        elif char == separator and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)

    parts.append("".join(current).strip())
    return [part for part in parts if part]


def split_commas(value: str) -> list[str]:
    """Split a comma-separated header row or credential list."""
    return split_on(value, ",")


def split_semis(value: str) -> list[str]:
    """Split header or URI parameters."""
    return split_on(value, ";")


def parse_params(items: list[str]) -> dict[str, str]:
    """Parse ``key=value`` items, unquoting values that were quoted."""
    out: dict[str, str] = {}
    for item in items:
        key, _, value = item.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        out[key.strip().lower()] = value
    return out


def has_header_param(value: str, name: str) -> bool:
    """Whether a header value already carries the named parameter.

    Used before adding a To tag: RFC 3261 §8.2.6.2 requires the same tag on every
    response to a request, and adding a second one produces a message no proxy will
    match to its transaction.
    """
    pieces = split_semis(value)
    return name.lower() in parse_params(pieces[1:] if pieces else [])


def name_addr(raw: str) -> str:
    """A header value with its header parameters removed.

    Used on ``From`` before the value leaves the SIP layer. The tag changes on every
    call, and the value is handed to a backend lookup that matches on the whole
    string; the official client never includes it, because pjsua's ``getRemoteUri()``
    prints the name-addr alone. All three forms were measured to resolve the same
    door, so this is about matching the reference rather than about correctness.

    In the bracket-less form every parameter is a header parameter (RFC 3261 §20.10
    requires angle brackets to carry URI parameters), so the first one ends the value.
    """
    text = raw.strip()
    if ">" in text:
        return text[: text.index(">") + 1].strip()
    pieces = split_semis(text)
    return pieces[0] if pieces else text


@dataclass(frozen=True, slots=True)
class SipUri:
    """A parsed SIP URI, enough of one to compare and rebuild."""

    scheme: str
    user: str
    host: str
    port: int | None
    params: dict[str, str] = field(default_factory=dict)

    @property
    def bare(self) -> str:
        """``scheme:user@host[:port]`` with no parameters."""
        authority = self.host if self.port is None else f"{self.host}:{self.port}"
        return (
            f"{self.scheme}:{self.user}@{authority}"
            if self.user
            else (f"{self.scheme}:{authority}")
        )

    def equivalent(self, other: SipUri) -> bool:
        """Compare per RFC 3261 §19.1.4.

        Only user, host, port and the transport parameter are compared, and everything
        but the user part is case-insensitive. A binding must not be judged foreign
        merely because the registrar echoed it with different capitalisation or with
        parameters we did not send.
        """
        return (
            self.scheme.lower() == other.scheme.lower()
            and self.user == other.user
            and self.host.lower() == other.host.lower()
            and self.port == other.port
            and self.params.get("transport", "").lower()
            == other.params.get("transport", "").lower()
        )


def parse_uri(raw: str) -> SipUri | None:
    """Parse a SIP URI, with or without angle brackets and a display name."""
    text = raw.strip()
    if "<" in text and ">" in text:
        text = text[text.index("<") + 1 : text.index(">")].strip()

    scheme, sep, rest = text.partition(":")
    if not sep or scheme.lower() not in ("sip", "sips"):
        return None

    pieces = split_semis(rest)
    if not pieces:
        return None
    authority = pieces[0]
    params = parse_params(pieces[1:])

    user, _, hostport = authority.rpartition("@")

    port: int | None = None
    host = hostport
    if hostport.startswith("["):
        # IPv6 literal: the colons inside the brackets are part of the address.
        close = hostport.find("]")
        if close == -1:
            return None
        host = hostport[1:close]
        remainder = hostport[close + 1 :]
        if remainder.startswith(":"):
            port = _port(remainder[1:])
    elif ":" in hostport:
        host, _, raw_port = hostport.partition(":")
        port = _port(raw_port)

    if not host:
        return None
    return SipUri(scheme.lower(), user, host, port, params)


def _port(raw: str) -> int | None:
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if 0 < value < 65536 else None


def display_name(raw: str) -> str:
    """Extract the display name from a name-addr, or return the URI itself.

    The official client takes the substring before ``<`` and throws when there is no
    ``<`` at all -- a bare ``sip:user@host`` crashes it. Ours must not, because the
    display name is only ever cosmetic here: the device lookup uses the whole raw
    value.
    """
    text = raw.strip()
    if "<" not in text:
        return text
    name = text[: text.index("<")].strip()
    if len(name) >= 2 and name[0] == '"' and name[-1] == '"':
        name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return name
