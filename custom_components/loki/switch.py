"""The SIP switch: the one-tap rollback the risk register asks for.

Registering on the account is the single riskiest thing this integration does, so it
is off until a person turns it on, and turning it off again takes effect immediately
rather than waiting for a config-entry reload.

The switch shows *intent*, not the client's live state. A client sitting in backoff
after a network blip is still meant to be on, and a switch that flipped itself off
every time the connection wobbled would be unusable in an automation. What the client
is actually doing is on ``sensor.<account>_sip``.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import OPT_SIP_ENABLED
from .coordinator import LokiConfigEntry
from .entity import LokiAccountEntity
from .sip_bridge import SipBridge


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LokiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the account-level switches."""
    runtime = entry.runtime_data
    if runtime.sip_bridge is None:
        return
    async_add_entities([LokiSipSwitch(runtime.coordinator, runtime.sip_bridge)])


class LokiSipSwitch(LokiAccountEntity, SwitchEntity):
    """Turns the SIP client on and off."""

    _attr_translation_key = "sip"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Any, bridge: SipBridge) -> None:
        """Initialise the switch."""
        super().__init__(coordinator)
        self._bridge = bridge
        self._attr_unique_id = f"{self._entry_id}_sip_enabled"

    @property
    def available(self) -> bool:
        """False for an account the operator never issued SIP credentials for."""
        return super().available and self._bridge.available

    @property
    def is_on(self) -> bool:
        """Whether SIP is meant to be running."""
        return bool(self.coordinator.config_entry.options.get(OPT_SIP_ENABLED, False))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start SIP now, and remember the choice across restarts."""
        # Turning the switch on is the explicit gesture that clears a latched
        # permanent failure. Doing it here rather than offering a one-click repair
        # button means the person has already read the card and decided to retry.
        await self._bridge.async_reset_terminal()
        self._store_option(enabled=True)
        await self._bridge.async_start()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop SIP now, and remember the choice across restarts."""
        self._store_option(enabled=False)
        await self._bridge.async_stop()
        self.async_write_ha_state()

    @callback
    def _store_option(self, *, enabled: bool) -> None:
        """Persist the choice without reloading the entry.

        A reload here would tear down every entity mid-service-call, and the whole
        point of the switch is that it acts instantly.
        """
        entry = self.coordinator.config_entry
        self.hass.config_entries.async_update_entry(
            entry, options={**entry.options, OPT_SIP_ENABLED: enabled}
        )
