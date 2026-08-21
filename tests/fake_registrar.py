#!/usr/bin/env python3
"""A minimal SIP registrar, enough to exercise the probe and later the client.

Deliberately built before the code it validates: a test bench that has never been
shown to reproduce the dangerous case is not evidence of anything. It can therefore
be told to enforce ``max_contacts``, which is the one registrar policy that decides
whether SIP is safe on a shared account -- and the one that cannot be observed from
outside without registering.

Standalone:
  python tests/fake_registrar.py --port 15060 --max-contacts 3 --seed-foreign
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from dataclasses import dataclass, field
import hashlib
import secrets
import sys
import time

CRLF = "\r\n"
REALM = "fake.registrar"


@dataclass
class Binding:
    """One registered contact."""

    uri: str
    expires: int
    instance_id: str | None
    reg_id: str | None
    registered_at: float = field(default_factory=time.monotonic)

    def render(self) -> str:
        """Format as a Contact header value."""
        parts = [f"<{self.uri}>", f"expires={self.expires}"]
        if self.instance_id:
            parts.append(f'+sip.instance="<urn:uuid:{self.instance_id}>"')
        if self.reg_id:
            parts.append(f"reg-id={self.reg_id}")
        return ";".join(parts)


def _split(value: str, sep: str) -> list[str]:
    """Split outside quotes and angle brackets."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    in_quotes = False
    for char in value:
        if in_quotes:
            current.append(char)
            if char == '"':
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


def _kv(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        key, _, value = item.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        out[key.strip().lower()] = value
    return out


class FakeRegistrar:
    """Serves REGISTER over TCP against an in-memory binding table."""

    def __init__(
        self,
        *,
        password: str = "secret",
        max_contacts: int = 3,
        require_auth: bool = True,
        report_bindings: bool = True,
        rewrite_contact: bool = False,
    ) -> None:
        """Configure the policies that matter to the probe."""
        self.password = password
        self.max_contacts = max_contacts
        self.require_auth = require_auth
        # A registrar that does not report bindings makes the whole safety scheme
        # blind, so the probe has to detect it. This switch reproduces that.
        self.report_bindings = report_bindings
        self.rewrite_contact = rewrite_contact
        self.bindings: list[Binding] = []
        self.evictions = 0
        self.wildcard_seen = False
        self.server: asyncio.Server | None = None
        self.port = 0
        self._writer: asyncio.StreamWriter | None = None
        self._replies: asyncio.Queue[str] = asyncio.Queue()

    def seed_foreign(self, count: int = 1) -> None:
        """Pretend somebody else's phone is already registered."""
        for index in range(count):
            self.bindings.append(
                Binding(
                    uri=f"sip:1001543@203.0.113.{20 + index}:5060;transport=tcp",
                    expires=300,
                    # The shape a PJSIP client really produces.
                    instance_id=f"00000000-0000-0000-0000-0000{index:08x}",
                    reg_id=None,
                )
            )

    async def start(self, port: int = 0) -> int:
        """Start listening; returns the bound port."""
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", port)
        self.port = self.server.sockets[0].getsockname()[1]
        return self.port

    async def stop(self) -> None:
        """Stop listening."""
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def send_invite(self, *, timeout: float = 5.0) -> list[str]:
        """Push an INVITE down a live connection and collect what comes back.

        Real registrars deliver an inbound call over the same connection the client
        registered on, which is what makes this integration work without a port
        forward. Reproducing that is the only way to test the client's answer.
        """
        if self._writer is None:
            raise RuntimeError("no client connected")

        self._replies = asyncio.Queue()
        invite = CRLF.join(
            [
                "INVITE sip:1009999@fake SIP/2.0",
                "Via: SIP/2.0/TCP fake:5060;branch=z9hG4bKinvite1",
                "Max-Forwards: 70",
                'From: "Дверь" <sip:1001@fake>;tag=callertag',
                "To: <sip:1009999@fake>",
                "Call-ID: invite-call-1",
                "CSeq: 1 INVITE",
                "Contact: <sip:1001@fake:5060>",
                "Content-Length: 0",
                "",
                "",
            ]
        )
        self._writer.write(invite.encode("utf-8"))
        await self._writer.drain()

        replies: list[str] = []
        try:
            async with asyncio.timeout(timeout):
                while True:
                    replies.append(await self._replies.get())
        except TimeoutError:
            pass
        return replies

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        buf = b""
        self._writer = writer
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    return
                buf += chunk
                while (end := buf.find(b"\r\n\r\n")) != -1:
                    block, buf = buf[:end].decode("latin-1"), buf[end + 4 :]
                    rows = [row for row in block.split(CRLF) if row]
                    if not rows:
                        continue
                    if rows[0].startswith("SIP/2.0"):
                        # A reply to something we sent, not a request to serve.
                        await self._replies.put(rows[0])
                        continue
                    writer.write(self._respond(rows).encode("latin-1"))
                    await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            return
        finally:
            writer.close()

    def _respond(self, rows: list[str]) -> str:
        headers: dict[str, list[str]] = {}
        for row in rows[1:]:
            name, sep, value = row.partition(":")
            if sep:
                headers.setdefault(name.strip().lower(), []).append(value.strip())

        def one(name: str) -> str:
            return (headers.get(name) or [""])[0]

        if self.require_auth and not headers.get("authorization"):
            nonce = secrets.token_hex(16)
            self._nonce = nonce
            return self._build(
                401,
                "Unauthorized",
                headers,
                extra=[
                    f'WWW-Authenticate: Digest realm="{REALM}", nonce="{nonce}", '
                    f'qop="auth", algorithm=MD5'
                ],
            )

        if self.require_auth and not self._check_auth(one("authorization")):
            return self._build(403, "Forbidden", headers)

        contacts = headers.get("contact", [])

        if not contacts:
            # RFC 3261 §10.2.3: report the bindings, change nothing.
            return self._build(200, "OK", headers, bindings=True)

        for row in contacts:
            for item in _split(row, ","):
                if item.strip() == "*":
                    # Never expected from our own code; recorded so a test can assert
                    # the probe and client never send it.
                    self.wildcard_seen = True
                    return self._build(400, "Bad Request", headers)
                self._apply(item, int(one("expires") or 3600))

        return self._build(200, "OK", headers, bindings=True)

    def _apply(self, item: str, default_expires: int) -> None:
        start, end = item.index("<"), item.index(">")
        uri = item[start + 1 : end].strip()
        params = _kv(_split(item[end + 1 :], ";"))
        expires = int(params.get("expires", default_expires))
        instance = (params.get("+sip.instance") or "").strip("<>").strip('"')
        if instance.lower().startswith("urn:uuid:"):
            instance = instance[len("urn:uuid:") :]

        existing = next((b for b in self.bindings if b.uri == uri), None)

        if expires == 0:
            if existing is not None:
                self.bindings.remove(existing)
            return

        if existing is not None:
            existing.expires = expires
            return

        self.bindings.append(
            Binding(uri, expires, instance.lower() or None, params.get("reg-id"))
        )
        # The policy that decides everything: drop the oldest to stay within the cap.
        while len(self.bindings) > self.max_contacts:
            self.bindings.pop(0)
            self.evictions += 1

    def _check_auth(self, header: str) -> bool:
        if not header.lower().startswith("digest"):
            return False
        params = _kv(_split(header.split(" ", 1)[1], ","))
        ha1 = hashlib.md5(
            f"{params.get('username')}:{REALM}:{self.password}".encode(),
            usedforsecurity=False,
        ).hexdigest()
        ha2 = hashlib.md5(
            f"REGISTER:{params.get('uri')}".encode(), usedforsecurity=False
        ).hexdigest()
        if params.get("qop") == "auth":
            raw = (
                f"{ha1}:{params.get('nonce')}:{params.get('nc')}:"
                f"{params.get('cnonce')}:auth:{ha2}"
            )
        else:
            raw = f"{ha1}:{params.get('nonce')}:{ha2}"
        expected = hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()
        return expected == params.get("response")

    def _build(
        self,
        status: int,
        reason: str,
        request: dict[str, list[str]],
        *,
        extra: list[str] | None = None,
        bindings: bool = False,
    ) -> str:
        def one(name: str) -> str:
            return (request.get(name) or [""])[0]

        via = one("via")
        if self.rewrite_contact and ";received=" not in via:
            via = f"{via};received=203.0.113.9;rport=44444"

        rows = [
            f"SIP/2.0 {status} {reason}",
            f"Via: {via}",
            f"From: {one('from')}",
            f"To: {one('to')};tag={secrets.token_hex(4)}",
            f"Call-ID: {one('call-id')}",
            f"CSeq: {one('cseq')}",
        ]
        rows += extra or []
        if bindings and self.report_bindings:
            rows += [f"Contact: {b.render()}" for b in self.bindings]
        rows += ["Content-Length: 0", "", ""]
        return CRLF.join(rows)


async def _serve(args: argparse.Namespace) -> None:
    registrar = FakeRegistrar(
        password=args.password,
        max_contacts=args.max_contacts,
        report_bindings=not args.no_report_bindings,
    )
    if args.seed_foreign:
        registrar.seed_foreign(args.seed_foreign)
    port = await registrar.start(args.port)
    print(f"fake registrar on 127.0.0.1:{port}, max_contacts={args.max_contacts}")
    print(f"seeded bindings: {len(registrar.bindings)}")
    await asyncio.Event().wait()


def main() -> int:
    """Run the registrar standalone."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=15060)
    parser.add_argument("--password", default="secret")
    parser.add_argument("--max-contacts", type=int, default=3)
    parser.add_argument("--seed-foreign", type=int, default=0)
    parser.add_argument("--no-report-bindings", action="store_true")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_serve(parser.parse_args()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
