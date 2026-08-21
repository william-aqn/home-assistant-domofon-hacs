"""SIP framing, parsing and serialisation.

The safety tests here are the point of the module: the rules that protect the
resident's phone live in the builder, so they are proven by trying to violate them.
"""

from __future__ import annotations

import pytest

from custom_components.loki.sip.errors import SipFramingError, SipSafetyError
from custom_components.loki.sip.messages import (
    MessageBuilder,
    Ping,
    Pong,
    SipMessage,
    StreamFramer,
    parse_message,
)
from custom_components.loki.sip.registration import RegistrationState

INVITE = (
    b"INVITE sip:1001@example SIP/2.0\r\n"
    b"Via: SIP/2.0/TCP 203.0.113.1:5060;branch=z9hG4bKaaa;received=198.51.100.7\r\n"
    b"Max-Forwards: 70\r\n"
    b'From: "\xd0\x9f\xd0\xbe\xd0\xb4\xd1\x8a\xd0\xb5\xd0\xb7\xd0\xb4 1" '
    b"<sip:1002@example>;tag=abc\r\n"
    b"To: <sip:1001@example>\r\n"
    b"Call-ID: call-one\r\n"
    b"CSeq: 314159 INVITE\r\n"
    b"Contact: <sip:1002@203.0.113.1>\r\n"
    b"Content-Length: 0\r\n"
    b"\r\n"
)


def _parse(raw: bytes) -> SipMessage:
    return parse_message(raw.split(b"\r\n\r\n")[0])


# --------------------------------------------------------------------- parsing


def test_parses_a_request() -> None:
    """Method, request-URI and headers come out of the start line and rows."""
    message = _parse(INVITE)

    assert message.is_response is False
    assert message.method == "INVITE"
    assert message.request_uri == "sip:1001@example"
    assert message.call_id == "call-one"
    assert message.cseq == (314159, "INVITE")


def test_compact_header_forms_are_expanded() -> None:
    """RFC 3261 §7.3.3: accepting every compact form is a MUST for a receiver."""
    raw = b"SIP/2.0 200 OK\r\ni: xyz\r\nv: SIP/2.0/TCP h;branch=z9hG4bK1\r\nl: 0\r\n"

    message = parse_message(raw)

    assert message.call_id == "xyz"
    assert message.first("via") is not None
    assert message.first("content-length") is not None


def test_folded_headers_are_unfolded() -> None:
    """§7.3.1: a line starting with whitespace continues the previous header."""
    raw = (
        b"SIP/2.0 200 OK\r\nContact: <sip:a@b>,\r\n <sip:c@d>\r\nContent-Length: 0\r\n"
    )

    message = parse_message(raw)

    assert message.value("contact") == "<sip:a@b>, <sip:c@d>"


def test_header_text_decodes_utf8_while_value_stays_byte_exact() -> None:
    """The display name must survive as UTF-8, or door lookup fails on every call."""
    message = _parse(INVITE)
    from_header = message.first("from")

    assert from_header is not None
    assert "Подъезд 1" in from_header.text()
    # ``value`` is a latin-1 decode so every byte round-trips for parsing; it is
    # mojibake by design and must never be shown or sent onward.
    assert "Подъезд 1" not in from_header.value


def test_unparseable_start_line_raises() -> None:
    """Rather than guess, the caller tears the connection down."""
    with pytest.raises(SipFramingError):
        parse_message(b"this is not sip\r\nContent-Length: 0\r\n")


# --------------------------------------------------------------------- framing


def test_framer_yields_one_message_per_complete_block() -> None:
    """Two messages arriving in one read are two messages."""
    framer = StreamFramer()

    out = list(framer.feed(INVITE + INVITE))

    assert [type(item) for item in out] == [SipMessage, SipMessage]


def test_framer_consumes_nothing_until_a_message_is_whole() -> None:
    """A partial message must not be parsed, and must not be lost."""
    framer = StreamFramer()

    assert list(framer.feed(INVITE[:40])) == []
    out = list(framer.feed(INVITE[40:]))

    assert len(out) == 1
    assert isinstance(out[0], SipMessage)


def test_framer_recognises_keepalive_ping_and_pong() -> None:
    """RFC 5626 §4.4.1: one CRLF is a pong, two is a ping we must answer."""
    framer = StreamFramer()

    assert [type(item) for item in framer.feed(b"\r\n")] == [Pong]
    assert [type(item) for item in framer.feed(b"\r\n\r\n")] == [Ping]


def test_framer_rejects_an_absurd_content_length() -> None:
    """A stream claiming a huge body is not a SIP peer."""
    framer = StreamFramer()

    with pytest.raises(SipFramingError):
        list(framer.feed(b"SIP/2.0 200 OK\r\nContent-Length: 999999999\r\n\r\n"))


def test_framer_skips_a_body_without_reading_it() -> None:
    """Bodies are framed on Content-Length and then dropped: we read no SDP."""
    with_body = (
        b"SIP/2.0 200 OK\r\nCall-ID: x\r\nCSeq: 1 INVITE\r\n"
        b"Content-Length: 5\r\n\r\nhello"
    )
    framer = StreamFramer()

    out = list(framer.feed(with_body + INVITE))

    assert len(out) == 2
    assert out[1].method == "INVITE"


# ---------------------------------------------------------------------- safety


def test_never_answers_an_invite_with_a_2xx() -> None:
    """Answering would take the call away from the resident's phone."""
    request = _parse(INVITE)

    with pytest.raises(SipSafetyError, match="2xx"):
        MessageBuilder.response(request, 200, "OK")


def test_a_2xx_to_other_methods_is_still_allowed() -> None:
    """The rule is method-specific: a 200 to CANCEL or BYE is normal and required."""
    cancel = _parse(INVITE.replace(b"INVITE sip:", b"CANCEL sip:", 1))

    out = MessageBuilder.response(cancel, 200, "OK")

    assert out.startswith(b"SIP/2.0 200 OK\r\n")


@pytest.mark.parametrize("code", [600, 603, 604, 606])
def test_never_sends_a_6xx(code: int) -> None:
    """A 6xx is a global decline: §16.7 makes the proxy kill every other branch."""
    request = _parse(INVITE)

    with pytest.raises(SipSafetyError, match="6xx"):
        MessageBuilder.response(request, code, "Decline")


def test_never_builds_a_wildcard_contact() -> None:
    """Contact: * with Expires: 0 wipes every binding on the account at once."""
    state = RegistrationState(host="h", user="u", sent_by="203.0.113.1:5060")

    with pytest.raises(SipSafetyError, match="wildcard"):
        MessageBuilder.register(
            state, contacts=["*"], expires=0, cseq=1, branch="z9hG4bK1"
        )


def test_probe_must_not_carry_expires() -> None:
    """A Contact-less REGISTER with Expires is no longer a read-only query."""
    state = RegistrationState(host="h", user="u", sent_by="203.0.113.1:5060")

    with pytest.raises(SipSafetyError, match="Expires"):
        MessageBuilder.register(
            state, contacts=[], expires=300, cseq=1, branch="z9hG4bK1"
        )


def test_refuses_to_emit_a_via_without_sent_by() -> None:
    """A bare Via names nowhere to answer, and the message is unroutable."""
    state = RegistrationState(host="h", user="u")

    with pytest.raises(SipSafetyError, match="sent_by"):
        MessageBuilder.register(
            state, contacts=[], expires=None, cseq=1, branch="z9hG4bK1"
        )


# -------------------------------------------------------------------- responses


def test_response_echoes_via_and_from_byte_for_byte() -> None:
    """Rebuilding them is how received/rport and quoting get corrupted."""
    request = _parse(INVITE)

    out = MessageBuilder.response(request, 180, "Ringing", to_tag="zzz")

    expected = (
        b"Via: SIP/2.0/TCP 203.0.113.1:5060;branch=z9hG4bKaaa;received=198.51.100.7"
    )
    assert expected in out
    assert b'From: "\xd0\x9f\xd0\xbe\xd0\xb4\xd1\x8a\xd0\xb5\xd0\xb7\xd0\xb4 1" ' in out


def test_response_adds_a_to_tag_except_on_a_100() -> None:
    """§8.2.6.2 requires one everywhere but §17.2.1 exempts the 100."""
    request = _parse(INVITE)

    trying = MessageBuilder.response(request, 100, "Trying", to_tag="zzz")
    ringing = MessageBuilder.response(request, 180, "Ringing", to_tag="zzz")

    assert b";tag=zzz" not in trying
    assert b"To: <sip:1001@example>;tag=zzz" in ringing


def test_response_never_adds_a_second_to_tag() -> None:
    """A request that already carries one keeps it; two tags match no transaction."""
    tagged = _parse(
        INVITE.replace(b"To: <sip:1001@example>", b"To: <sip:1001@x>;tag=q")
    )

    out = MessageBuilder.response(tagged, 486, "Busy Here", to_tag="zzz")

    assert out.count(b"tag=") == 2  # the From tag and the original To tag
    assert b"tag=zzz" not in out


def test_cseq_is_echoed_so_a_cancel_response_says_cancel() -> None:
    """The single most commonly mis-built message in SIP."""
    cancel = _parse(
        INVITE.replace(b"INVITE sip:", b"CANCEL sip:", 1).replace(
            b"CSeq: 314159 INVITE", b"CSeq: 314159 CANCEL"
        )
    )

    out = MessageBuilder.response(cancel, 200, "OK")

    assert b"CSeq: 314159 CANCEL" in out


def test_response_always_declares_a_zero_length_body() -> None:
    """Content-Length is mandatory over TCP, and we never send a body."""
    request = _parse(INVITE)

    out = MessageBuilder.response(request, 180, "Ringing", to_tag="t")

    assert out.endswith(b"Content-Length: 0\r\n\r\n")


def test_register_probe_has_no_contact_header_at_all() -> None:
    """The read-only form: anything else would modify the binding table."""
    state = RegistrationState(host="h", user="u", sent_by="203.0.113.1:5060")

    out = MessageBuilder.register(
        state, contacts=[], expires=None, cseq=7, branch="z9hG4bKq"
    )

    assert b"Contact" not in out
    assert b"Expires" not in out
    assert b"CSeq: 7 REGISTER" in out


def test_register_withdrawal_names_only_our_own_contact() -> None:
    """Withdrawal is per-binding; nothing else on the account is touched."""
    state = RegistrationState(host="h", user="u", sent_by="203.0.113.1:5060")

    out = MessageBuilder.register(
        state,
        contacts=[state.contact(expires=0)],
        expires=0,
        cseq=2,
        branch="z9hG4bK2",
    )

    assert b"expires=0" in out
    assert b"Contact: *" not in out
    assert state.instance_id.encode() in out
