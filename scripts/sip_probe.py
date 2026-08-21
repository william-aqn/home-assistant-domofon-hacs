#!/usr/bin/env python3
"""Non-destructive SIP binding probe.

Answers "can this account safely run SIP?" before a single line of the client exists.

It sends a REGISTER with NO Contact header (RFC 3261 §10.2.3). The registrar returns
the address-of-record's complete binding list and, per §10.3 step 6, "skips to the last
step" -- the steps that add, update or remove bindings never run, and the per-binding
Call-ID/CSeq records are not touched. Nothing changes.

That matters because the intercom account is a single AOR that the resident's phone is
already registered to. If our registration were to evict it, the phone would not
notice: it re-registers only when its own timer fires, which with PJSIP defaults means
up to ~295 seconds with a doorbell that silently does not ring.

``--policy-test`` goes further -- it briefly registers, re-probes, then withdraws its
own contact. It is the only way to learn whether the registrar enforces one contact per
AOR, and it carries exactly the risk described above. Hence the acknowledgement flag,
the 60-second Expires, and the refusal to run when it would teach us nothing.

This script never emits a 6xx response and never builds a wildcard ``Contact: *``; the
only Contact rows it can produce are URIs it generated itself.

Usage:
  python scripts/sip_probe.py --url sip.example.net --user 1001543 --password ***
  python scripts/sip_probe.py --from-ha config/.storage/core.config_entries
  python scripts/sip_probe.py ... --json
  python scripts/sip_probe.py ... --policy-test
      --i-understand-this-may-break-the-doorbell
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import re
import secrets
import socket
import sys
from typing import Any
import uuid

CRLF = "\r\n"
ALLOW = "INVITE, ACK, CANCEL, BYE, OPTIONS"
UA = "loki-sip-probe/1.0"
TIMEOUT = 32.0  # RFC 3261 Timer F = 64 * T1
MAX_HEADER = 16384
MAX_BODY = 65536
DEFAULT_PORT = 5060

EXIT_SAFE = 0
EXIT_CAUTION = 2
EXIT_UNSAFE = 3
EXIT_AUTH = 4
EXIT_REFUSED = 5
EXIT_TRANSPORT = 6
EXIT_ODD = 7

# pjsua derives its instance-id from a hash of the hostname rather than a UUID:
# 26 zeros followed by 8 hex digits. On Android gethostname() is very often
# "localhost", so two phones can even share one. A binding of this shape is a PJSIP
# client -- which is what the official intercom app is.
PJSUA_SHAPE = re.compile(r"^0{8}-0000-0000-0000-0000[0-9a-f]{8}$", re.I)


def _md5(data: bytes = b"") -> Any:
    return hashlib.md5(data, usedforsecurity=False)


_HASHES = {
    "": _md5,
    "md5": _md5,
    "md5-sess": _md5,
    "sha-256": hashlib.sha256,
    "sha-256-sess": hashlib.sha256,
}


class ProbeError(Exception):
    """Something went wrong that is worth reporting rather than raising."""


# --------------------------------------------------------------------- scanning


def _split(value: str, sep: str) -> list[str]:
    """Split on a separator that is outside quotes and outside angle brackets.

    One scanner for every list in SIP. Two separate ones is how ``qop="auth,auth-int"``
    ends up shredded in one place and intact in another.
    """
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
        elif char == sep and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)

    parts.append("".join(current).strip())
    return [part for part in parts if part]


def split_commas(value: str) -> list[str]:
    """Split a comma-separated header row or parameter list."""
    return _split(value, ",")


def split_semis(value: str) -> list[str]:
    """Split Via or URI parameters."""
    return _split(value, ";")


def kv(items: list[str]) -> dict[str, str]:
    """Parse ``key=value`` items, tolerating servers that quote things they need not."""
    out: dict[str, str] = {}
    for item in items:
        key, _, value = item.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        out[key.strip().lower()] = value
    return out


def parse_contact(row: str) -> tuple[str, dict[str, str]]:
    """Parse one Contact value into its URI and parameters.

    Handles both ``<sip:x@y>;expires=60`` and the equally legal bracket-less
    ``sip:x@y;expires=60``. Getting this wrong drops every parameter and would report
    a perfectly healthy binding as somebody else's.
    """
    if "<" in row and ">" in row:
        start, end = row.index("<"), row.index(">")
        return row[start + 1 : end].strip(), kv(split_semis(row[end + 1 :]))
    pieces = split_semis(row)
    return pieces[0], kv(pieces[1:])


def _unfold(block: str) -> list[str]:
    """RFC 3261 §7.3.1: a line starting with SP or HTAB continues the previous one."""
    rows: list[str] = []
    for line in block.split(CRLF):
        if line[:1] in (" ", "\t") and rows:
            rows[-1] += " " + line.strip()
        else:
            rows.append(line)
    return [row for row in rows if row]


# -------------------------------------------------------------------- transport


class Wire:
    """One TCP connection to the registrar, framing exactly one message at a time."""

    def __init__(self, host: str, port: int) -> None:
        """Connect and remember the local address for our Via and Contact."""
        self.sock = socket.create_connection((host, port), timeout=TIMEOUT)
        self.sock.settimeout(TIMEOUT)
        host_part, port_part = self.sock.getsockname()[:2]
        self.sent_by = (
            f"[{host_part}]:{port_part}"
            if ":" in host_part
            else f"{host_part}:{port_part}"
        )
        self.buf = b""

    def send(self, text: str) -> None:
        """Write one complete message."""
        self.sock.sendall(text.encode("latin-1"))

    def recv(self) -> tuple[int, dict[str, list[str]]]:
        """Read one response, framed on Content-Length (§18.3). Never resynchronise."""
        while True:
            # A bare CRLF is a keepalive pong (RFC 5626 §4.4.1), not a message.
            while self.buf[:2] == b"\r\n":
                self.buf = self.buf[2:]

            end = self.buf.find(b"\r\n\r\n")
            if end != -1:
                rows = _unfold(self.buf[:end].decode("latin-1"))
                headers: dict[str, list[str]] = {}
                for row in rows[1:]:
                    name, sep, value = row.partition(":")
                    if sep:
                        headers.setdefault(name.strip().lower(), []).append(
                            value.strip()
                        )
                raw_len = (headers.get("content-length") or headers.get("l") or ["0"])[
                    0
                ]
                length = int(raw_len or 0)
                if length > MAX_BODY:
                    raise ProbeError("absurd Content-Length")
                if len(self.buf) >= end + 4 + length:
                    self.buf = self.buf[end + 4 + length :]
                    return int(rows[0].split()[1]), headers
            elif len(self.buf) > MAX_HEADER:
                raise ProbeError("header block too large")

            chunk = self.sock.recv(65536)
            if not chunk:
                raise ProbeError("registrar closed the connection")
            self.buf += chunk

    def close(self) -> None:
        """Close, ignoring a socket that is already gone."""
        with contextlib.suppress(OSError):
            self.sock.close()


# ---------------------------------------------------------------------- session


class Session:
    """A REGISTER dialogue with one registrar."""

    def __init__(
        self, wire: Wire, url: str, port: int, user: str, password: str
    ) -> None:
        """Initialise the registration identity, constant for this session."""
        self.w = wire
        self.user = user
        self.password = password
        self.registrar_uri = f"sip:{url}:{port};transport=tcp"
        self.aor = f"sip:{user}@{url}"
        self.call_id = secrets.token_hex(16)
        self.from_tag = secrets.token_hex(5)
        self.instance = str(uuid.uuid4())
        self.cseq = 0
        self.challenge: dict[str, str] | None = None
        self.nc = 0
        self.seen_nonces: set[str] = set()

    def contact_uri(self) -> str:
        """The only Contact URI this script is ever allowed to name."""
        return f"sip:{self.user}@{self.w.sent_by};transport=tcp"

    def _auth_header(self) -> str:
        """Build the Digest credentials for the current challenge."""
        challenge = self.challenge or {}
        algorithm = challenge.get("algorithm", "").lower()
        factory = _HASHES.get(algorithm)
        if factory is None:
            raise ProbeError(
                f"unsupported digest algorithm {algorithm!r} -- report this"
            )

        def h(text: str) -> str:
            return factory(text.encode("utf-8")).hexdigest()

        realm = challenge.get("realm", "")
        nonce = challenge.get("nonce", "")
        cnonce = secrets.token_hex(8)

        ha1 = h(f"{self.user}:{realm}:{self.password}")
        if algorithm.endswith("-sess"):
            ha1 = h(f"{ha1}:{nonce}:{cnonce}")
        ha2 = h(f"REGISTER:{self.registrar_uri}")

        qop_offered = [item.strip() for item in challenge.get("qop", "").split(",")]
        quoted = [
            ("username", self.user),
            ("realm", realm),
            ("nonce", nonce),
            ("uri", self.registrar_uri),
        ]

        if "auth" in qop_offered:
            # RFC 7616 §3.4.3: the counter is per nonce, and is reset when it changes.
            self.nc += 1
            counter = f"{self.nc:08x}"
            quoted.append(
                ("response", h(f"{ha1}:{nonce}:{counter}:{cnonce}:auth:{ha2}"))
            )
            quoted.append(("cnonce", cnonce))
            bare = [
                ("algorithm", challenge.get("algorithm") or "MD5"),
                ("qop", "auth"),
                ("nc", counter),
            ]
        else:
            quoted.append(("response", h(f"{ha1}:{nonce}:{ha2}")))
            bare = [("algorithm", challenge.get("algorithm") or "MD5")]

        if challenge.get("opaque"):
            quoted.append(("opaque", challenge["opaque"]))

        def esc(value: str) -> str:
            return value.replace("\\", "\\\\").replace('"', '\\"')

        name = (
            "Proxy-Authorization" if challenge.get("_proxy") == "1" else "Authorization"
        )
        body = ", ".join(
            [f'{key}="{esc(value)}"' for key, value in quoted]
            + [f"{key}={value}" for key, value in bare]
        )
        return f"{name}: Digest {body}"

    def register(
        self, *, contacts: list[str], expires: int | None
    ) -> tuple[int, dict[str, list[str]]]:
        """Send a REGISTER, answering one authentication challenge.

        An empty ``contacts`` list is the probe itself: it must produce a request with
        no Contact header at all.
        """
        for wildcard in contacts:
            # Checked with a raise rather than an assert: `python -O` strips asserts,
            # and a wildcard Contact would wipe every binding on the AOR at once.
            if wildcard.strip() == "*":
                raise ProbeError("refusing to build a wildcard Contact")

        status = 0
        headers: dict[str, list[str]] = {}

        for attempt in (1, 2):
            self.cseq += 1  # §22.2: credentials always go on a fresh CSeq.
            rows = [
                f"REGISTER {self.registrar_uri} SIP/2.0",
                f"Via: SIP/2.0/TCP {self.w.sent_by};rport;"
                f"branch=z9hG4bK{secrets.token_hex(8)}",
                "Max-Forwards: 70",
                f"From: <{self.aor}>;tag={self.from_tag}",
                f"To: <{self.aor}>",
                f"Call-ID: {self.call_id}",
                f"CSeq: {self.cseq} REGISTER",
            ]
            if self.challenge is not None:
                rows.append(self._auth_header())
            rows += [f"Contact: {contact}" for contact in contacts]
            if expires is not None:
                rows.append(f"Expires: {expires}")
            rows += [
                "Supported: outbound, path",
                f"Allow: {ALLOW}",
                f"User-Agent: {UA}",
                "Content-Length: 0",
                "",
                "",
            ]
            self.w.send(CRLF.join(rows))

            while True:
                status, headers = self.w.recv()
                if status >= 200:
                    break  # a 100 to a REGISTER is legal, just uninteresting

            if status not in (401, 407) or attempt == 2:
                return status, headers

            proxy = status == 407
            name = "proxy-authenticate" if proxy else "www-authenticate"
            # Never comma-merge these rows (§7.3.1 exemption): one row per realm.
            offers = [
                row for row in headers.get(name, []) if row.lower().startswith("digest")
            ]
            if not offers:
                return status, headers

            parsed = kv(split_commas(offers[0].split(" ", 1)[1]))
            nonce = parsed.get("nonce", "")
            if nonce in self.seen_nonces and parsed.get("stale", "").lower() != "true":
                # The server replayed a nonce we already answered: the credentials are
                # wrong. Retrying is how an IP gets banned.
                return status, headers
            if nonce not in self.seen_nonces:
                self.nc = 0
            self.seen_nonces.add(nonce)
            parsed["_proxy"] = "1" if proxy else "0"
            self.challenge = parsed

        return status, headers


def bindings(headers: dict[str, list[str]]) -> list[dict[str, Any]]:
    """Parse every Contact the registrar reported into a structured binding."""
    out: list[dict[str, Any]] = []
    for row in headers.get("contact", []) + headers.get("m", []):
        for item in split_commas(row):
            uri, params = parse_contact(item)
            instance = (params.get("+sip.instance") or "").strip("<>").strip('"')
            if instance.lower().startswith("urn:uuid:"):
                instance = instance[len("urn:uuid:") :]
            out.append(
                {
                    "uri": uri,
                    "expires": params.get("expires"),
                    "instance_id": instance.lower() or None,
                    "reg_id": params.get("reg-id"),
                    "looks_like_pjsua": bool(instance and PJSUA_SHAPE.match(instance)),
                }
            )
    return out


# ------------------------------------------------------------------------ verdicts

VERDICTS: dict[str, str] = {
    "transport": "TCP to the registrar did not come up, or it dropped us",
    "auth": "authentication was rejected",
    "forbidden": "authenticated, but not authorised for this address-of-record",
    "not_found": "the address-of-record is not valid on this server",
    "misbehaving": "the registrar answered 6xx, which a registrar must never do",
    "unexpected": "unexpected final response",
    "bindings_not_reported": "the registrar does not report bindings",
    "empty": "no bindings at all right now",
    "one_contact": "exactly one binding, which is somebody else's",
    "multi_contact": "several bindings coexist right now",
    "policy_test_pointless": (
        "policy test needs exactly one foreign binding to be useful"
    ),
    "register_failed": "our own registration was rejected",
    "evicted": "registering evicted the existing binding",
    "coexists": "we registered and every existing binding survived",
}


def verdict(name: str) -> dict[str, str]:
    """Attach a verdict and its plain-language meaning to a result."""
    return {"verdict": name, "message": VERDICTS[name]}


def run(
    url: str, user: str, password: str, port: int, policy_test: bool
) -> tuple[int, dict[str, Any]]:
    """Probe one address-of-record. Returns an exit code and a result document."""
    result: dict[str, Any] = {
        "registrar": f"{url}:{port}",
        "aor": f"sip:{user}@{url}",
        # A Contact-less REGISTER is not an outbound registration, so the registrar has
        # no reason to answer Require: outbound. This simply is not observable here.
        "outbound": "unknown",
    }

    try:
        wire = Wire(url, port)
    except OSError as err:
        return EXIT_TRANSPORT, result | {"verdict": "transport", "message": str(err)}

    try:
        session = Session(wire, url, port, user, password)
        status, headers = session.register(contacts=[], expires=None)  # THE PROBE

        challenge = session.challenge or {}
        result |= {
            "status": status,
            "challenged": session.challenge is not None,
            "realm": challenge.get("realm"),
            "algorithm": challenge.get("algorithm") or "MD5",
            "qop": challenge.get("qop"),
        }
        via_params = kv(split_semis((headers.get("via") or [""])[0])[1:])
        result["nat"] = {
            "local": wire.sent_by,
            "received": via_params.get("received"),
            "rport": via_params.get("rport"),
        }

        if status in (401, 407):
            return EXIT_AUTH, result | verdict("auth")
        if status == 403:
            return EXIT_REFUSED, result | verdict("forbidden")
        if status == 404:
            return EXIT_REFUSED, result | verdict("not_found")
        if status >= 600:
            return EXIT_ODD, result | verdict("misbehaving")
        if status != 200:
            return EXIT_ODD, result | verdict("unexpected")

        before = bindings(headers)
        result["bindings_before"] = before

        if not policy_test:
            if len(before) >= 2:
                return EXIT_SAFE, result | verdict("multi_contact")
            if len(before) == 1:
                return EXIT_CAUTION, result | verdict("one_contact")
            return EXIT_CAUTION, result | verdict("empty")

        if len(before) != 1:
            return EXIT_ODD, result | verdict("policy_test_pointless")

        contact = (
            f"<{session.contact_uri()}>;"
            f'+sip.instance="<urn:uuid:{session.instance}>";reg-id=1'
        )
        # 60 seconds bounds the damage if this does evict the resident's phone.
        status, _ = session.register(contacts=[contact], expires=60)
        result["register_status"] = status
        if status != 200:
            return EXIT_ODD, result | verdict("register_failed")

        status, headers = session.register(contacts=[], expires=None)
        after = bindings(headers)
        result["bindings_after"] = after
        result["outbound"] = any(item["reg_id"] for item in after)

        gone = [
            item
            for item in before
            if not any(
                other["uri"] == item["uri"]
                or (item["instance_id"] and other["instance_id"] == item["instance_id"])
                for other in after
            )
        ]

        # Withdraw OUR contact and nothing else, whatever the outcome.
        session.register(contacts=[f"<{session.contact_uri()}>;expires=0"], expires=0)
        result["withdrew_own_contact"] = True

        if not any(item["instance_id"] == session.instance for item in after):
            return EXIT_ODD, result | verdict("bindings_not_reported")
        if gone:
            return EXIT_UNSAFE, result | verdict("evicted")
        return EXIT_SAFE, result | verdict("coexists")

    except (OSError, ProbeError) as err:
        return EXIT_TRANSPORT, result | {"verdict": "transport", "message": str(err)}
    finally:
        wire.close()


# ---------------------------------------------------------------------------- cli


def credentials_from_ha(path: Path) -> tuple[str, str, str]:
    """Read the SIP credentials straight out of a Home Assistant config entry store."""
    document = json.loads(path.read_text(encoding="utf-8"))
    for entry in document.get("data", {}).get("entries", []):
        if entry.get("domain") != "loki":
            continue
        sip = entry.get("data", {}).get("sip") or {}
        if sip.get("url") and sip.get("phone"):
            return str(sip["url"]), str(sip["phone"]), str(sip.get("password", ""))
    raise SystemExit(f"no Loki entry with SIP credentials found in {path}")


def render(result: dict[str, Any]) -> str:
    """Format a result for a human."""
    lines = [
        f"registrar : {result['registrar']}",
        f"AOR       : {result['aor']}",
    ]
    if "status" in result:
        lines.append(f"status    : {result['status']}")
    if result.get("challenged"):
        lines.append(
            f"digest    : realm={result.get('realm')!r} "
            f"algorithm={result.get('algorithm')} qop={result.get('qop')!r}"
        )
    if nat := result.get("nat"):
        lines.append(
            f"nat       : local={nat['local']} "
            f"received={nat['received']} rport={nat['rport']}"
        )

    for label in ("bindings_before", "bindings_after"):
        if label not in result:
            continue
        items = result[label]
        lines.append(f"{label.replace('_', ' ')[:10]:<10}: {len(items)}")
        for item in items:
            tag = " [pjsip-style]" if item["looks_like_pjsua"] else ""
            lines.append(
                f"    {item['uri']}  expires={item['expires']} "
                f"reg-id={item['reg_id']}{tag}"
            )

    lines.append("")
    lines.append(f"VERDICT   : {result['verdict']} -- {result['message']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the probe."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", help="registrar host, exactly as the API reports it")
    parser.add_argument("--user", help="SIP extension")
    parser.add_argument("--password", help="SIP password")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--from-ha",
        type=Path,
        metavar="CORE_CONFIG_ENTRIES",
        help="read credentials from a Home Assistant .storage/core.config_entries",
    )
    parser.add_argument("--json", action="store_true", help="emit the raw result")
    parser.add_argument(
        "--policy-test",
        action="store_true",
        help="briefly register to find out whether the registrar evicts others",
    )
    parser.add_argument(
        "--i-understand-this-may-break-the-doorbell",
        dest="acknowledged",
        action="store_true",
        help="required with --policy-test",
    )
    args = parser.parse_args(argv)

    if args.from_ha:
        url, user, password = credentials_from_ha(args.from_ha)
    elif args.url and args.user:
        url, user, password = args.url, args.user, args.password or ""
    else:
        parser.error("either --from-ha or both --url and --user are required")

    if args.policy_test and not args.acknowledged:
        parser.error(
            "--policy-test can evict the resident's phone for up to ~5 minutes; "
            "pass --i-understand-this-may-break-the-doorbell to confirm"
        )

    code, result = run(url, user, password, args.port, args.policy_test)
    print(
        json.dumps(result, indent=2, ensure_ascii=False)
        if args.json
        else render(result)
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
