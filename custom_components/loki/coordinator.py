"""Device list coordinator and per-entry runtime state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import LokiApiError, LokiAuthError, LokiClient
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN, OPT_SCAN_INTERVAL
from .models import LokiDevice
from .reauth import async_clear_auth_failed, async_fire_auth_failed

if TYPE_CHECKING:
    from .call import CallManager

_LOGGER = logging.getLogger(__name__)


@dataclass
class LokiRuntimeData:
    """Everything one config entry owns while it is loaded."""

    client: LokiClient
    coordinator: LokiCoordinator
    call_manager: CallManager


type LokiConfigEntry = ConfigEntry[LokiRuntimeData]


class LokiCoordinator(DataUpdateCoordinator[dict[int, LokiDevice]]):
    """Polls the device list.

    Doors and cameras change very rarely, so this runs on a slow interval. Newly
    provisioned devices do gain entities without a reload; a *rename* in the Loki app
    only reaches the device registry after the entry is reloaded.
    """

    config_entry: LokiConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: LokiConfigEntry,
        client: LokiClient,
    ) -> None:
        """Initialise the coordinator."""
        interval = entry.options.get(OPT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=(
                timedelta(seconds=interval) if interval else DEFAULT_SCAN_INTERVAL
            ),
            # LokiDevice is a frozen dataclass, so an unchanged list compares equal
            # and there is no reason to wake every listener every five minutes.
            always_update=False,
        )
        self.client = client

    async def _async_update_data(self) -> dict[int, LokiDevice]:
        """Fetch the current device list."""
        try:
            devices = await self.client.async_get_devices()
        except LokiAuthError as err:
            # Only a fresh SMS login can recover from this. The event is latched, so
            # this fires once rather than on every poll for as long as it stays broken.
            async_fire_auth_failed(self.hass, self.config_entry, err)
            raise ConfigEntryAuthFailed(
                str(err),
                translation_domain=DOMAIN,
                translation_key="auth_expired",
                translation_placeholders={"phone": str(self.config_entry.title)},
            ) from err
        except LokiApiError as err:
            raise UpdateFailed(str(err)) from err

        async_clear_auth_failed(self.hass, self.config_entry)
        return {device.id: device for device in devices}
