"""Tests for the pure protocol rules."""

from __future__ import annotations

import pytest

from custom_components.loki.protocol import auth_hash, normalize_phone


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
