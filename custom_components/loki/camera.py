"""Cameras for doors and standalone cameras."""

from __future__ import annotations

import asyncio
import logging
import time

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import LokiApiError, LokiAuthError
from .call import CallUpdate, signal_call_update
from .const import DOMAIN
from .coordinator import LokiConfigEntry, LokiCoordinator
from .entity import LokiEntity, build_unique_id
from .models import LokiDevice

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

# The backend's own still is cheap and always available, but it is NOT a live frame:
# measured on the real account, three fetches ninety seconds apart returned
# byte-identical JPEGs. Treat it as a recent-ish picture of the door, never as "what is
# there now". For that, see async_capture_from_stream.
#
# The TTL only stops twenty dashboard tiles from asking the operator's API the same
# question at once.
_SNAPSHOT_TTL = 10.0

# A single frame off RTSP. Bounded because the media host is routinely unreachable --
# a VPN, a firewall -- and a capture that hangs would hold a dashboard button hostage.
_CAPTURE_TIMEOUT = 20.0

# How long a frame somebody asked for stays on screen.
#
# It gets its own lifetime rather than sharing the snapshot cache, and a much longer
# one. Sharing was tried and was visibly wrong: the captured frame expired after ten
# seconds, the next routine refresh pulled the backend's static picture back, and the
# wall silently reverted to placeholders a few seconds after the user pressed the
# button. A frame that was explicitly requested outranks one nobody asked for.
_CAPTURE_TTL = 300.0

# A capture this young is reused rather than repeated. A ring sets several things off
# at once -- the integration's own grab, a Telegram blueprint, somebody pressing the
# button on the card -- and each opening its own RTSP connection to photograph the
# same second would be three connections for one picture. Measured: a capture takes
# about three and a half seconds against the live intercom.
_CAPTURE_FRESH = 8.0

# How long the stream is kept ready after a ring, with nobody watching.
#
# Connecting to the intercom is the fast part -- measured at 3.6 s. The rest of the
# wait is HLS: the stream has to cut segments on keyframes and pile up enough of them
# before a player will start, and on this hardware that turned fifteen to twenty
# seconds of staring at a spinner. Started at the ring instead, those seconds are
# spent while the phone is still buzzing, and the picture is there by the time anyone
# opens the card.
#
# The number is an idle timeout, not a lifetime: every read from a viewer pushes it
# back, so watching keeps it alive and looking away lets it go. Long enough to cover a
# whole call with nobody watching at all.
_WARM_STREAM = 150.0


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
        self._captured_image: bytes | None = None
        self._captured_at = 0.0
        # The capture in flight, if any. Shared rather than duplicated: see
        # _CAPTURE_FRESH.
        self._capturing: asyncio.Task[bool] | None = None
        # Advertised whenever the device has a stream URL at all, even while the media
        # host is unreachable. Withdrawing the feature when video is down was tried and
        # made things worse: Home Assistant's own camera dialog stops rendering the
        # still image too, so the user gets a blank card instead of a picture plus an
        # explanation. Reachability is reported by binary_sensor instead.
        self._attr_supported_features = (
            CameraEntityFeature.STREAM
            if device.stream is not None
            else CameraEntityFeature(0)
        )

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
        """Take a real frame the moment this camera's door starts ringing.

        The backend's own still does not follow the camera -- it is the same bytes
        minutes later -- so a notification built from it shows the doorway, not the
        person standing in it. Only the stream knows who is there, and the one moment
        it is worth paying for a stream is this one.

        Started here rather than left to each notification: a ring feeds a phone, a
        messenger, a wall panel and a dashboard, and every one of them wanting its own
        photograph of the same second would be several RTSP connections for one
        picture. They share this capture through _CAPTURE_FRESH.
        """
        if update.device_id != self._device_id or update.kind != "start":
            return
        # Expire, don't discard: the cached frame is still the best fallback if the
        # fresh fetch fails, and it fails exactly when the cloud is busy.
        self._cached_at = 0.0
        if not self._attr_supported_features & CameraEntityFeature.STREAM:
            return
        self.hass.async_create_task(
            self._async_on_ring(), f"{DOMAIN}_ring_{self._device_id}"
        )

    async def _async_on_ring(self) -> None:
        """Get the video ready while somebody is still walking to the screen.

        Two things, in this order and for the same reason: both are worth the one RTSP
        connection a ring justifies, and both are useless if they arrive late.
        """
        await self._async_warm_stream()
        await self.async_capture_from_stream()

    async def _async_warm_stream(self) -> None:
        """Start the stream now so it is not started when somebody presses play.

        Nothing consumes it here. The point is the pipeline: connected, decoding, and
        cutting segments, so a viewer arriving in twenty seconds finds them waiting
        rather than starting the clock themselves. If nobody arrives, the idle timer
        takes it all down again.
        """
        # Never against a host the coordinator has already found dead.
        #
        # A stream started against an unreachable host does not fail and stop: the
        # worker retries for ever on a widening backoff, and the one thing that would
        # take it down -- the provider's idle timer -- is only armed by the arrival of
        # a first segment, which never comes. The camera then reports itself
        # unavailable until Home Assistant restarts, and the media host is hammered
        # the whole time. A timeout on this side does not help: it abandons the wait,
        # not the thread.
        #
        # `is False` and not falsiness: None means nobody has looked yet, and refusing
        # then would leave the picture off until the first poll.
        if self.coordinator.stream_reachable is False:
            return
        try:
            async with asyncio.timeout(_CAPTURE_TIMEOUT):
                stream = await self.async_create_stream()
                if stream is None:
                    return
                stream.add_provider("hls", timeout=_WARM_STREAM)
                await stream.start()
        except (TimeoutError, HomeAssistantError, OSError) as err:
            # The media host is routinely unreachable -- a VPN, a firewall -- and a
            # doorbell must not care.
            _LOGGER.debug("Warming stream for %s failed: %s", self._device_id, err)

    async def stream_source(self) -> str | None:
        """Return the RTSP URL.

        RTSP rather than the HLS form the API hands out directly: HLS adds five to
        twenty seconds of latency, which is useless for answering a door.

        Returned even when the media host is known to be unreachable. Withholding it
        only moves the failure: Home Assistant then logs "does not support play stream
        service" instead, and its camera dialog renders nothing at all. Better to let
        the attempt fail honestly while binary_sensor explains why.
        """
        device = self.device
        return device.stream.rtsp if device and device.stream else None

    async def async_capture_from_stream(self) -> bool:
        """Pull one frame off RTSP and make it the picture this camera serves.

        This is what "current frame" has to mean here. The backend's own still does not
        follow the camera -- it is the same bytes minutes later -- so re-fetching it
        harder buys nothing. Only the stream knows what is in front of the door now.

        Deliberately not wired into ``async_camera_image``: that would put an RTSP
        connection behind every dashboard tile, which is the cost the cheap still
        exists to avoid. This is an action somebody asks for, one frame at a time.

        Callers arriving together share one capture, and one that has just finished is
        not repeated -- otherwise a single ring would photograph the same second three
        times over three separate connections.
        """
        if not self._attr_supported_features & CameraEntityFeature.STREAM:
            return False

        now = time.monotonic()
        fresh = self._captured_image is not None
        if fresh and now - self._captured_at < _CAPTURE_FRESH:
            return True
        if self._capturing is not None and not self._capturing.done():
            return await asyncio.shield(self._capturing)

        self._capturing = self.hass.async_create_task(
            self._async_grab_frame(), f"{DOMAIN}_capture_{self._device_id}"
        )
        return await asyncio.shield(self._capturing)

    async def _async_grab_frame(self) -> bool:
        """The capture itself. One at a time per camera; see the caller."""
        # Same guard, same reason: this opens a stream too.
        if self.coordinator.stream_reachable is False:
            return False
        try:
            async with asyncio.timeout(_CAPTURE_TIMEOUT):
                stream = await self.async_create_stream()
                if stream is None:
                    return False
                # A keyframe rather than whatever partial frame is to hand: without it
                # the first capture after connecting is usually a grey smear.
                image = await stream.async_get_image(wait_for_next_keyframe=True)
        except (TimeoutError, HomeAssistantError, OSError) as err:
            _LOGGER.debug("Capture for device %s failed: %s", self._device_id, err)
            return False

        if not image:
            return False
        self._captured_image = image
        self._captured_at = time.monotonic()
        # Moves entity_picture on, so anything showing this camera re-fetches.
        self.async_write_ha_state()
        return True

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return the picture to show for this camera.

        A frame somebody asked for wins while it is fresh; otherwise the backend's own
        still, which is cheap and keeps working when the video host does not.
        """
        now = time.monotonic()
        if self._captured_image is not None and now - self._captured_at < _CAPTURE_TTL:
            return self._captured_image

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
