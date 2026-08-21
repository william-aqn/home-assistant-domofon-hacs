"""Integration-level actions."""

from __future__ import annotations

from typing import cast

from homeassistant.components.camera.const import (
    DATA_COMPONENT as CAMERA_DATA_COMPONENT,
)
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
import voluptuous as vol

from .api import LokiApiError, LokiAuthError, LokiDeviceForbidden
from .camera import LokiCamera
from .const import (
    ATTR_DEVICE_ID,
    DOMAIN,
    SERVICE_CAPTURE,
    SERVICE_HANGUP,
    SERVICE_OPEN_DOOR,
    SERVICE_SIMULATE_RING,
)
from .coordinator import LokiConfigEntry
from .entity import build_unique_id
from .models import LokiDevice

_DEVICE_ID_SCHEMA = vol.Schema(
    {
        # This is the Loki numeric id, not Home Assistant's device-registry id. The
        # actions deliberately declare no `target:`, so the two never meet.
        vol.Required(ATTR_DEVICE_ID): vol.All(vol.Coerce(int), vol.Range(min=1))
    }
)


def _find_camera(
    hass: HomeAssistant, entry: LokiConfigEntry, device_id: int
) -> LokiCamera | None:
    """This entry's camera object for a door, or None if it has no camera.

    Goes through the entity registry rather than guessing an entity id: ids here are
    derived from device names, and a rename would silently break this.
    """
    entity_id = er.async_get(hass).async_get_entity_id(
        "camera", DOMAIN, build_unique_id(entry.entry_id, device_id)
    )
    if entity_id is None:
        return None
    component = hass.data.get(CAMERA_DATA_COMPONENT)
    entity = component.get_entity(entity_id) if component else None
    return entity if isinstance(entity, LokiCamera) else None


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions once, from async_setup.

    Deliberately not from async_setup_entry: setup can bail out with
    ConfigEntryNotReady while the cloud is down, and an action that only exists on a
    healthy start is an action a notification button cannot rely on.
    """

    def _find_device(
        device_id: int, *, door_only: bool = False
    ) -> tuple[LokiConfigEntry, LokiDevice]:
        """Locate the loaded entry and device for an id, or raise for the UI."""
        loaded = cast(
            "list[LokiConfigEntry]", hass.config_entries.async_loaded_entries(DOMAIN)
        )
        if not loaded:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_loaded_entry"
            )

        for entry in loaded:
            if not hasattr(entry, "runtime_data"):
                # runtime_data is assigned mid-setup and deleted on unload.
                continue
            device = entry.runtime_data.coordinator.data.get(device_id)
            if device is None:
                continue
            if door_only and not device.is_door:
                # A plain camera has no relay and no call entities, so ringing or
                # opening it would produce a call nothing in HA can answer.
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="not_a_door",
                    translation_placeholders={
                        "device_id": str(device_id),
                        "device_name": device.name,
                    },
                )
            return entry, device

        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="device_not_found",
            translation_placeholders={"device_id": str(device_id)},
        )

    async def _async_open_door(call: ServiceCall) -> None:
        """Open a door by its Loki device id.

        Exists so a notification action can carry a plain device id rather than an
        entity id, which is what makes the incoming-call blueprint work.
        """
        entry, device = _find_device(call.data[ATTR_DEVICE_ID], door_only=True)
        try:
            await entry.runtime_data.client.async_open_door(device_id=device.id)
        except LokiDeviceForbidden as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="open_forbidden",
                translation_placeholders={"error": str(err)},
            ) from err
        except LokiAuthError as err:
            entry.async_start_reauth(hass)
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

    async def _async_simulate_ring(call: ServiceCall) -> None:
        """Fake an incoming call, to test the whole notification chain without SIP."""
        entry, device = _find_device(call.data[ATTR_DEVICE_ID], door_only=True)
        entry.runtime_data.call_manager.async_start_call(device, context=call.context)

    async def _async_hangup(call: ServiceCall) -> None:
        """End the active call for a door.

        Clearing the tracked state is all a simulated call needs. For a real one the
        SIP bridge is listening to the same signal and declines its own branch, which
        is what lets the resident's phone finish the call.
        """
        entry, device = _find_device(call.data[ATTR_DEVICE_ID], door_only=True)
        entry.runtime_data.call_manager.async_end_call(device.id, reason="hangup")

    async def _async_capture(call: ServiceCall) -> None:
        """Grab one live frame off RTSP for a door, replacing its cached still.

        Exists because the backend's own still is static: it does not follow the
        camera, so "show me what is there now" cannot be answered by fetching it
        again. The stream can answer it, one frame at a time, without paying for a
        continuous view.
        """
        entry, device = _find_device(call.data[ATTR_DEVICE_ID])
        camera = _find_camera(hass, entry, device.id)
        if camera is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_camera",
                translation_placeholders={
                    "device_id": str(device.id),
                    "device_name": device.name,
                },
            )
        if not await camera.async_capture_from_stream():
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="capture_failed",
                translation_placeholders={"device_name": device.name},
            )

    hass.services.async_register(
        DOMAIN, SERVICE_OPEN_DOOR, _async_open_door, schema=_DEVICE_ID_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SIMULATE_RING, _async_simulate_ring, schema=_DEVICE_ID_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_HANGUP, _async_hangup, schema=_DEVICE_ID_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CAPTURE, _async_capture, schema=_DEVICE_ID_SCHEMA
    )
