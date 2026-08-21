"""Digest authentication and URI handling.

The digest expectations are computed from the RFC formulas rather than copied from a
capture, so a refactor that quietly changes the algorithm fails here instead of
failing against a live registrar with a rate limiter behind it.
"""

from __future__ import annotations

import hashlib

import pytest

from custom_components.loki.sip.digest import DigestChallenge, challenges_from
from custom_components.loki.sip.errors import SipPermanentError
from custom_components.loki.sip.uri import (
    display_name,
    has_header_param,
    name_addr,
    parse_params,
    parse_uri,
    split_commas,
    split_semis,
)

USER = "1001543"
PASSWORD = "s3cret"
URI = "sip:registrar.example:5060;transport=tcp"


def _md5(text: str) -> str:
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()


def _fields(header: str) -> dict[str, str]:
    return parse_params(split_commas(header.split(" ", 1)[1]))


# --------------------------------------------------------------------- parsing


def test_parses_a_challenge() -> None:
    """Realm, nonce, algorithm and qop all come off the row."""
    challenge = DigestChallenge.parse(
        'Digest realm="asterisk", nonce="abc123", qop="auth", algorithm=MD5'
    )

    assert challenge is not None
    assert challenge.realm == "asterisk"
    assert challenge.nonce == "abc123"
    assert challenge.qop == "auth"
    assert challenge.proxy is False


def test_challenges_are_never_comma_merged() -> None:
    """§7.3.1 exempts these headers: a challenge is itself a comma-separated list."""
    rows = [
        'Digest realm="one", nonce="n1"',
        'Digest realm="two", nonce="n2"',
    ]

    parsed = challenges_from(rows)

    assert [item.realm for item in parsed] == ["one", "two"]


def test_non_digest_schemes_are_ignored() -> None:
    """Basic in a SIP challenge is not something we answer."""
    assert DigestChallenge.parse('Basic realm="x"') is None
    assert DigestChallenge.parse('Digest realm="x"') is None  # no nonce


# ------------------------------------------------------------------ responses


def test_response_without_qop_matches_the_rfc_formula() -> None:
    """RFC 2617: response = H(HA1:nonce:HA2)."""
    challenge = DigestChallenge(realm="asterisk", nonce="n0", algorithm="MD5")

    fields = _fields(challenge.header(USER, PASSWORD, "REGISTER", URI))

    ha1 = _md5(f"{USER}:asterisk:{PASSWORD}")
    ha2 = _md5(f"REGISTER:{URI}")
    assert fields["response"] == _md5(f"{ha1}:n0:{ha2}")
    assert "qop" not in fields
    assert "cnonce" not in fields


def test_response_with_qop_auth_matches_the_rfc_formula() -> None:
    """RFC 2617 with qop: response = H(HA1:nonce:nc:cnonce:qop:HA2)."""
    challenge = DigestChallenge(realm="asterisk", nonce="n1", qop="auth")

    fields = _fields(challenge.header(USER, PASSWORD, "REGISTER", URI))

    ha1 = _md5(f"{USER}:asterisk:{PASSWORD}")
    ha2 = _md5(f"REGISTER:{URI}")
    expected = _md5(f"{ha1}:n1:{fields['nc']}:{fields['cnonce']}:auth:{ha2}")
    assert fields["response"] == expected
    assert fields["qop"] == "auth"
    assert fields["nc"] == "00000001"


def test_qop_list_offering_auth_int_still_selects_auth() -> None:
    """We implement auth only; auth-int would need the body we never send."""
    challenge = DigestChallenge(realm="r", nonce="n", qop="auth,auth-int")

    fields = _fields(challenge.header(USER, PASSWORD, "REGISTER", URI))

    assert fields["qop"] == "auth"


def test_nonce_counter_increments_per_nonce() -> None:
    """RFC 7616 §3.4.3: reusing a counter with one nonce is a replay to the server."""
    challenge = DigestChallenge(realm="r", nonce="n", qop="auth")

    first = _fields(challenge.header(USER, PASSWORD, "REGISTER", URI))
    second = _fields(challenge.header(USER, PASSWORD, "REGISTER", URI))

    assert first["nc"] == "00000001"
    assert second["nc"] == "00000002"


def test_md5_sess_folds_the_nonce_into_ha1_and_sends_the_cnonce() -> None:
    """-sess derives HA1 from the cnonce, so the cnonce must go out with it.

    RFC 2617 ties cnonce to qop, which leaves the qop-less -sess case unverifiable by
    the server unless it is sent anyway.
    """
    challenge = DigestChallenge(realm="r", nonce="n", algorithm="MD5-sess")

    fields = _fields(challenge.header(USER, PASSWORD, "REGISTER", URI))

    assert "cnonce" in fields, "the server cannot verify HA1 without it"
    base = _md5(f"{USER}:r:{PASSWORD}")
    ha1 = _md5(f"{base}:n:{fields['cnonce']}")
    assert fields["response"] == _md5(f"{ha1}:n:{_md5(f'REGISTER:{URI}')}")


def test_sha256_algorithm_is_supported() -> None:
    """RFC 7616 added it and some registrars now prefer it."""
    challenge = DigestChallenge(realm="r", nonce="n", algorithm="SHA-256")

    fields = _fields(challenge.header(USER, PASSWORD, "REGISTER", URI))

    assert len(fields["response"]) == 64


def test_unknown_algorithm_is_refused_rather_than_guessed() -> None:
    """Guessing would send a wrong response and burn an authentication attempt."""
    challenge = DigestChallenge(realm="r", nonce="n", algorithm="whirlpool")

    with pytest.raises(SipPermanentError, match="algorithm"):
        challenge.header(USER, PASSWORD, "REGISTER", URI)


def test_proxy_challenge_uses_the_proxy_header_name() -> None:
    """407 is answered with Proxy-Authorization, not Authorization."""
    challenge = DigestChallenge(realm="r", nonce="n", proxy=True)

    header = challenge.header(USER, PASSWORD, "REGISTER", URI)

    assert header.startswith("Proxy-Authorization: Digest ")


def test_quoted_values_are_escaped() -> None:
    """A realm containing a quote must not break out of the quoted-string."""
    challenge = DigestChallenge(realm='we"rd', nonce="n")

    header = challenge.header(USER, PASSWORD, "REGISTER", URI)

    assert r"we\"rd" in header


# --------------------------------------------------------------------- scanning


def test_split_respects_quotes_and_angle_brackets() -> None:
    """One scanner for every list; qop="auth,auth-int" must survive it."""
    assert split_commas('a="x,y", b=z') == ['a="x,y"', "b=z"]
    assert split_semis("<sip:a@b;p=1>;q=2") == ["<sip:a@b;p=1>", "q=2"]


def test_has_header_param_finds_an_existing_tag() -> None:
    """Used before adding a To tag, so a second one is never appended."""
    assert has_header_param("<sip:a@b>;tag=xyz", "tag") is True
    assert has_header_param("<sip:a@b>", "tag") is False


# -------------------------------------------------------------------------- uri


@pytest.mark.parametrize(
    ("raw", "user", "host", "port"),
    [
        ("sip:1001@example.net", "1001", "example.net", None),
        ("sip:1001@example.net:5060", "1001", "example.net", 5060),
        ("<sip:1001@example.net;transport=tcp>", "1001", "example.net", None),
        ('"Дверь" <sip:1001@example.net>', "1001", "example.net", None),
        ("sip:[2001:db8::1]:5060", "", "2001:db8::1", 5060),
    ],
)
def test_parse_uri(raw: str, user: str, host: str, port: int | None) -> None:
    """Angle brackets, display names, parameters and IPv6 literals."""
    uri = parse_uri(raw)

    assert uri is not None
    assert (uri.user, uri.host, uri.port) == (user, host, port)


def test_uri_equivalence_ignores_case_and_extra_parameters() -> None:
    """RFC 3261 §19.1.4 -- otherwise our own binding reads back as somebody else's."""
    first = parse_uri("sip:1001@Example.NET;transport=tcp")
    second = parse_uri("sip:1001@example.net;transport=TCP;ob")

    assert first is not None
    assert second is not None
    assert first.equivalent(second) is True


def test_uri_equivalence_is_case_sensitive_in_the_user_part() -> None:
    """The user part is the one component the RFC keeps case-sensitive."""
    first = parse_uri("sip:Alice@example.net")
    second = parse_uri("sip:alice@example.net")

    assert first is not None
    assert second is not None
    assert first.equivalent(second) is False


def test_display_name_survives_a_uri_without_angle_brackets() -> None:
    """The official client throws here; a bare sip: URI must not break door lookup."""
    assert display_name("sip:1001@example") == "sip:1001@example"
    assert display_name('"Подъезд 1" <sip:1001@example>') == "Подъезд 1"
    assert display_name("Gate <sip:1001@example>") == "Gate"


def test_name_addr_drops_the_from_tag() -> None:
    """The tag changes per call; the backend door lookup matches on the whole string."""
    raw = '"Подъезд 1" <sip:1001@example>;tag=abc-123'
    assert name_addr(raw) == '"Подъезд 1" <sip:1001@example>'


def test_name_addr_leaves_a_value_without_parameters_alone() -> None:
    value = '"Подъезд 1" <sip:1001@example>'
    assert name_addr(value) == value


def test_name_addr_keeps_a_semicolon_inside_the_display_name() -> None:
    """A quoted display name may contain anything, including the parameter separator."""
    raw = '"Вход; служебный" <sip:1001@example>;tag=zz'
    assert name_addr(raw) == '"Вход; служебный" <sip:1001@example>'


def test_name_addr_on_a_bracketless_uri_strips_header_parameters() -> None:
    """Without angle brackets every parameter is a header one (RFC 3261 §20.10)."""
    assert name_addr("sip:1001@example;tag=abc") == "sip:1001@example"


def test_name_addr_ignores_an_angle_bracket_inside_the_display_name() -> None:
    """A quoted display name may contain anything, and truncating there is fatal.

    The value goes straight to the backend door lookup, so a door whose name carries
    a ">" would be handed a mangled string and could never resolve -- meaning that
    door would never ring in Home Assistant.
    """
    raw = '"Вход -> двор" <sip:1001@example>;tag=zz'
    assert name_addr(raw) == '"Вход -> двор" <sip:1001@example>'
