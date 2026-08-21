"""Incoming-call state, shared by every source and consumer of a ring.

This is the join point the plan is built around. The SIP client (phase 3) and the
``simulate_ring`` service (phase 2) both *push* calls in here; the ``event`` and
``binary_sensor`` entities *read* from here. Building it before SIP is deliberate --
the whole notification chain can be exercised with ``simulate_ring`` before a single
SIP packet is sent.

Everything runs on the event loop, so the mutating methods are plain callbacks.
"""

from __future__ import annotations

from dataclasses import dataclass
import functools
import logging
import time
from typing import TYPE_CHECKING

from homeassistant.core import CALLBACK_TYPE, Context, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.util.uuid import random_uuid_hex

from .const import (
    CALL_TIMEOUT,
    DOMAIN,
    EVENT_LOKI,
    EVENT_TYPE_CALL_ENDED,
    EVENT_TYPE_CALL_INCOMING,
)
from .entity import build_unique_id

if TYPE_CHECKING:
    from datetime import datetime

    from .models import LokiDevice

_LOGGER = logging.getLogger(__name__)


def signal_call_update(entry_id: str) -> str:
    """Dispatcher signal fired when a door's call state changes."""
    return f"{DOMAIN}_call_update_{entry_id}"


@dataclass(frozen=True)
class CallUpdate:
    """Payload of the dispatcher signal. Entities filter on ``device_id``."""

    device_id: int
    kind: str  # "start" | "end"
    # Carried so a listener can tell *which* call ended. Without it, a second ring at
    # the same door would make an "end" ambiguous, and the SIP bridge could release
    # the branch belonging to the call that replaced it.
    call_id: str = ""
    reason: str = ""


@dataclass
class ActiveCall:
    """A ring currently in progress at one door."""

    device_id: int
    device_name: str
    call_id: str
    sip_uri: str | None
    started: float  # time.monotonic()
    context: Context | None = None
    cancel_timeout: CALLBACK_TYPE | None = None


class CallManager:
    """Tracks the current call per door and notifies entities of changes."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialise the manager for one config entry."""
        self.hass = hass
        self.entry_id = entry_id
        self._calls: dict[int, ActiveCall] = {}
        # Monotonic start times and their call ids, kept for the life of the entry so
        # the Frigate bridge (phase 4) can correlate a recognised face with a recent
        # ring at the same door. Bounded by the number of doors on the account.
        self._last_ring: dict[int, float] = {}
        self._last_ring_call_id: dict[int, str] = {}
        self._shutdown = False

    @callback
    def active_call(self, device_id: int) -> ActiveCall | None:
        """Return the in-progress call for a door, if any."""
        return self._calls.get(device_id)

    @callback
    def seconds_since_ring(self, device_id: int) -> float | None:
        """Monotonic seconds since the most recent ring at a door, or None."""
        started = self._last_ring.get(device_id)
        return None if started is None else time.monotonic() - started

    @callback
    def recent_call_id(self, device_id: int, within: float) -> str | None:
        """Return the call id of a ring within ``within`` seconds, for correlation.

        An in-progress call is not privileged: a hung call (a missed SIP CANCEL) can
        sit in ``_calls`` for CALL_TIMEOUT seconds, and correlating a face with it long
        after the ring is exactly the mistake the window exists to prevent.
        """
        elapsed = self.seconds_since_ring(device_id)
        if elapsed is None or elapsed > within:
            return None
        return self._last_ring_call_id.get(device_id)

    @callback
    def async_start_call(
        self,
        device: LokiDevice,
        *,
        sip_uri: str | None = None,
        call_id: str | None = None,
        context: Context | None = None,
    ) -> ActiveCall | None:
        """Register a ring and notify listeners.

        Returns None if the entry is unloading.
        """
        if self._shutdown:
            # A late push must not re-arm timers or fire events into automations that
            # no longer have an integration behind them.
            return None

        if (existing := self._calls.get(device.id)) is not None:
            if call_id is not None and existing.call_id == call_id:
                # The same call re-announced (a SIP INVITE retransmission): refresh
                # the safety timeout, do not mint a new identity or re-announce.
                if existing.cancel_timeout:
                    existing.cancel_timeout()
            else:
                # A genuinely new call at a door that is already ringing: close the
                # old one properly so anything waiting on its call_id is released.
                self.async_end_call(device.id, reason="superseded")

        now = time.monotonic()
        call = ActiveCall(
            device_id=device.id,
            device_name=device.name,
            call_id=call_id or f"sim-{random_uuid_hex()}",
            sip_uri=sip_uri,
            started=now,
            context=context,
        )
        call.cancel_timeout = async_call_later(
            self.hass,
            CALL_TIMEOUT,
            functools.partial(self._async_timeout, device.id, call.call_id),
        )
        self._calls[device.id] = call
        self._last_ring[device.id] = now
        self._last_ring_call_id[device.id] = call.call_id

        _LOGGER.debug("Ring at door %s (%s)", device.id, device.name)
        # Entities first, bus second: an automation triggered by the bus event may read
        # binary_sensor.<door>_call_active, which must already be on by then.
        async_dispatcher_send(
            self.hass,
            signal_call_update(self.entry_id),
            CallUpdate(device_id=device.id, kind="start", call_id=call.call_id),
        )
        self._fire_bus_event(EVENT_TYPE_CALL_INCOMING, call)
        return call

    @callback
    def async_end_call(
        self, device_id: int, *, reason: str = "ended", call_id: str | None = None
    ) -> None:
        """Clear a door's call and notify listeners.

        A no-op if nothing is active, or if ``call_id`` names a call that has already
        been superseded -- a late SIP BYE must not tear down the ring that replaced it.
        """
        call = self._calls.get(device_id)
        if call is None or (call_id is not None and call.call_id != call_id):
            return

        del self._calls[device_id]
        if call.cancel_timeout:
            call.cancel_timeout()

        _LOGGER.debug("Call at door %s ended (%s)", device_id, reason)
        async_dispatcher_send(
            self.hass,
            signal_call_update(self.entry_id),
            CallUpdate(
                device_id=device_id, kind="end", call_id=call.call_id, reason=reason
            ),
        )
        self._fire_bus_event(EVENT_TYPE_CALL_ENDED, call, reason=reason)

    @callback
    def async_shutdown(self) -> None:
        """Cancel every pending timeout on unload and refuse further calls."""
        self._shutdown = True
        for call in self._calls.values():
            if call.cancel_timeout:
                call.cancel_timeout()
        self._calls.clear()
        self._last_ring.clear()
        self._last_ring_call_id.clear()

    # -- internals ------------------------------------------------------------

    @callback
    def _async_timeout(self, device_id: int, call_id: str, _now: datetime) -> None:
        """End a call that never received an explicit end."""
        self.async_end_call(device_id, reason="timeout", call_id=call_id)

    @callback
    def _fire_bus_event(
        self, event_type: str, call: ActiveCall, *, reason: str | None = None
    ) -> None:
        """Fire the rich ``loki_event`` bus event automations hook onto."""
        data = {
            "type": event_type,
            "device_id": call.device_id,
            "device_name": call.device_name,
            "entity_id": self._camera_entity_id(call.device_id),
            "sip_uri": call.sip_uri,
            "call_id": call.call_id,
        }
        if reason is not None:
            data["reason"] = reason
        # Propagating the originating context keeps the logbook chain intact:
        # simulate_ring -> loki_event -> notification -> open_door reads as one story.
        self.hass.bus.async_fire(EVENT_LOKI, data, context=call.context)

    @callback
    def _camera_entity_id(self, device_id: int) -> str | None:
        """Resolve *this entry's* camera entity for a door, or None.

        Returns None for a disabled registry entry too: a disabled entity has no state,
        so /api/camera_proxy/<entity_id> would hand the notification a URL that 404s.
        """
        registry = er.async_get(self.hass)
        entity_id = registry.async_get_entity_id(
            "camera", DOMAIN, build_unique_id(self.entry_id, device_id)
        )
        if entity_id is None:
            return None
        entry = registry.async_get(entity_id)
        return None if entry is None or entry.disabled_by is not None else entity_id
