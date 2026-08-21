"""Tests for the pure protocol rules."""

from __future__ import annotations

import pytest

from custom_components.loki.protocol import (
    auth_hash,
    build_user_agent,
    normalize_phone,
)


def test_auth_hash_is_md5_of_token_then_code() -> None:
    """Locks in the rule: MD5 of the provisional token concatenated with the SMS code.

    A synthetic token/code pair is used deliberately -- a real captured token plus its
    hash would let anyone brute-force the (six-digit) SMS code in under a second, which
    is exactly the kind of thing that must never live in a public repository.
    """
    assert auth_hash("provisional.jwt.sample", "000000") == (
        "bc3e606072712dcc3daa941a3178cf93"
    )


def test_auth_hash_is_plain_concatenation() -> None:
    """No separator, no salt -- the code is appended directly to the token."""
    assert auth_hash("abc", "123") == auth_hash("abc1", "23")


def test_user_agent_matches_the_observed_android_header() -> None:
    """The composed default must be byte-identical to what the client really sent."""
    assert build_user_agent() == (
        "Dalvik/2.1.0 (Linux; U; Android 11; sdk_gphone_x86 Build/RSR1.201013.001)"
    )


def test_user_agent_is_composed_from_device_properties() -> None:
    """Each field is a real Android build property, not part of a fixed string."""
    agent = build_user_agent(
        dalvik_version="2.1.0",
        android_release="14",
        device_model="Pixel 8",
        build_id="UQ1A.240205.004",
    )

    assert agent == (
        "Dalvik/2.1.0 (Linux; U; Android 14; Pixel 8 Build/UQ1A.240205.004)"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "+79991234567",
        "79991234567",
        "89991234567",
        "9991234567",
        "8 (999) 123-45-67",
        "+7 999 123 45 67",
        "  +7-999-123-45-67  ",
    ],
)
def test_normalize_phone_accepts_the_shapes_people_type(raw: str) -> None:
    """All common Russian input forms collapse to the backend's format."""
    assert normalize_phone(raw) == "+79991234567"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        None,
        "12345",
        "999123456",
        "899912345678",
        "not a phone",
        "+1 555 0100",
    ],
)
def test_normalize_phone_rejects_what_it_cannot_read(raw: str | None) -> None:
    """Anything but a ten-digit Russian number is refused rather than guessed."""
    assert normalize_phone(raw) is None
