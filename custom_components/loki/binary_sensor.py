"""Call-in-progress binary sensors."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .call import CallManager, CallUpdate, signal_call_update
from .coordinator import LokiConfigEntry, LokiCoordinator
from .entity import LokiEntity, build_unique_id
from .models import LokiDevice

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LokiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one call-active sensor per door."""
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
                LokiCallActive(coordinator, runtime.call_manager, device)
                for device in new
            )

    entry.async_on_unload(coordinator.async_add_listener(_async_add_new))
    _async_add_new()


class LokiCallActive(LokiEntity, BinarySensorEntity):
    """On while a call from this door is in progress.

    This is the genuine *state* half of a ring: the momentary press lives in the
    ``event`` entity, this reflects the ongoing call.
    """

    _attr_translation_key = "call_active"

    def __init__(
        self,
        coordinator: LokiCoordinator,
        call_manager: CallManager,
        device: LokiDevice,
    ) -> None:
        """Initialise the binary sensor."""
        super().__init__(coordinator, device)
        self._call_manager = call_manager
        self._attr_unique_id = build_unique_id(
            self._entry_id, device.id, "_call_active"
        )

    @property
    def available(self) -> bool:
        """A call is pushed, not polled.

        A failed device-list poll says nothing about whether a call is in progress.
        """
        return self.device is not None

    @property
    def is_on(self) -> bool:
        """Whether a call from this door is currently ringing."""
        return self._call_manager.active_call(self._device_id) is not None

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
        """Refresh state when this door's call changes."""
        if update.device_id == self._device_id:
            self.async_write_ha_state()
