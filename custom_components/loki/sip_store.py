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
MAX_CONTACTS = 4


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
    # Contact URIs a previous process registered with, and when they were written.
    # Without these a restart cannot recognise its own leftover binding -- the source
    # port changes with the connection and this registrar does not echo
    # ``+sip.instance`` -- so the client blocks itself out of its own account.
    contacts: list[str] = field(default_factory=list)
    contacts_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        """Serialise for the store."""
        return {
            "instance_id": self.instance_id,
            "first_registration_done": self.first_registration_done,
            "terminal": self.terminal,
            "terminal_detail": self.terminal_detail,
            "resolved": self.resolved,
            "contacts": self.contacts,
            "contacts_at": self.contacts_at,
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
                if isinstance(key, str) and isinstance(value, int):
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
            contacts=[
                item for item in (raw.get("contacts") or []) if isinstance(item, str)
            ],
            contacts_at=(
                float(raw["contacts_at"])
                if isinstance(raw.get("contacts_at"), (int, float))
                else 0.0
            ),
        )

    def fresh_contacts(self) -> list[str]:
        """Remembered Contact URIs, unless they are too old to still be ours."""
        if not self.contacts or time.time() - self.contacts_at > CONTACT_TTL:
            return []
        return list(self.contacts)

    def record_contact(self, uri: str) -> bool:
        """Remember a Contact URI we hold a binding at. False if nothing changed."""
        stamped = time.time()
        if self.contacts and self.contacts[-1] == uri:
            # Same URI, but the clock moved: the refresh keeps it trustworthy.
            self.contacts_at = stamped
            return False
        self.contacts = [*self.contacts, uri][-MAX_CONTACTS:]
        self.contacts_at = stamped
        return True

    def remember(self, remote_uri: str, device_id: int) -> bool:
        """Record which door a SIP URI belongs to. False if nothing changed."""
        if self.resolved.get(remote_uri) == device_id:
            return False
        self.resolved[remote_uri] = device_id
        # Oldest first: dicts keep insertion order, so this drops the URIs that have
        # gone longest without being re-resolved.
        while len(self.resolved) > MAX_RESOLVED:
            self.resolved.pop(next(iter(self.resolved)))
        return True


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

    async def async_record_contact(self, uri: str) -> None:
        """Remember the Contact URI our binding lives at, and persist it."""
        if self.state.record_contact(uri):
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
