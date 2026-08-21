"""What the SIP client has to remember across restarts.

Three separate reasons, all of which cost the resident their doorbell if forgotten:

* **The instance id.** RFC 5626 §4.1: a registrar recognises a re-registration as the
  *same* device by this value. Minting a new UUID on every Home Assistant restart
  would make each restart look like an additional device, so the bindings pile up on
  an account whose table has room for very few -- and squeezing the resident's phone
  out of that table is the exact harm the whole SIP design exists to prevent.

* **The terminal latch.** ``LokiSipClient`` stops for good on a permanent failure, and
  that promise is worth nothing if a restart quietly retries the manoeuvre we already
  decided was unsafe. Cleared only by an explicit human gesture: switching SIP off and
  back on again. Which states get latched is the bridge's decision, not this module's
  -- being *blocked* deliberately does not, because looking again changes nothing.

* **Our own Contact URIs.** The source port changes with every connection, and the
  live registrar does not echo ``+sip.instance``, so after a restart the only handle
  on our own leftover binding is the URI it was registered at. Without it the client
  reads its own binding as another device and blocks itself out of its own account --
  observed, not theorised.

* **The resolved doors.** Which door a SIP URI belongs to is answered by the backend,
  and the answer never changes. Remembering it means a ring still reaches the right
  door when the API is briefly unreachable -- the alternative being to decline the
  call, because guessing the door from a display name is how the wrong entrance gets
  opened.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
import time
from typing import Any
import uuid

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

STORAGE_VERSION = 1

# Bounded so a backend that answers with a fresh URI each time cannot grow the file
# without limit. Far above the number of doors any one account has.
MAX_RESOLVED = 64

# How long a remembered Contact URI may be trusted as ours. Comfortably past the
# longest expiry we ever ask for (300 s), and short enough that a NAT port handed to
# another device in the meantime is never claimed as our own.
CONTACT_TTL = 600.0
# Six, not four: one registration can leave more than one row on the account -- the
# address we guessed and the corrected one the registrar answered with -- and all of
# them have to be recognisable after a restart.
MAX_CONTACTS = 6

# Rewriting the store on every registration refresh would be wasteful, and never
# rewriting it lets the stamp go stale under a long-held registration. Refresh
# when a third of the lifetime has gone.
CONTACT_REFRESH_AFTER = CONTACT_TTL / 3


def _parse_contacts(raw: Any, legacy_at: Any) -> list[tuple[str, float]]:
    """Parse stored Contact URIs, tolerating every shape this file has held.

    The current shape is ``[{"uri": ..., "at": ...}]``. An earlier one was a bare list
    of strings with a single ``contacts_at`` for all of them, and a store written by
    that version must still be readable -- otherwise the upgrade silently drops the
    URIs, the client stops recognising its own leftover binding, and it blocks itself
    out of its own account. That is not hypothetical: it is what this parser was
    changed for, and it happened on the first restart after the change.
    """
    if not isinstance(raw, list):
        return []
    inherited = float(legacy_at) if isinstance(legacy_at, (int, float)) else 0.0
    out: list[tuple[str, float]] = []
    for item in raw:
        if isinstance(item, str):
            if item:
                out.append((item, inherited))
            continue
        if not isinstance(item, dict):
            continue
        uri, at = item.get("uri"), item.get("at")
        if isinstance(uri, str) and uri and isinstance(at, (int, float)):
            out.append((uri, float(at)))
    return out[-MAX_CONTACTS:]


@dataclass
class SipStoredState:
    """The persistent half of the SIP client's state."""

    instance_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    # The first registration on an account uses a short expiry so a mistake heals in a
    # minute. Once one has succeeded there is no reason to pay that again.
    first_registration_done: bool = False
    # A latched permanent failure: the SipState value and its explanation.
    terminal: str | None = None
    terminal_detail: str | None = None
    # SIP remote URI -> Loki device id, as answered by the backend.
    resolved: dict[str, int] = field(default_factory=dict)
    # Contact URIs a previous process registered with, each with its own wall-clock
    # stamp. Without these a restart cannot recognise its own leftover binding --
    # the source port changes with the connection and this registrar does not echo
    # ``+sip.instance`` -- so the client blocks itself out of its own account.
    #
    # Per entry and not one stamp for the list: a shared stamp makes recording a
    # new URI vouch for three older ones, and a NAT port that has since been handed
    # to the resident's phone would then be withdrawn as though it were ours --
    # the precise harm this whole design exists to prevent.
    contacts: list[tuple[str, float]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Serialise for the store."""
        return {
            "instance_id": self.instance_id,
            "first_registration_done": self.first_registration_done,
            "terminal": self.terminal,
            "terminal_detail": self.terminal_detail,
            "resolved": self.resolved,
            "contacts": [{"uri": uri, "at": at} for uri, at in self.contacts],
        }

    @classmethod
    def from_dict(cls, raw: Any) -> SipStoredState:
        """Rebuild from the store, tolerating anything a hand-edit left behind.

        A corrupted field must not stop the integration loading: the worst outcome of
        falling back to a default here is one extra binding, and the worst outcome of
        raising is an account with no doorbell at all.
        """
        if not isinstance(raw, dict):
            return cls()

        instance_id = raw.get("instance_id")
        resolved_raw = raw.get("resolved")
        resolved: dict[str, int] = {}
        if isinstance(resolved_raw, dict):
            for key, value in resolved_raw.items():
                # `isinstance(True, int)` is True in Python, and a door id of
                # True would resolve a ring to whatever device 1 happens to be.
                if (
                    isinstance(key, str)
                    and isinstance(value, int)
                    and not isinstance(value, bool)
                ):
                    resolved[key] = value

        return cls(
            instance_id=(
                instance_id
                if isinstance(instance_id, str) and instance_id
                else str(uuid.uuid4())
            ),
            first_registration_done=bool(raw.get("first_registration_done")),
            terminal=raw.get("terminal")
            if isinstance(raw.get("terminal"), str)
            else None,
            terminal_detail=(
                raw.get("terminal_detail")
                if isinstance(raw.get("terminal_detail"), str)
                else None
            ),
            resolved=resolved,
            contacts=_parse_contacts(raw.get("contacts"), raw.get("contacts_at")),
        )

    def fresh_contacts(self) -> list[str]:
        """Remembered Contact URIs still young enough to be ours.

        Both ends of the interval are checked. A stamp in the future -- a clock
        correction, a restore from a backup taken on another machine -- would
        otherwise be trusted forever, and the whole point of the TTL is that a
        NAT port handed to another device is never claimed as our own.
        """
        now = time.time()
        return [uri for uri, at in self.contacts if 0 <= now - at <= CONTACT_TTL]

    def record_contact(self, uri: str) -> bool:
        """Remember one Contact URI we hold a binding at."""
        return self.record_contacts([uri])

    def record_contacts(self, uris: Sequence[str]) -> bool:
        """Remember every Contact URI we hold a binding at.

        More than one because a single registration can leave more than one row: the
        address the socket reported, and the corrected one the registrar answered
        with. Remembering only the second was enough to lock the client out of its
        own account when the first outlived it.

        Returns whether the change is worth a disk write: a URI we have not seen
        always is, and a re-stamp of one we have is once its age approaches the TTL.
        Matching anywhere in the list rather than only at the end, because two URIs
        recorded together would otherwise take turns re-stamping each other.
        """
        now = time.time()
        changed = False
        for uri in uris:
            if not uri:
                continue
            for index, (known, at) in enumerate(self.contacts):
                if known == uri:
                    if now - at >= CONTACT_REFRESH_AFTER:
                        self.contacts[index] = (uri, now)
                        changed = True
                    break
            else:
                self.contacts.append((uri, now))
                changed = True
        if len(self.contacts) > MAX_CONTACTS:
            self.contacts = self.contacts[-MAX_CONTACTS:]
        return changed

    def remember(self, remote_uri: str, device_id: int) -> bool:
        """Record which door a SIP URI belongs to. False if nothing needs saving.

        The entry is re-inserted at the end even when the mapping is unchanged, so
        eviction is by least recent use rather than by first sighting -- the door that
        rings every day is precisely the one that must not be dropped in favour of one
        seen once. That reordering alone is not worth a disk write; the next real
        change serialises the whole dict in its current order anyway.
        """
        unchanged = self.resolved.get(remote_uri) == device_id
        self.resolved.pop(remote_uri, None)
        self.resolved[remote_uri] = device_id
        while len(self.resolved) > MAX_RESOLVED:
            self.resolved.pop(next(iter(self.resolved)))
        return not unchanged


class SipStore:
    """Loads and saves one entry's SIP state."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialise the store for one config entry."""
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.sip"
        )
        self.state = SipStoredState()

    async def async_load(self) -> SipStoredState:
        """Load the stored state, or start a fresh one."""
        self.state = SipStoredState.from_dict(await self._store.async_load())
        return self.state

    async def async_save(self) -> None:
        """Persist the current state."""
        await self._store.async_save(self.state.as_dict())

    async def async_remember(self, remote_uri: str, device_id: int) -> None:
        """Record which door a SIP URI belongs to, and persist it if it is new."""
        if self.state.remember(remote_uri, device_id):
            await self.async_save()

    async def async_latch_terminal(self, state: str, detail: str) -> None:
        """Record that the client stopped for good."""
        self.state.terminal = state
        self.state.terminal_detail = detail
        await self.async_save()

    async def async_record_contacts(self, uris: Sequence[str]) -> None:
        """Remember the Contact URIs our bindings live at, and persist them."""
        if self.state.record_contacts(uris):
            await self.async_save()

    async def async_clear_terminal(self) -> None:
        """Forget a latched failure, on an explicit request from a person."""
        if self.state.terminal is None:
            return
        self.state.terminal = None
        self.state.terminal_detail = None
        await self.async_save()

    async def async_mark_registered(self) -> None:
        """Record that a registration has succeeded at least once."""
        if self.state.first_registration_done:
            return
        self.state.first_registration_done = True
        await self.async_save()

    async def async_remove(self) -> None:
        """Delete the file, when the config entry is removed."""
        await self._store.async_remove()
