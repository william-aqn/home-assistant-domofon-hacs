"""The identity of one registration.

Split out because several of these fields must stay constant for the lifetime of a
registration -- a re-REGISTER that changes Call-ID or the From tag reads to the
registrar as a different client, which is how a device ends up holding two bindings
and pushing somebody else's out of the table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import secrets
import uuid

from .const import BRANCH_MAGIC, SIP_PORT


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

    cseq: int = 0

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

    def contact(self, *, expires: int | None = None) -> str:
        """Our Contact value.

        Carries the instance id so the registrar can tell a re-registration from a
        second device. ``expires=0`` withdraws this one binding and nothing else --
        the only form of removal this integration ever sends.
        """
        parts = [
            f"<sip:{self.user}@{self.sent_by};transport=tcp>",
            f'+sip.instance="<urn:uuid:{self.instance_id}>"',
            "reg-id=1",
        ]
        if expires is not None:
            parts.append(f"expires={expires}")
        return ";".join(parts)
