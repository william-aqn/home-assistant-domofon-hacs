"""Server-side INVITE transactions.

Small, but the details matter more than the size suggests. Two of them decide whether
the resident's own phone keeps working while Home Assistant is also registered:

* every response to one request must carry the SAME To tag (RFC 3261 §8.2.6.2), or no
  proxy will match it to the transaction it belongs to;
* once a final response has been sent, a retransmitted INVITE must be answered with
  that final again -- answering with a provisional instead puts the branch back into
  Proceeding and resets the proxy's Timer C, which is what keeps a forking proxy from
  delivering the other branches' answers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import secrets
import time

from .messages import SipMessage

# Long enough to absorb retransmissions of an INVITE we have already answered
# (RFC 3261 Timer H is 64*T1 = 32 s), short enough not to grow without bound.
COMPLETED_TTL = 40.0

# A transaction that never got a final response looks impossible -- the branch
# deadline guarantees one inside 115 s. It becomes possible when that deadline task
# is cancelled without firing, which is exactly what a reconnect does. Without this
# the table would hold such a transaction, and its Call-ID, for the life of the
# process.
UNANSWERED_TTL = 300.0


def transaction_key(message: SipMessage) -> tuple[str, str, str]:
    """Identify the transaction a request belongs to (RFC 3261 §17.2.3).

    Keyed on the topmost Via branch, its sent-by, and the CSeq method. A CANCEL
    deliberately shares the branch of the INVITE it cancels, which is exactly how it
    finds it -- so the method is part of the key to keep the two apart.
    """
    via = message.value("via")
    branch = ""
    sent_by = ""
    for piece in via.split(";"):
        piece = piece.strip()
        if piece.lower().startswith("branch="):
            branch = piece[len("branch=") :]
        elif not sent_by and piece.upper().startswith("SIP/2.0/"):
            sent_by = piece.split(None, 1)[-1] if " " in piece else piece
    _number, method = message.cseq
    return branch, sent_by, method


@dataclass
class InviteTransaction:
    """One inbound call as far as SIP is concerned."""

    call_id: str
    remote_uri: str
    # The INVITE this transaction answers. Every final response must be built from
    # it and from nothing else: a 487 built from the CANCEL carries "CSeq: n
    # CANCEL" instead of "n INVITE" (RFC 3261 §9.2), and a 486 built from "the
    # last INVITE seen" goes out on whichever branch arrived most recently.
    request: SipMessage
    to_tag: str = field(default_factory=lambda: secrets.token_hex(4))
    created: float = field(default_factory=time.monotonic)
    final_sent: bool = False
    # Kept so a retransmission can be answered with the same bytes.
    last_final: bytes | None = None
    cancelled: bool = False

    @property
    def age(self) -> float:
        """Seconds since the INVITE arrived."""
        return time.monotonic() - self.created


class TransactionTable:
    """The inbound transactions currently worth remembering."""

    def __init__(self) -> None:
        """Start empty."""
        self._live: dict[tuple[str, str, str], InviteTransaction] = {}
        self._by_call: dict[str, InviteTransaction] = {}

    def get(self, key: tuple[str, str, str]) -> InviteTransaction | None:
        """The transaction for a key, if we still hold it."""
        return self._live.get(key)

    def by_call_id(self, call_id: str) -> InviteTransaction | None:
        """The transaction for a Call-ID, used when Home Assistant ends a call."""
        return self._by_call.get(call_id)

    def add(
        self, key: tuple[str, str, str], transaction: InviteTransaction
    ) -> InviteTransaction:
        """Remember a new transaction."""
        self._live[key] = transaction
        self._by_call[transaction.call_id] = transaction
        return transaction

    def active(self) -> list[InviteTransaction]:
        """Transactions that have not been answered with a final response."""
        return [item for item in self._live.values() if not item.final_sent]

    def prune(self) -> None:
        """Forget transactions that can no longer matter.

        Completed ones once retransmissions can no longer arrive, and
        unanswered ones once they have outlived any plausible call -- a
        reconnect cancels branch deadlines without firing them, so
        "unanswered" is not the impossible state it looks like.
        """
        stale = [
            key
            for key, item in self._live.items()
            if item.age > (COMPLETED_TTL if item.final_sent else UNANSWERED_TTL)
        ]
        for key in stale:
            transaction = self._live.pop(key)
            if self._by_call.get(transaction.call_id) is transaction:
                del self._by_call[transaction.call_id]
