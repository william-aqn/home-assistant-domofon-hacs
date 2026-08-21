"""The identity of one registration.

Split out because several of these fields must stay constant for the lifetime of a
registration -- a re-REGISTER that changes Call-ID or the From tag reads to the
registrar as a different client, which is how a device ends up holding two bindings
and pushing somebody else's out of the table.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
import random
import re
import secrets
import time
import uuid

from .const import BRANCH_MAGIC, SIP_PORT
from .errors import SipSafetyError
from .uri import parse_params, parse_uri, split_commas, split_semis

# Enough to clean up after a burst of reconnects, few enough that a flapping link
# cannot turn the withdrawal list into a message the registrar rejects.
MAX_PRIOR_CONTACTS = 8

# pjsua derives its instance-id from a hash of the hostname rather than a UUID:
# 26 zeros then 8 hex digits. On Android gethostname() is very often "localhost", so
# two phones can even share one. A binding of this shape is a PJSIP client -- which is
# what the official intercom app is.
PJSUA_INSTANCE_SHAPE = re.compile(r"^0{8}-0000-0000-0000-0000[0-9a-f]{8}$", re.I)


@dataclass(frozen=True, slots=True)
class Binding:
    """One Contact row from a registrar's 200 OK."""

    uri: str
    expires: int | None
    instance_id: str | None  # lowercased, urn:uuid: stripped
    reg_id: str | None

    @property
    def looks_like_pjsua(self) -> bool:
        """Whether this binding was probably created by the official app."""
        return bool(self.instance_id and PJSUA_INSTANCE_SHAPE.match(self.instance_id))


def parse_bindings(rows: tuple[str, ...]) -> list[Binding]:
    """Parse every Contact value a registrar reported into structured bindings."""
    out: list[Binding] = []
    for row in rows:
        for item in split_commas(row):
            uri, params = _split_contact(item)
            instance = (params.get("+sip.instance") or "").strip("<>").strip('"')
            if instance.lower().startswith("urn:uuid:"):
                instance = instance[len("urn:uuid:") :]
            raw_expires = params.get("expires")
            out.append(
                Binding(
                    uri=uri,
                    expires=int(raw_expires) if (raw_expires or "").isdigit() else None,
                    instance_id=instance.lower() or None,
                    reg_id=params.get("reg-id"),
                )
            )
    return out


def _split_contact(row: str) -> tuple[str, dict[str, str]]:
    """Split one Contact value into its URI and its header parameters.

    Handles both ``<sip:x@y>;expires=60`` and the equally legal bracket-less
    ``sip:x@y;expires=60``. Mishandling the second form drops every parameter, which
    would report a healthy binding as somebody else's.
    """
    if "<" in row and ">" in row:
        start, end = row.index("<"), row.index(">")
        return row[start + 1 : end].strip(), parse_params(split_semis(row[end + 1 :]))
    pieces = split_semis(row)
    return pieces[0], parse_params(pieces[1:])


def uri_equal(first: str, second: str) -> bool:
    """Compare two contact URIs per RFC 3261 §19.1.4, falling back to text."""
    left, right = parse_uri(first), parse_uri(second)
    if left is None or right is None:
        return first.strip() == second.strip()
    return left.equivalent(right)


@dataclass(frozen=True, slots=True)
class PriorContact:
    """A Contact URI we used before, kept only long enough to withdraw it.

    Every reconnect gets a new source port and therefore a new Contact. Without
    withdrawing the old ones our own reconnects would fill the account's binding table
    and push the resident's phone out of it -- the very thing the whole design exists
    to prevent. They expire from this list because a NAT port can be reused by somebody
    else, and withdrawing a binding that is no longer ours would be exactly the harm
    we are avoiding.
    """

    uri: str
    recorded_at: float


@dataclass
class RegistrationState:
    """Constant identity plus the mutable counters of one registration."""

    host: str
    user: str
    port: int = SIP_PORT

    # Constant for the life of the registration (RFC 3261 §10.2).
    call_id: str = field(default_factory=lambda: secrets.token_hex(16))
    from_tag: str = field(default_factory=lambda: secrets.token_hex(5))
    # RFC 5626: lets a registrar recognise a re-registration as the *same* device even
    # when its address changed, which is what stops our reconnects piling up bindings.
    instance_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    # Filled in once the socket exists, and rewritten from received/rport.
    sent_by: str = ""
    contact_uri: str | None = None
    prior_contacts: list[PriorContact] = field(default_factory=list)

    # What the registrar actually granted, which may be less than we asked for.
    granted_expires: int | None = None

    cseq: int = 0

    def adopt_prior_contacts(self, uris: Sequence[str]) -> None:
        """Seed Contact URIs a previous process registered with.

        A restart otherwise cannot recognise its own binding. The source port changes
        with the connection, and a registrar that does not echo ``+sip.instance``
        leaves nothing else to match on -- so the client reads its own leftover
        binding as somebody else's and refuses to register on its own account.

        Only the caller can judge how old these are; anything stale must be dropped
        before it gets here, because a NAT port can be handed to another device.
        """
        self.prior_contacts = [
            PriorContact(uri, time.monotonic()) for uri in uris if uri
        ][-MAX_PRIOR_CONTACTS:]

    def set_contact(self, uri: str) -> None:
        """Adopt a new Contact URI, remembering the old one so it can be withdrawn."""
        if self.contact_uri and self.contact_uri != uri:
            self.prior_contacts.append(PriorContact(self.contact_uri, time.monotonic()))
            # Bounded: a flapping connection must not accumulate an unbounded list of
            # URIs we would keep trying to withdraw.
            del self.prior_contacts[:-MAX_PRIOR_CONTACTS]
        self.contact_uri = uri

    def forget_stale_priors(self, ttl: float) -> None:
        """Drop old Contact URIs once the registrar would have expired them anyway."""
        now = time.monotonic()
        self.prior_contacts = [
            prior for prior in self.prior_contacts if now - prior.recorded_at < ttl
        ]

    def refresh_delay(self) -> float:
        """How long to wait before renewing the registration.

        Measured against what the registrar GRANTED, not what we asked for, and with a
        wide lead: a refresh may have to rebuild the TCP connection and redo an
        authentication handshake before it can even send the REGISTER.
        """
        granted = self.granted_expires or 300
        if granted < 20:
            return max(1.0, granted / 2)
        lead = min(60, max(5, granted // 10))
        # Jitter downwards only, never later than the lead allows. Not a
        # cryptographic choice: it just spreads refreshes in time.
        return granted - lead - random.uniform(0, 5)  # noqa: S311

    @property
    def registrar_uri(self) -> str:
        """Where REGISTER is sent. The API reports a bare host, so we add the rest."""
        return f"sip:{self.host}:{self.port};transport=tcp"

    @property
    def aor(self) -> str:
        """The address-of-record this registration is for."""
        return f"sip:{self.user}@{self.host}"

    def next_cseq(self) -> int:
        """Advance the sequence number. §22.2: credentials go on a fresh CSeq."""
        self.cseq += 1
        return self.cseq

    def new_branch(self) -> str:
        """A fresh transaction identifier (§8.1.1.7)."""
        return f"{BRANCH_MAGIC}{secrets.token_hex(8)}"

    def make_contact_uri(self, host: str, port: int) -> str:
        """The Contact URI naming an address the registrar can reach us at."""
        authority = f"[{host}]" if ":" in host else host
        return f"sip:{self.user}@{authority}:{port};transport=tcp"

    def contact(self, *, expires: int | None = None) -> str:
        """Our live Contact value.

        Carries the instance id so the registrar can tell a re-registration from a
        second device -- which is what stops our own reconnects from accumulating
        bindings and squeezing the resident's phone out of the table.
        """
        uri = self.contact_uri or f"sip:{self.user}@{self.sent_by};transport=tcp"
        parts = [
            f"<{uri}>",
            f'+sip.instance="<urn:uuid:{self.instance_id}>"',
            "reg-id=1",
        ]
        if expires is not None:
            parts.append(f"expires={expires}")
        return ";".join(parts)

    def build_contacts(self, *, live: str | None, reap: Sequence[str]) -> list[str]:
        """Assemble the Contact rows for one REGISTER.

        This is the second place the wildcard rule is enforced, and the only place a
        ``;expires=0`` row can be produced. Every such row must name a URI this object
        minted and that the caller positively attributed to us -- withdrawing anything
        else is indistinguishable, from the resident's side, from us evicting them.
        """
        rows: list[str] = []

        if live is not None:
            if "*" in live:
                raise SipSafetyError("never build a wildcard Contact")
            rows.append(
                f'<{live}>;+sip.instance="<urn:uuid:{self.instance_id}>";reg-id=1'
            )

        for uri in reap:
            if "*" in uri:
                raise SipSafetyError("never build a wildcard Contact")
            if uri == live:
                raise SipSafetyError(
                    "refusing to withdraw the contact being registered"
                )
            rows.append(f"<{uri}>;expires=0")

        return rows
