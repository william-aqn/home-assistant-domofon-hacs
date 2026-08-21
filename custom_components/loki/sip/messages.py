"""SIP framing, parsing and serialisation. No Home Assistant imports.

``MessageBuilder`` is the only place a SIP message becomes bytes. The hard safety
rules live there rather than in the policy code that calls it, so a message that could
take the resident's call away cannot be constructed at all -- not merely avoided by
convention.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
import re
from typing import TYPE_CHECKING

from .const import (
    ALLOW,
    COMPACT_HEADERS,
    CRLF,
    MAX_BODY_BYTES,
    MAX_HEADER_BYTES,
    USER_AGENT,
)
from .errors import SipFramingError, SipSafetyError
from .uri import has_header_param

if TYPE_CHECKING:
    from .registration import RegistrationState

_STATUS_LINE = re.compile(r"^SIP/2\.0\s+(\d{3})\s*(.*)$")
_REQUEST_LINE = re.compile(r"^([A-Za-z]+)\s+(\S+)\s+SIP/2\.0$")

# Echoed byte-for-byte into every response. Record-Route is handled separately: it is
# meaningful only on dialog-creating responses, and a stateful proxy does not forward
# a 100 anyway (RFC 3261 §16.7 step 5).
_ECHO = ("via", "from")


class Pong:
    """RFC 5626 §4.4.1: a bare CRLF from the server, answering our ping."""

    __slots__ = ()


class Ping:
    """RFC 5626 §4.4.1: a double CRLF from the server. We must answer with a CRLF."""

    __slots__ = ()


@dataclass(frozen=True, slots=True)
class Header:
    """One header row in three forms; each is needed for a different job."""

    name: str  # lowercased, compact form expanded
    value: str  # latin-1, unfolded, stripped -- for STRUCTURAL parsing only
    raw: bytes  # the original row without CRLF -- for echoing verbatim

    def text(self) -> str:
        """UTF-8 view of the value.

        Anything leaving the SIP layer -- a door's display name on its way to the
        device lookup, a log line -- must come from here. ``value`` is a latin-1 decode
        chosen so every byte round-trips for parsing; using it for a Cyrillic display
        name produces mojibake, and door resolution then fails on every single call.
        """
        return self.raw.partition(b":")[2].strip().decode("utf-8", "replace")


@dataclass(frozen=True, slots=True)
class SipMessage:
    """A parsed request or response. The body is never retained: we read no SDP."""

    start_line: str
    headers: tuple[Header, ...]
    is_response: bool
    method: str | None  # request method, or the CSeq method of a response
    status: int | None
    request_uri: str | None

    def first(self, name: str) -> Header | None:
        """First header with this name, or None."""
        return next((header for header in self.headers if header.name == name), None)

    def value(self, name: str) -> str:
        """Structural value of the first header with this name, or ""."""
        header = self.first(name)
        return "" if header is None else header.value

    def all_raw(self, name: str) -> tuple[bytes, ...]:
        """Every row with this name, in order, exactly as received."""
        return tuple(header.raw for header in self.headers if header.name == name)

    @property
    def call_id(self) -> str:
        """The Call-ID."""
        return self.value("call-id")

    @property
    def cseq(self) -> tuple[int, str]:
        """The CSeq as (number, method), or (0, "") if unparseable."""
        parts = self.value("cseq").split()
        return (int(parts[0]), parts[1].upper()) if len(parts) == 2 else (0, "")


def _unfold(block: bytes) -> list[bytes]:
    """RFC 3261 §7.3.1: a line starting with SP or HTAB continues the previous one."""
    rows: list[bytearray] = []
    for line in block.split(CRLF):
        if line[:1] in (b" ", b"\t") and rows:
            rows[-1] += b" " + line.strip()
        else:
            rows.append(bytearray(line))
    return [bytes(row) for row in rows if row]


def parse_message(block: bytes) -> SipMessage:
    """Parse a header block into a message."""
    rows = _unfold(block)
    if not rows:
        raise SipFramingError("empty header block")

    start_line = rows[0].decode("latin-1").strip()
    headers: list[Header] = []
    for raw in rows[1:]:
        name, sep, value = raw.decode("latin-1").partition(":")
        if not sep:
            continue  # §8.2.2: ignore what we do not understand
        key = name.strip().lower()
        headers.append(Header(COMPACT_HEADERS.get(key, key), value.strip(), raw))

    tup = tuple(headers)

    if match := _STATUS_LINE.match(start_line):
        cseq = next((h.value for h in tup if h.name == "cseq"), "").split()
        return SipMessage(
            start_line,
            tup,
            True,
            cseq[1].upper() if len(cseq) == 2 else None,
            int(match.group(1)),
            None,
        )

    if match := _REQUEST_LINE.match(start_line):
        return SipMessage(
            start_line, tup, False, match.group(1).upper(), None, match.group(2)
        )

    raise SipFramingError("unparseable start line")


class StreamFramer:
    """Bytes in, messages out. Never desynchronises, never partially consumes.

    Any framing-level ambiguity raises: the caller closes the connection and rebuilds
    it with backoff. Scanning forward for the next "SIP/2.0" is how half a message gets
    processed as a whole one.
    """

    def __init__(self, on_anomaly: Callable[[str], None] | None = None) -> None:
        """Initialise an empty framer."""
        self._buf = bytearray()
        self._warn = on_anomaly or (lambda _message: None)

    def feed(self, data: bytes) -> Iterator[SipMessage | Pong | Ping]:
        """Consume bytes, yielding every complete message they contain."""
        self._buf += data
        while True:
            # §7.5 requires ignoring CRLF before a start-line. One CRLF between
            # messages is the RFC 5626 pong; two in a row is the server's own ping and
            # must be answered with a single CRLF.
            crlfs = 0
            while self._buf[:2] == CRLF:
                del self._buf[:2]
                crlfs += 1
            if crlfs >= 2:
                yield Ping()
            elif crlfs == 1:
                yield Pong()
            while self._buf[:1] == b"\n":  # lenient: a bare LF
                del self._buf[:1]
            if not self._buf:
                return

            end = self._buf.find(b"\r\n\r\n")
            if end == -1:
                if len(self._buf) > MAX_HEADER_BYTES:
                    raise SipFramingError("header block too large")
                return

            header_bytes = bytes(self._buf[:end])
            header_len = end + 4

            # Content-Length is a MUST over stream transports (§18.3).
            message = parse_message(header_bytes)
            declared = message.first("content-length")
            if declared is None:
                self._warn("message without Content-Length; assuming 0")
                length = 0
            else:
                try:
                    length = int(declared.value)
                except ValueError as err:
                    raise SipFramingError("bad Content-Length") from err
            if not 0 <= length <= MAX_BODY_BYTES:
                raise SipFramingError(f"Content-Length {length} out of range")

            # Whole message present? If not, consume nothing at all.
            if len(self._buf) < header_len + length:
                return

            # Exactly one message leaves the buffer. The body is dropped unread.
            del self._buf[: header_len + length]
            yield message


class MessageBuilder:
    """The only place a SIP message becomes bytes."""

    @staticmethod
    def response(
        request: SipMessage,
        code: int,
        reason: str,
        *,
        to_tag: str | None = None,
        extra: Sequence[tuple[str, str]] = (),
    ) -> bytes:
        """Build a response to a request.

        There is deliberately no body parameter: this integration answers signalling
        and never negotiates media, so it has nothing to put in one.
        """
        # Home Assistant must never take the call away from the resident's phone.
        if request.method == "INVITE" and 200 <= code < 300:
            raise SipSafetyError("never answer an INVITE with a 2xx")
        # A 6xx is a GLOBAL decline: §16.7 step 5 makes the proxy cancel every other
        # branch, the resident's phone included. There is no legitimate use for one
        # here, so there is no way to ask for one.
        if code >= 600:
            raise SipSafetyError(f"never send {code}: a 6xx kills every branch")
        if not 100 <= code < 600:
            raise SipSafetyError(f"nonsense status {code}")

        out = bytearray(f"SIP/2.0 {code} {reason}".encode("latin-1") + CRLF)

        for name in _ECHO:
            for raw in request.all_raw(name):
                out += raw + CRLF
        if code >= 180:
            for raw in request.all_raw("record-route"):
                out += raw + CRLF

        to = request.first("to")
        if to is None:
            raise SipFramingError("request has no To header")
        to_raw = to.raw
        # §17.2.1 downgrades a tag on a 100 to SHOULD NOT; §8.2.6.2 makes it a MUST
        # everywhere else, with the SAME tag on every response to that request.
        if to_tag and code != 100 and not has_header_param(to.value, "tag"):
            to_raw = to_raw + f";tag={to_tag}".encode("latin-1")
        out += to_raw + CRLF

        for raw in request.all_raw("call-id"):
            out += raw + CRLF
        # CSeq echoed verbatim, so the 200 to a CANCEL carries "N CANCEL" -- the single
        # most commonly mis-built message in SIP.
        for raw in request.all_raw("cseq"):
            out += raw + CRLF

        for name, value in extra:
            out += f"{name}: {value}".encode("latin-1") + CRLF

        out += b"Content-Length: 0" + CRLF + CRLF
        return bytes(out)

    @staticmethod
    def register(
        state: RegistrationState,
        *,
        contacts: Sequence[str],
        expires: int | None,
        auth: Sequence[str] = (),
        cseq: int,
        branch: str,
    ) -> bytes:
        """Build a REGISTER.

        ``contacts=(), expires=None`` is the non-destructive binding probe.
        """
        for contact in contacts:
            # Checked with a raise, not an assert: `python -O` strips asserts, and a
            # wildcard Contact with Expires: 0 wipes every binding on the account.
            if contact.strip() == "*":
                raise SipSafetyError("never build a wildcard Contact")
        if not contacts and expires is not None:
            raise SipSafetyError("a Contact-less probe must not carry Expires")
        if not state.sent_by:
            raise SipSafetyError("sent_by is unset: refusing to emit a bare Via")

        lines = [
            f"REGISTER {state.registrar_uri} SIP/2.0",
            f"Via: SIP/2.0/TCP {state.sent_by};rport;branch={branch}",
            "Max-Forwards: 70",
            f"From: <{state.aor}>;tag={state.from_tag}",
            f"To: <{state.aor}>",
            f"Call-ID: {state.call_id}",
            f"CSeq: {cseq} REGISTER",
            *auth,
            *(f"Contact: {contact}" for contact in contacts),
            *([f"Expires: {expires}"] if expires is not None else []),
            "Supported: outbound, path",  # advertise, never Require
            f"Allow: {ALLOW}",
            f"User-Agent: {USER_AGENT}",
            "Content-Length: 0",
            "",
            "",
        ]
        return CRLF.join(line.encode("latin-1") for line in lines)
