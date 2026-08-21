"""Cameras for doors and standalone cameras."""

from __future__ import annotations

import logging
import time

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import LokiApiError, LokiAuthError
from .call import CallUpdate, signal_call_update
from .coordinator import LokiConfigEntry, LokiCoordinator
from .entity import LokiEntity, build_unique_id
from .models import LokiDevice

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# The backend refreshes its stills on its own schedule, so re-fetching per dashboard
# tile is wasted work. Short enough that a snapshot taken on a ring is still current.
_SNAPSHOT_TTL = 10.0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LokiConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a camera for every device that actually has a stream."""
    coordinator = entry.runtime_data.coordinator
    known: set[int] = set()

    @callback
    def _async_add_new() -> None:
        # Doors and cameras are provisioned in the Loki app, and a device whose url
        # was empty at setup can gain a stream later. ``known`` tracks what we actually
        # created, so both cases are picked up without reloading the entry.
        new = [
            device
            for device_id, device in coordinator.data.items()
            if device_id not in known and device.stream is not None
        ]
        if new:
            known.update(device.id for device in new)
            async_add_entities(LokiCamera(coordinator, device) for device in new)

    entry.async_on_unload(coordinator.async_add_listener(_async_add_new))
    _async_add_new()


class LokiCamera(LokiEntity, Camera):
    """Live view of one intercom or camera."""

    _attr_name = None
    # Home Assistant drives its still-based MJPEG fallback at frame_interval (0.5s by
    # default). Polling faster than the snapshot cache just re-serves the same bytes.
    _attr_frame_interval = _SNAPSHOT_TTL

    def __init__(self, coordinator: LokiCoordinator, device: LokiDevice) -> None:
        """Initialise the camera."""
        LokiEntity.__init__(self, coordinator, device)
        # Camera.__init__ sets around ten attributes of its own, and
        # CoordinatorEntity.__init__ does not chain past itself, so a single super()
        # call here would leave the Camera half uninitialised. The two write disjoint
        # attribute sets, so the order between them does not matter.
        Camera.__init__(self)
        self._attr_unique_id = build_unique_id(self._entry_id, device.id)
        # Plain cameras are numerous and mostly uninteresting; doors are the point of
        # the integration, so only those are enabled out of the box.
        self._attr_entity_registry_enabled_default = device.is_door
        self._cached_image: bytes | None = None
        self._cached_at = 0.0
        self._attr_supported_features = self._features()

    def _features(self) -> CameraEntityFeature:
        """Advertise STREAM only while a stream could actually be played.

        Advertising it and then returning no source is worse than not advertising:
        the frontend takes the feature at its word, asks to play, and Home Assistant
        logs "does not support play stream service" for every tile on the dashboard.
        """
        device = self.device
        if device is None or device.stream is None:
            return CameraEntityFeature(0)
        # None means "not checked yet" -- stay optimistic until we know otherwise.
        if self.coordinator.stream_reachable is False:
            return CameraEntityFeature(0)
        return CameraEntityFeature.STREAM

    @callback
    def _handle_coordinator_update(self) -> None:
        """Re-evaluate whether a stream is playable, then write state."""
        self._attr_supported_features = self._features()
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        """Re-add Camera's stream-health check, which the MRO skips.

        LokiEntity -> CoordinatorEntity.available returns without chaining to Camera,
        so a dead RTSP worker would otherwise still report the camera as available.
        """
        if (stream := self.stream) and not stream.available:
            return False
        return super().available

    async def async_added_to_hass(self) -> None:
        """Subscribe to call updates so a ring refreshes the still image."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                signal_call_update(self.coordinator.config_entry.entry_id),
                self._handle_call_update,
            )
        )

    @callback
    def _handle_call_update(self, update: CallUpdate) -> None:
        """Expire the cached still when this camera's door starts ringing.

        The notification the blueprint sends fetches a snapshot the moment the call
        arrives; without this it could serve a frame up to the cache TTL old, i.e. the
        doorway before the visitor stepped into it.
        """
        if update.device_id == self._device_id and update.kind == "start":
            # Expire, don't discard: the cached frame is still the best fallback if
            # the fresh fetch fails, and it fails exactly when the cloud is busy.
            self._cached_at = 0.0

    async def stream_source(self) -> str | None:
        """Return the RTSP URL, or None when no stream could possibly open.

        RTSP rather than the HLS form the API hands out directly: HLS adds five to
        twenty seconds of latency, which is useless for answering a door.

        Handing back a URL we already know is unreachable is worse than admitting it:
        Home Assistant retries the stream every couple of minutes and fills the log
        with ffmpeg timeouts, which buries the one line that would have explained the
        problem. The still image keeps working either way.
        """
        device = self.device
        if device is None or device.stream is None:
            return None
        if self.coordinator.stream_reachable is False:
            return None
        return device.stream.rtsp

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a still image, proxied from the backend.

        Uses the backend's own periodically-updated still rather than decoding a frame
        off RTSP: it is far cheaper and it works for devices whose stream is briefly
        unavailable.
        """
        now = time.monotonic()
        if self._cached_image is not None and now - self._cached_at < _SNAPSHOT_TTL:
            return self._cached_image

        try:
            image = await self.coordinator.client.async_get_snapshot(self._device_id)
        except (LokiApiError, LokiAuthError) as err:
            _LOGGER.debug("Snapshot for device %s failed: %s", self._device_id, err)
            # Serving the previous frame beats a broken tile while the cloud hiccups.
            return self._cached_image

        self._cached_image = image
        self._cached_at = now
        return image
