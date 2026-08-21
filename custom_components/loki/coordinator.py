"""Device list coordinator and per-entry runtime state."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import LokiApiError, LokiAuthError, LokiClient
from .const import (
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MEDIA_PROBE_TIMEOUT,
    OPT_SCAN_INTERVAL,
    RTSP_PORT,
)
from .models import LokiDevice
from .reauth import async_clear_auth_failed, async_fire_auth_failed

if TYPE_CHECKING:
    from .call import CallManager
    from .sip_bridge import SipBridge

_LOGGER = logging.getLogger(__name__)


@dataclass
class LokiRuntimeData:
    """Everything one config entry owns while it is loaded."""

    client: LokiClient
    coordinator: LokiCoordinator
    call_manager: CallManager
    # None only for an account the operator issued no SIP credentials for; the SIP
    # platforms then create no entities rather than showing permanently dead ones.
    sip_bridge: SipBridge | None = None


type LokiConfigEntry = ConfigEntry[LokiRuntimeData]


class LokiCoordinator(DataUpdateCoordinator[dict[int, LokiDevice]]):
    """Polls the device list.

    Doors and cameras change very rarely, so this runs on a slow interval. Newly
    provisioned devices do gain entities without a reload; a *rename* in the Loki app
    only reaches the device registry after the entry is reloaded.
    """

    config_entry: LokiConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: LokiConfigEntry,
        client: LokiClient,
    ) -> None:
        """Initialise the coordinator."""
        interval = entry.options.get(OPT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=(
                timedelta(seconds=interval) if interval else DEFAULT_SCAN_INTERVAL
            ),
            # LokiDevice is a frozen dataclass, so an unchanged list compares equal
            # and there is no reason to wake every listener every five minutes.
            always_update=False,
        )
        self.client = client
        # None until the first poll; False means live video cannot work at all.
        self.stream_reachable: bool | None = None

    async def _async_update_data(self) -> dict[int, LokiDevice]:
        """Fetch the current device list."""
        try:
            devices = await self.client.async_get_devices()
        except LokiAuthError as err:
            # Only a fresh SMS login can recover from this. The event is latched, so
            # this fires once rather than on every poll for as long as it stays broken.
            async_fire_auth_failed(self.hass, self.config_entry, err)
            raise ConfigEntryAuthFailed(
                str(err),
                translation_domain=DOMAIN,
                translation_key="auth_expired",
                translation_placeholders={"phone": str(self.config_entry.title)},
            ) from err
        except LokiApiError as err:
            raise UpdateFailed(str(err)) from err

        async_clear_auth_failed(self.hass, self.config_entry)
        await self._async_check_media_hosts(devices)
        return {device.id: device for device in devices}

    async def _async_check_media_hosts(self, devices: list[LokiDevice]) -> None:
        """Note whether the video hosts can be reached at all.

        Without this the integration degrades far too gracefully: the camera keeps
        showing the backend's periodically-updated still while the live stream is dead,
        and the only evidence is an ffmpeg timeout buried in the Home Assistant log.
        A VPN, a firewall or an operator outage all look identical from the dashboard.

        A TCP connect is enough and costs nothing -- video is served from a different
        host to the API, so the API working says nothing about the streams.
        """
        hosts = {
            (device.stream.host, RTSP_PORT)
            for device in devices
            if device.stream is not None
        }
        if not hosts:
            self.stream_reachable = None
            return

        results = await asyncio.gather(
            *(self._async_can_connect(host, port) for host, port in hosts)
        )
        reachable = any(results)

        if reachable != self.stream_reachable:
            if reachable:
                _LOGGER.info("Видеопоток снова доступен")
            else:
                _LOGGER.warning(
                    "Видеохост недоступен: живое видео работать не будет, останутся "
                    "только снимки. Обычная причина — VPN или межсетевой экран, "
                    "закрывающий порт %s. Снимки идут через другой хост и продолжают "
                    "работать, поэтому карточка выглядит исправной",
                    RTSP_PORT,
                )
        self.stream_reachable = reachable

    async def _async_can_connect(self, host: str, port: int) -> bool:
        """Whether a TCP connection to a media host succeeds."""
        writer = None
        try:
            async with asyncio.timeout(MEDIA_PROBE_TIMEOUT):
                _reader, writer = await asyncio.open_connection(host, port)
        except (OSError, TimeoutError):
            return False
        else:
            return True
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
