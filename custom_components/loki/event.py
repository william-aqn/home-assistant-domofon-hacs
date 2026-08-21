"""Doorbell event entities."""

from __future__ import annotations

from typing import ClassVar

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .call import CallManager, CallUpdate, signal_call_update
from .const import EVENT_RING
from .coordinator import LokiConfigEntry, LokiCoordinator
from .entity import LokiEntity, build_unique_id
from .models import LokiDevice

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LokiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one doorbell event entity per door."""
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    known: set[int] = set()

    @callback
    def _async_add_new() -> None:
        new = [
            device
            for device_id, device in coordinator.data.items()
            if device_id not in known and device.is_door
        ]
        if new:
            known.update(device.id for device in new)
            async_add_entities(
                LokiDoorbellEvent(coordinator, runtime.call_manager, device)
                for device in new
            )

    entry.async_on_unload(coordinator.async_add_listener(_async_add_new))
    _async_add_new()


class LokiDoorbellEvent(LokiEntity, EventEntity):
    """Fires when someone rings a door.

    The ring is an event, not a state -- there is nothing to stay "on". The
    ``call_active`` binary sensor represents the in-progress state instead.
    """

    _attr_translation_key = "doorbell"
    _attr_device_class = EventDeviceClass.DOORBELL
    # Declaring the event types on the class is Home Assistant's own pattern for a
    # fixed set; the list is never mutated.
    _attr_event_types: ClassVar[list[str]] = [EVENT_RING]

    def __init__(
        self,
        coordinator: LokiCoordinator,
        call_manager: CallManager,
        device: LokiDevice,
    ) -> None:
        """Initialise the event entity."""
        super().__init__(coordinator, device)
        self._call_manager = call_manager
        self._attr_unique_id = build_unique_id(self._entry_id, device.id, "_doorbell")

    @property
    def available(self) -> bool:
        """A ring is pushed, not polled.

        A failed device-list poll says nothing about whether the intercom can ring, and
        going unavailable would swallow a real ring and then replay its stale timestamp
        when the poll recovers.
        """
        return self.device is not None

    async def async_added_to_hass(self) -> None:
        """Subscribe to call updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_call_update(self.coordinator.config_entry.entry_id),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self, update: CallUpdate) -> None:
        """Trigger the entity on a ring at this door."""
        if update.device_id != self._device_id or update.kind != "start":
            return
        call = self._call_manager.active_call(self._device_id)
        if call is None:
            return
        self._trigger_event(
            EVENT_RING,
            {"sip_uri": call.sip_uri, "call_id": call.call_id},
        )
        # _trigger_event only records the event; it does not write state.
        self.async_write_ha_state()
