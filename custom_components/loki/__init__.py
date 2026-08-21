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
from homeassistant.loader import async_get_integration

from .api import LokiApiError, LokiAuthError, LokiClient
from .call import CallManager
from .const import CONF_REFRESH_TOKEN, DOMAIN, OPT_SIP_ENABLED
from .coordinator import LokiConfigEntry, LokiCoordinator, LokiRuntimeData
from .frontend import async_register_cards
from .reauth import async_clear_auth_failed, async_fire_auth_failed
from .repairs import async_clear_reauth_unrecoverable, async_clear_sip_terminal
from .services import async_setup_services
from .sip_bridge import SipBridge, sip_credentials
from .sip_store import SipStore

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.EVENT,
    Platform.SENSOR,
    Platform.SWITCH,
]

# Required by hassfest as soon as async_setup exists.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions and the dashboard cards once, before any account loads."""
    async_setup_services(hass)
    # Once per Home Assistant start, not per config entry: registering a static path
    # adds a route, and doing that on every reload would stack duplicates.
    #
    # The version comes from the already-loaded manifest rather than from reading the
    # file again -- a blocking read on the event loop is exactly what Home Assistant
    # now warns about, and the loader has the answer in memory.
    integration = await async_get_integration(hass, DOMAIN)
    await async_register_cards(hass, str(integration.version or "0"))
    return True


async def async_setup_entry(hass: HomeAssistant, entry: LokiConfigEntry) -> bool:
    """Set up Loki from a config entry."""
    # Cleared first, before anything that can fail: reaching this line at all means
    # the entry is usable again. It must not live in async_unload_entry, because an
    # unload runs before every failing setup too.
    async_clear_reauth_unrecoverable(hass, entry.entry_id)

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
        async_fire_auth_failed(hass, entry, err)
        raise ConfigEntryAuthFailed(
            str(err),
            translation_domain=DOMAIN,
            translation_key="auth_expired",
            translation_placeholders={"phone": str(entry.title)},
        ) from err
    except LokiApiError as err:
        raise ConfigEntryNotReady(str(err)) from err

    async_clear_auth_failed(hass, entry)

    coordinator = LokiCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    call_manager = CallManager(hass, entry.entry_id)

    bridge: SipBridge | None = None
    if sip_credentials(entry) is not None:
        store = SipStore(hass, entry.entry_id)
        await store.async_load()
        bridge = SipBridge(hass, entry, client, call_manager, store)

    entry.runtime_data = LokiRuntimeData(
        client=client,
        coordinator=coordinator,
        call_manager=call_manager,
        sip_bridge=bridge,
    )
    entry.async_on_unload(call_manager.async_shutdown)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # SIP starts last, and only after the platforms exist. An INVITE arriving in the
    # window before then would reach a CallManager with no subscribers: the event
    # entity and the call sensor would both miss it, and the ring would be lost with
    # nothing in the log to say why.
    if bridge is not None and entry.options.get(OPT_SIP_ENABLED, False):
        await bridge.async_start()

    return True


async def async_unload_entry(hass: HomeAssistant, entry: LokiConfigEntry) -> bool:
    """Unload a config entry."""
    # Stopped before the platforms go, mirroring the setup order: while the client is
    # running it can announce a call, and announcing one into a half-dismantled entry
    # is how a stale timer outlives its own integration.
    if (bridge := entry.runtime_data.sip_bridge) is not None:
        await bridge.async_stop()

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unloaded:
        # on_unload callbacks only run on a successful unload; don't leave call
        # timeouts armed against an entry that is going away regardless.
        entry.runtime_data.call_manager.async_shutdown()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: LokiConfigEntry) -> None:
    """Clean up what outlives the account: the SIP state file and its card.

    The SIP repair issue is persistent and not fixable, so nothing else ever
    clears it -- an account deleted while blocked would leave a card pointing at
    an entry that no longer exists.
    """
    async_clear_sip_terminal(hass, entry.entry_id)
    await SipStore(hass, entry.entry_id).async_remove()


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: LokiConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow deleting a device the account no longer reports."""
    known = {str(device_id) for device_id in entry.runtime_data.coordinator.data}
    return not any(
        identifier[0] == DOMAIN and identifier[1] in known
        for identifier in device.identifiers
    )
