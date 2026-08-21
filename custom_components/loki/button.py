"""Door open buttons."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import LokiApiError, LokiAuthError, LokiDeviceForbidden
from .const import DOMAIN
from .coordinator import LokiConfigEntry, LokiCoordinator
from .entity import LokiEntity, build_unique_id
from .models import LokiDevice

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LokiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one open button per door."""
    coordinator = entry.runtime_data.coordinator
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
            async_add_entities(LokiOpenButton(coordinator, device) for device in new)

    entry.async_on_unload(coordinator.async_add_listener(_async_add_new))
    _async_add_new()


class LokiOpenButton(LokiEntity, ButtonEntity):
    """Opens a door.

    Modelled as a button rather than a lock: the intercom exposes a momentary relay
    and reports no state, so there is nothing for a lock entity to represent.
    """

    _attr_translation_key = "open"

    def __init__(self, coordinator: LokiCoordinator, device: LokiDevice) -> None:
        """Initialise the button."""
        super().__init__(coordinator, device)
        self._attr_unique_id = build_unique_id(self._entry_id, device.id, "_open")

    async def async_press(self) -> None:
        """Open the door."""
        try:
            await self.coordinator.client.async_open_door(device_id=self._device_id)
        except LokiDeviceForbidden as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="open_forbidden",
                translation_placeholders={"error": str(err)},
            ) from err
        except LokiAuthError as err:
            # The client already refreshed and replayed on a 401, so reaching here
            # means the refresh token itself is dead. Start reauth now rather than
            # making the user wait for the next poll to notice.
            self.coordinator.config_entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="open_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        except LokiApiError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="open_failed",
                translation_placeholders={"error": str(err)},
            ) from err
