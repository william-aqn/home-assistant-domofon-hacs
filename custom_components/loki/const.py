"""Constants for the Loki integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "loki"

# Shown in the config flow so the full terms are one click away from the checkbox.
DISCLAIMER_URL: Final = (
    "https://github.com/william-aqn/home-assistant-domofon-hacs/blob/main/DISCLAIMER.md"
)

# The upstream service endpoint. This is infrastructure, not branding -- it must
# match the operator's real host or nothing works.
DEFAULT_API_HOST: Final = "https://app.risan-service.ru"

# The official Android client identifies itself this way. The backend does not appear
# to enforce it, but sending something familiar keeps us from standing out.
DEFAULT_USER_AGENT: Final = (
    "Dalvik/2.1.0 (Linux; U; Android 11; sdk_gphone_x86 Build/RSR1.201013.001)"
)

DEFAULT_SCAN_INTERVAL: Final = timedelta(minutes=5)

# A call with no explicit end -- a missed SIP CANCEL, or a simulate_ring left hanging
# in testing -- must not pin a door's call_active sensor on forever. The blueprint's
# wait timeout is documented to match this.
CALL_TIMEOUT: Final = 120

# --- config entry keys -------------------------------------------------------

CONF_PHONE: Final = "phone"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_SIP: Final = "sip"
CONF_MASTER_FLG: Final = "master_flg"
CONF_MAX_PHONES: Final = "max_phones"

# --- phase 3 (SIP), not yet wired --------------------------------------------
# Keys inside the ``sip`` object stored at entry.data[CONF_SIP]. Note CONF_SIP_USER
# is also the string "phone": harmless only while the SIP data stays nested.

CONF_SIP_URL: Final = "url"
CONF_SIP_USER: Final = "phone"
CONF_SIP_PASSWORD: Final = "password"

OPT_SIP_ENABLED: Final = "sip_enabled"

# --- phase 4 (Frigate), not yet wired ----------------------------------------

OPT_FRIGATE_ENABLED: Final = "frigate_enabled"
OPT_FRIGATE_TOPIC_PREFIX: Final = "frigate_topic_prefix"
OPT_FRIGATE_CAMERA_MAP: Final = "frigate_camera_map"
OPT_FACE_CORRELATION_WINDOW: Final = "face_correlation_window"

DEFAULT_FRIGATE_TOPIC_PREFIX: Final = "frigate"
DEFAULT_FACE_CORRELATION_WINDOW: Final = 30

EVENT_TYPE_FACE_RECOGNIZED: Final = "face_recognized"

# --- options keys ------------------------------------------------------------

OPT_SCAN_INTERVAL: Final = "scan_interval"

# --- events ------------------------------------------------------------------

EVENT_LOKI: Final = "loki_event"

EVENT_TYPE_CALL_INCOMING: Final = "call_incoming"
EVENT_TYPE_CALL_ENDED: Final = "call_ended"

# Matches HA's DoorbellEventType.RING. Imported as a literal rather than from
# homeassistant.components.event because that enum postdates our minimum version.
EVENT_RING: Final = "ring"

# --- services ----------------------------------------------------------------

SERVICE_OPEN_DOOR: Final = "open_door"
SERVICE_SIMULATE_RING: Final = "simulate_ring"
SERVICE_HANGUP: Final = "hangup"

ATTR_DEVICE_ID: Final = "device_id"

# --- stream layout -----------------------------------------------------------
#
# The media host serves the same channel two ways. The scheme and port carried in
# the API's own ``url`` field are unreliable (see models.normalize_stream), so we
# rebuild both forms from the hostname and channel id alone.

RTSP_PORT: Final = 8554
HLS_PORT: Final = 8888
