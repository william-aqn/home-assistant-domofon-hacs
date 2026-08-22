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
    # The order is the colour.
    #
    # Home Assistant draws an enum sensor's history by looking the state up in this
    # list and taking the palette entry at that index -- `options.indexOf(state)` into
    # `--color-1..54`. So position 8 is green (#01ab63), 2 is red (#ff725c), 1 and 10
    # are amber, 6 is brown. In enum order `registered` landed on brown and `blocked`
    # on green, which is the wrong way round for the one chart anybody opens.
    #
    # Hence an explicit list rather than the enum's own order: green for the state we
    # want, red and amber for the ones that need attention, cool colours for the steps
    # on the way. `test_sip_status_options_cover_every_state` keeps it honest -- a new
    # SipState missing from here would be a state the sensor cannot report at all.
    _attr_options: ClassVar[list[str]] = [
        SipState.CONNECTING.value,  # 0  blue
        SipState.BACKOFF.value,  # 1  amber -- waiting to retry
        SipState.FAILED.value,  # 2  red
        SipState.PROBING.value,  # 3  mint
        SipState.REGISTERING.value,  # 4  purple
        SipState.VERIFYING.value,  # 5  pink
        SipState.EVICTED.value,  # 6  brown
        SipState.DISABLED.value,  # 7  pale blue -- off, not broken
        SipState.REGISTERED.value,  # 8  green
        SipState.BASELINE.value,  # 9  deep blue -- the long quiet watch
        SipState.BLOCKED.value,  # 10 deep amber -- refused, on purpose
    ]

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
        # Whether the registrar sees us at a different public address than it did
        # before the restart. If a provider hands out a new one per connection,
        # remembering our own Contact by address is hopeless -- and that is a
        # different repair from anything else on this sensor.
        if self._bridge.address_changed is not None:
            attributes["address_changed"] = self._bridge.address_changed
        if (snapshot := self._bridge.snapshot) is not None:
            attributes["foreign_bindings"] = snapshot.foreign_count
            attributes["realm"] = snapshot.realm
            if snapshot.foreign:
                # Neither of these names an address, and together they answer the
                # only question worth asking when the account looks busy: is this
                # our own binding from before a restart, and how long until it goes.
                attributes["foreign_where"] = snapshot.foreign_where
                attributes["foreign_expires_in"] = snapshot.foreign_expires_in
                attributes["foreign_same_user"] = snapshot.foreign_same_user
            # Not only while something is being refused. Half the diagnosis is
            # whether anything was remembered at all, and that half has to be
            # readable BEFORE the restart that loses it -- afterwards there is
            # nothing left to compare against.
            attributes["known_contacts"] = snapshot.known_contacts
            # How many rows the account carries in total, ours included. While
            # registered this should be one. Two means registering leaves a second
            # row behind -- the address of our own socket, which the correction was
            # supposed to withdraw -- and that second row is what the next start
            # cannot recognise once the container comes up on a different address.
            attributes["bindings_total"] = len(snapshot.bindings)
        return attributes
