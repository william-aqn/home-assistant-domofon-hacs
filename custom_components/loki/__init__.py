"""The Loki integration."""

from __future__ import annotations

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import LokiApiError, LokiAuthError, LokiClient
from .call import CallManager
from .const import CONF_REFRESH_TOKEN, DOMAIN
from .coordinator import LokiConfigEntry, LokiCoordinator, LokiRuntimeData
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.EVENT,
]

# Required by hassfest as soon as async_setup exists.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions once, before any account is loaded."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: LokiConfigEntry) -> bool:
    """Set up Loki from a config entry."""
    client = LokiClient(
        async_get_clientsession(hass),
        refresh_token=entry.data[CONF_REFRESH_TOKEN],
    )

    # Access tokens live ~20 minutes and are never persisted, so every startup begins
    # by trading the refresh token for a fresh one. If that fails the refresh token is
    # gone for good -- it is never rotated, so there is nothing to retry with.
    try:
        await client.async_refresh_token()
    except LokiAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except LokiApiError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = LokiCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    call_manager = CallManager(hass, entry.entry_id)
    entry.runtime_data = LokiRuntimeData(
        client=client, coordinator=coordinator, call_manager=call_manager
    )
    entry.async_on_unload(call_manager.async_shutdown)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: LokiConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unloaded:
        # on_unload callbacks only run on a successful unload; don't leave call
        # timeouts armed against an entry that is going away regardless.
        entry.runtime_data.call_manager.async_shutdown()
    return unloaded


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: LokiConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow deleting a device the account no longer reports."""
    known = {str(device_id) for device_id in entry.runtime_data.coordinator.data}
    return not any(
        identifier[0] == DOMAIN and identifier[1] in known
        for identifier in device.identifiers
    )
