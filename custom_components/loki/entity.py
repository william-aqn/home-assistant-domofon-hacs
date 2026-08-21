"""Shared entity base for Loki."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import LokiCoordinator
from .models import LokiDevice


def build_unique_id(entry_id: str, device_id: int, suffix: str = "") -> str:
    """Entry-scoped unique id.

    Entity unique_ids are global across config entries, and two accounts legitimately
    see the same shared entrance door -- adding a second phone number is the
    documented way to run Home Assistant alongside the resident's own app. Without the
    entry prefix the second entry would silently lose every entity to a duplicate-id
    abort. ``call.py`` resolves camera entity ids through this same function, so the
    format has exactly one definition.
    """
    return f"{entry_id}_{device_id}{suffix}"


def account_device_info(entry_id: str, title: str) -> DeviceInfo:
    """The device representing the account itself.

    Integration-wide diagnostics -- video reachability now, SIP status later -- belong
    to the account rather than to any one door, and hanging them off a door would put
    a "video unavailable" card on an entrance that has nothing to do with the problem.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, f"account_{entry_id}")},
        name=f"Loki {title}",
        manufacturer="Loki",
        model="Учётная запись",
        entry_type=DeviceEntryType.SERVICE,
    )


class LokiAccountEntity(CoordinatorEntity[LokiCoordinator]):
    """Base for entities describing the account rather than a device."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: LokiCoordinator) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._entry_id = entry.entry_id
        self._attr_device_info = account_device_info(entry.entry_id, entry.title)


class LokiEntity(CoordinatorEntity[LokiCoordinator]):
    """Base entity bound to one Loki device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: LokiCoordinator, device: LokiDevice) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        self._device_id = device.id
        self._entry_id = coordinator.config_entry.entry_id
        self._attr_device_info = DeviceInfo(
            # Deliberately global, unlike the entity unique_id: two accounts that can
            # both see one shared gate should show one device card, not two.
            identifiers={(DOMAIN, str(device.id))},
            name=device.name,
            manufacturer="Loki",
            model="Дверь" if device.is_door else "Камера",
            suggested_area=device.area,
        )

    @property
    def device(self) -> LokiDevice | None:
        """The device as of the last refresh, or None if it disappeared."""
        return self.coordinator.data.get(self._device_id)

    @property
    def available(self) -> bool:
        """Whether the backend still reports this device."""
        return super().available and self.device is not None
