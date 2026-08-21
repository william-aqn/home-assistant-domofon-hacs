"""Diagnostic sensors for the account."""

from __future__ import annotations

from typing import Any, ClassVar

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import LokiConfigEntry, LokiCoordinator
from .entity import LokiAccountEntity
from .sip.client import SipState
from .sip_bridge import SipBridge, signal_sip_update


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LokiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the account-level sensors."""
    runtime = entry.runtime_data
    if runtime.sip_bridge is None:
        return
    async_add_entities([LokiSipStatusSensor(runtime.coordinator, runtime.sip_bridge)])


class LokiSipStatusSensor(LokiAccountEntity, SensorEntity):
    """What the SIP client is actually doing.

    Separate from the switch on purpose: the switch is the intent, this is the truth.
    The states that matter most are the ones a person would otherwise never see --
    ``baseline`` (the ten quiet minutes before the first registration), ``blocked``
    (somebody else is registered, so we refused to proceed) and ``evicted``
    (registering displaced somebody, so we withdrew and stopped).
    """

    _attr_translation_key = "sip_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options: ClassVar[list[str]] = [state.value for state in SipState]

    def __init__(self, coordinator: LokiCoordinator, bridge: SipBridge) -> None:
        """Initialise the sensor."""
        super().__init__(coordinator)
        self._bridge = bridge
        self._attr_unique_id = f"{self._entry_id}_sip_status"

    @property
    def available(self) -> bool:
        """Always: this reports the SIP client, not the device-list poller.

        The two run against different hosts on different connections, so a failed
        cloud poll says nothing about SIP. Inheriting the coordinator's availability
        would blank this sensor exactly when somebody is trying to work out what is
        wrong.
        """
        return True

    async def async_added_to_hass(self) -> None:
        """Follow the bridge as well as the coordinator."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, signal_sip_update(self._entry_id), self._handle_sip_update
            )
        )

    @callback
    def _handle_sip_update(self) -> None:
        """Redraw when the client moves."""
        self.async_write_ha_state()

    @property
    def native_value(self) -> str:
        """The client's current state."""
        return self._bridge.state.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Enough detail to explain the state without leaking an address.

        The registrar reports the public address it sees us at, which is exactly the
        kind of value that ends up in a screenshot pasted into an issue. It stays in
        diagnostics, where redaction applies, and out of a state attribute.
        """
        attributes: dict[str, Any] = {"detail": self._bridge.detail}
        if (snapshot := self._bridge.snapshot) is not None:
            attributes["foreign_bindings"] = snapshot.foreign_count
            attributes["realm"] = snapshot.realm
        return attributes
