"""HTTP Digest authentication as SIP uses it (RFC 3261 §22, RFC 7616).

The official client configures the realm as ``*`` -- accept whatever the server
challenges with -- so this does the same rather than pinning a realm it has never
seen.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import secrets
from typing import Any

from .errors import SipPermanentError
from .uri import parse_params, split_commas


def _md5(data: bytes = b"") -> Any:
    return hashlib.md5(data, usedforsecurity=False)


_ALGORITHMS = {
    "": _md5,
    "md5": _md5,
    "md5-sess": _md5,
    "sha-256": hashlib.sha256,
    "sha-256-sess": hashlib.sha256,
}


@dataclass
class DigestChallenge:
    """One parsed WWW-Authenticate or Proxy-Authenticate challenge."""

    realm: str
    nonce: str
    algorithm: str = "MD5"
    qop: str = ""
    opaque: str = ""
    stale: bool = False
    proxy: bool = False
    # RFC 7616 §3.4.3: the nonce counter is per nonce and restarts when it changes.
    nc: int = field(default=0, repr=False)

    @classmethod
    def parse(cls, row: str, *, proxy: bool = False) -> DigestChallenge | None:
        """Parse one challenge row, or None if it is not a Digest challenge."""
        if not row.lower().startswith("digest"):
            return None
        params = parse_params(split_commas(row.split(" ", 1)[1]))
        if not params.get("nonce"):
            return None
        return cls(
            realm=params.get("realm", ""),
            nonce=params["nonce"],
            algorithm=params.get("algorithm") or "MD5",
            qop=params.get("qop", ""),
            opaque=params.get("opaque", ""),
            stale=params.get("stale", "").lower() == "true",
            proxy=proxy,
        )

    @property
    def header_name(self) -> str:
        """Which credentials header answers this challenge."""
        return "Proxy-Authorization" if self.proxy else "Authorization"

    def header(self, user: str, password: str, method: str, uri: str) -> str:
        """Build the credentials header answering this challenge."""
        algorithm = self.algorithm.lower()
        factory = _ALGORITHMS.get(algorithm)
        if factory is None:
            raise SipPermanentError(f"unsupported digest algorithm {self.algorithm!r}")

        def h(text: str) -> str:
            return factory(text.encode("utf-8")).hexdigest()

        cnonce = secrets.token_hex(8)
        ha1 = h(f"{user}:{self.realm}:{password}")
        if algorithm.endswith("-sess"):
            ha1 = h(f"{ha1}:{self.nonce}:{cnonce}")
        ha2 = h(f"{method}:{uri}")

        quoted = [
            ("username", user),
            ("realm", self.realm),
            ("nonce", self.nonce),
            ("uri", uri),
        ]

        offered = [item.strip() for item in self.qop.split(",")]
        if "auth" in offered:
            # auth-int is deliberately not offered back: it hashes the body, and this
            # integration never sends one.
            self.nc += 1
            counter = f"{self.nc:08x}"
            quoted.append(
                ("response", h(f"{ha1}:{self.nonce}:{counter}:{cnonce}:auth:{ha2}"))
            )
            quoted.append(("cnonce", cnonce))
            bare = [("algorithm", self.algorithm), ("qop", "auth"), ("nc", counter)]
        else:
            quoted.append(("response", h(f"{ha1}:{self.nonce}:{ha2}")))
            if algorithm.endswith("-sess"):
                # HA1 was derived from this cnonce, so the server cannot verify the
                # response without it. RFC 2617 ties cnonce to qop, which leaves the
                # qop-less -sess case unverifiable unless it is sent anyway.
                quoted.append(("cnonce", cnonce))
            bare = [("algorithm", self.algorithm)]

        if self.opaque:
            quoted.append(("opaque", self.opaque))

        body = ", ".join(
            [f'{key}="{_escape(value)}"' for key, value in quoted]
            + [f"{key}={value}" for key, value in bare]
        )
        return f"{self.header_name}: Digest {body}"


def _escape(value: str) -> str:
    """Escape a quoted-string value."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def challenges_from(rows: list[str], *, proxy: bool = False) -> list[DigestChallenge]:
    """Parse every Digest challenge in a set of header rows.

    The rows are never comma-merged: §7.3.1 exempts these headers precisely because a
    challenge is itself a comma-separated list, and merging two realms into one row
    makes both unparseable.
    """
    parsed = [DigestChallenge.parse(row, proxy=proxy) for row in rows]
    return [challenge for challenge in parsed if challenge is not None]
