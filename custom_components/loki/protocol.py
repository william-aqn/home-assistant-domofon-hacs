"""Pure protocol rules for Loki.

Deliberately free of Home Assistant and aiohttp imports: these are the details most
likely to be got wrong and most cheaply covered by tests.
"""

from __future__ import annotations

import hashlib
import re


def auth_hash(provisional_token: str, sms_code: str) -> str:
    """Return the login hash: MD5 of the provisional token concatenated with the code.

    MD5 is what the protocol specifies; it is not a security choice of ours.
    """
    digest = hashlib.md5(  # noqa: S324 - mandated by the backend
        f"{provisional_token}{sms_code}".encode()
    )
    return digest.hexdigest()


def normalize_phone(raw: str | None) -> str | None:
    """Normalise user input to the ``+7XXXXXXXXXX`` form the backend expects.

    Accepts the shapes people actually type -- ``8 (999) 123-45-67``, ``+7 999 …``,
    ``9991234567`` -- and returns None if the input cannot be read as one.
    """
    # [^0-9] rather than \D: \D is Unicode-aware, so Arabic-Indic and full-width
    # digits would survive and produce an unresolvable unique_id like +7٩٩٩١٢٣٤٥٦٧.
    digits = re.sub(r"[^0-9]", "", raw or "")

    if len(digits) == 11 and digits[0] in ("7", "8"):
        digits = digits[1:]

    if len(digits) != 10:
        return None
    return f"+7{digits}"
