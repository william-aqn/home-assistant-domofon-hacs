"""Pure protocol rules for Loki.

Deliberately free of Home Assistant and aiohttp imports: these are the details most
likely to be got wrong and most cheaply covered by tests.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final

# Defaults chosen so the composed header is byte-identical to what the official client
# was observed to send. They are ordinary device properties, not magic.
DEFAULT_DALVIK_VERSION: Final = "2.1.0"
DEFAULT_ANDROID_RELEASE: Final = "11"
DEFAULT_DEVICE_MODEL: Final = "sdk_gphone_x86"
DEFAULT_BUILD_ID: Final = "RSR1.201013.001"


def build_user_agent(
    *,
    dalvik_version: str = DEFAULT_DALVIK_VERSION,
    android_release: str = DEFAULT_ANDROID_RELEASE,
    device_model: str = DEFAULT_DEVICE_MODEL,
    build_id: str = DEFAULT_BUILD_ID,
) -> str:
    """Compose the User-Agent the way Android composes it.

    The official client never sets this header itself. Android's HttpURLConnection
    sends the platform value from the ``http.agent`` system property, which libcore
    assembles from the runtime version and three build properties:
    ``Build.VERSION.RELEASE``, ``Build.MODEL`` and ``Build.ID``. Reproducing that
    assembly rather than pasting the resulting string keeps it obvious where each
    field comes from -- and makes it adjustable if the backend ever starts caring
    which client it is talking to.

    The "Linux; U;" pair is fixed in the platform template and carries no meaning; it
    is a fossil of the original Mozilla product-comment convention.
    """
    return (
        f"Dalvik/{dalvik_version} "
        f"(Linux; U; Android {android_release}; {device_model} Build/{build_id})"
    )


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
