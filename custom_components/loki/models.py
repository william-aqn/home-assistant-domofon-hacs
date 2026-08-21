"""Data model for Loki devices.

This module owns *all* interpretation of the backend's ``url`` field. Nothing else
in the integration may parse it -- see ``normalize_stream`` for why that matters.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import logging
import re
from typing import Any, Self
from urllib.parse import urlsplit

from .const import HLS_PORT, RTSP_PORT

_LOGGER = logging.getLogger(__name__)

_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Keys of the two arrays the device list endpoint returns. There is no type field
# inside a device object -- door vs camera is decided purely by which array it
# arrived in.
KEY_DOORS = "dom"
KEY_CAMERAS = "video"


@dataclass(frozen=True, slots=True)
class StreamUrls:
    """The two playable forms of one camera channel."""

    host: str
    channel: str

    @property
    def _authority(self) -> str:
        """Host in URL-authority form: a bare IPv6 literal needs brackets."""
        return f"[{self.host}]" if ":" in self.host else self.host

    @property
    def rtsp(self) -> str:
        """Low-latency form. This is what Home Assistant streams."""
        return f"rtsp://{self._authority}:{RTSP_PORT}/{self.channel}"

    @property
    def hls(self) -> str:
        """What the official app plays. Kept as a fallback; adds 5-20s of latency."""
        return f"http://{self._authority}:{HLS_PORT}/{self.channel}/index.m3u8"


def _is_plausible_host(host: str) -> bool:
    """Whether a string can actually be used as a host in a stream URL.

    The tolerant fallback below can extract nonsense from badly malformed input, and a
    camera entity built on a host that can never resolve is worse than no entity at
    all -- it is a permanently broken tile the user has to diagnose.
    """
    if _HOSTNAME_RE.match(host):
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _split_host_and_path(raw: str) -> tuple[str, str] | None:
    """Return (hostname, path) from a URL, tolerating malformed input.

    Mirrors ``parseUrlStream`` in the reference client, including its fallback for
    strings that are not parseable URLs.
    """
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
    except ValueError:
        # urlsplit raises on bracketed hosts that are not valid IPv6, e.g.
        # "http://[not-ipv6]/ch". Fall through to the tolerant parser below, exactly
        # as the reference client's try/catch around new URL() does.
        parsed, hostname = None, None

    if parsed is not None and hostname:
        # ``hostname`` already drops userinfo (the inline ``1:1@`` credentials) and
        # the port, both of which we deliberately discard.
        return hostname, parsed.path

    # Fallback for input without a usable scheme, e.g. "host:8888/channel". Drop the
    # scheme first if there is one, otherwise "http" itself gets mistaken for the host.
    _, separator, remainder = raw.rpartition("://")
    if not separator:
        remainder = raw
    # Strip userinfo by hand, then separate host from path.
    remainder = remainder.rsplit("@", 1)[-1]
    host, _, path = remainder.partition("/")
    host = host.split(":", 1)[0]
    if not host or not _is_plausible_host(host):
        return None
    return host, f"/{path}" if path else ""


def normalize_stream(raw: Any) -> StreamUrls | None:
    """Derive playable stream URLs from a device's ``url`` field.

    The backend's data is internally inconsistent and cannot be trusted as a URL:

    * the scheme may say ``rtsp://`` while the port is the HTTP one (8888);
    * credentials may be inlined (``http://1:1@host:8888/channel``);
    * the field may be empty, meaning the device simply has no camera.

    So only the hostname and the channel id are taken from it; the scheme and port
    are rebuilt from known-good constants. Returns ``None`` when there is no usable
    stream, in which case no camera entity should be created for the device.
    """
    # The backend's ``url`` field arrives off the wire and is not guaranteed to be a
    # string at all.
    if not isinstance(raw, str) or not raw.strip():
        return None

    split = _split_host_and_path(raw.strip())
    if split is None:
        return None

    host, path = split
    # The reference client takes the *first* path segment. Channels are single
    # segment today, so this only matters if that ever changes -- in which case we
    # want to behave like the client that is known to work, not diverge silently.
    channel = next((segment for segment in path.split("/") if segment), "")
    if not host or not channel:
        return None

    return StreamUrls(host=host, channel=channel)


@dataclass(frozen=True, slots=True)
class LokiDevice:
    """One door or camera as reported by ``/api/device/list/``."""

    id: int
    name: str
    area_path: str
    is_door: bool
    has_thumbnail: bool
    stream: StreamUrls | None

    @classmethod
    def from_api(cls, payload: dict[str, Any], *, is_door: bool) -> Self | None:
        """Build a device from one array element, or None if it is unusable."""
        raw_id = payload.get("id")
        # bool is a subclass of int, and True would hash equal to device 1.
        if not isinstance(raw_id, int) or isinstance(raw_id, bool):
            return None

        name = str(payload.get("name") or "").strip() or f"#{raw_id}"

        try:
            stream = normalize_stream(payload.get("url"))
        except Exception:  # noqa: BLE001 - one bad url must not lose every device
            _LOGGER.debug("Unparseable stream url on device %s", raw_id)
            stream = None

        return cls(
            id=raw_id,
            name=name,
            area_path=str(payload.get("rname") or "").strip(),
            is_door=is_door,
            # The ``img`` field accumulates repeated "?a=<hash>" suffixes and turns
            # into an invalid URL, so it is only ever used as a "a thumbnail exists"
            # flag. The real path is built from the device id instead.
            has_thumbnail=bool(str(payload.get("img") or "").strip()),
            stream=stream,
        )

    @property
    def area(self) -> str | None:
        """Deepest segment of the ``->``-delimited location path, for suggested_area."""
        if not self.area_path:
            return None
        segment = self.area_path.split("->")[-1].strip()
        return segment or None


def parse_device_list(payload: dict[str, Any]) -> list[LokiDevice]:
    """Parse a device list response into a flat, deduplicated device list."""
    devices: dict[int, LokiDevice] = {}

    for key, is_door in ((KEY_DOORS, True), (KEY_CAMERAS, False)):
        entries = payload.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            device = LokiDevice.from_api(entry, is_door=is_door)
            if device is None:
                continue
            existing = devices.get(device.id)
            # A device appearing in both arrays is treated as a door: doors are the
            # only devices lockOpen accepts, and losing that would lose the button.
            # Among duplicates of the same role the first wins, so the result no
            # longer depends on array order.
            if existing is not None and (existing.is_door or not is_door):
                continue
            devices[device.id] = device

    return sorted(
        devices.values(), key=lambda device: (not device.is_door, device.name)
    )
